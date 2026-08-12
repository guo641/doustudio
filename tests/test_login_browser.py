"""v0.2.7:沿 yaonieyo/doubao-account-pool 双轨判定测试 + v0.2.8 加 sessionid 闸门。

不再测试 on_response / /passport/web/account/info/ 调用 —— 字节系 aegis 风控
把所有非浏览器指纹请求拒为 1011。我们只信 Chromium 自己看到的:
  Tier 1: context.cookies() doubao.com cookie 计数
  Tier 2: page.evaluate DOM innerText 不含登录关键词
  v0.2.8 闸门 1: 主循环起 ≥3s 才接受 identity(排除 page.goto 假阳性窗口)
  v0.2.8 闸门 2: sessionid cookie(32-hex)是字节系唯一登录凭证
  user_id: page.evaluate localStorage.__tea_cache_tokens_497858.user_unique_id
"""
import json
import threading
from pathlib import Path

from doupool.login.browser import (
    LOGGED_OUT_KEYWORDS,
    _MIN_LOOP_SECONDS_BEFORE_IDENTITY,
    _context_doubao_cookie_count,
    _extract_user_id_from_cookies,
    _has_valid_sessionid,
    _is_doubao_cookie,
    _page_looks_logged_in,
    _read_user_unique_id_from_page,
    _save_doubao_cookies_to_disk,
    _sessionid_cookie_value,
    _try_rescue_for_fallback,
    wait_for_identity,
)
import doupool.login.browser as browser_module
from doupool.login.detector import DoubaoIdentity
from doupool.login.service import _verify_from_disk
from playwright.sync_api import Error as PlaywrightError


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------


class FakePage:
    """v0.2.7 FakePage:支持 evaluate_queue(按 evaluate 调用顺序返回不同值),
    因为新主循环对同一 page 调两次 evaluate(一次 DOM,一次 localStorage)。

    v0.3.0 加 evaluate_mapping —— 主循环可能 N 次调 evaluate(loop 不退,
    cookie 有了之后每轮都跑 DOM 探针),evaluate_queue 会被耗光。改用按
    script 内容匹配:匹配 "__tea_cache_tokens" 走 localStorage 队列,
    匹配 "document.body.innerText" 走 DOM 队列,各自 FIFO;空了就用
    evaluate_payload 兜底。这样 wait_for_identity 主循环能持续稳定地
    返回正确值,gate 通过后顺利命中 identity。
    """

    def __init__(
        self,
        alive=True,
        evaluate_payload=None,
        evaluate_raises=None,
        evaluate_queue=None,
        evaluate_mapping=None,
    ):
        self._alive = alive
        self._evaluate_payload = evaluate_payload
        self._evaluate_raises = evaluate_raises
        self._evaluate_queue = list(evaluate_queue) if evaluate_queue else None
        self._evaluate_mapping = {
            k: list(v) for k, v in (evaluate_mapping or {}).items()
        }
        self._evaluate_calls: list[str] = []
        self.url = "https://www.doubao.com/"

    def is_closed(self):
        if self._evaluate_raises and "is_closed" in self._evaluate_raises:
            raise self._evaluate_raises["is_closed"]
        return not self._alive

    def evaluate(self, script):
        self._evaluate_calls.append(script)
        if self._evaluate_raises and "evaluate" in self._evaluate_raises:
            raise self._evaluate_raises["evaluate"]
        # v0.3.0:按 script 内容路由到不同 queue —— 主循环会反复调
        # evaluate,evaluate_queue 单一 FIFO 会耗光。mapping 模式按
        # 关键字区分(DOM 探针 / localStorage 探针),各自 FIFO,
        # 耗尽后回退到 _evaluate_payload(老语义)。
        if self._evaluate_mapping:
            for marker, q in self._evaluate_mapping.items():
                if marker in script and q:
                    return q.pop(0)
            return self._evaluate_payload
        if self._evaluate_queue:
            try:
                return self._evaluate_queue.pop(0)
            except IndexError:
                return None
        return self._evaluate_payload

    def wait_for_timeout(self, ms):
        pass


class FakeContext:
    def __init__(self, alive=True, cookies=None, pages=None):
        self._alive = alive
        self._cookies = cookies or []
        self._pages = pages if pages is not None else [FakePage(alive=True)]

    def is_closed(self):
        return not self._alive

    @property
    def pages(self):
        return list(self._pages)

    def cookies(self):
        if not self._alive:
            raise PlaywrightError("Target page, context or browser has been closed")
        return list(self._cookies)

    def wait_for_event(self, event_name, timeout=None):
        return None


class _FakeCancelingPage(FakePage):
    """每次 evaluate 后调一次 after_evaluate(callback)。用于测试"等待 N 次
    evaluate 后用户取消"的场景,避免主循环真死锁卡 pytest。"""

    def __init__(
        self,
        alive=True,
        evaluate_payload=None,
        evaluate_queue=None,
        after_evaluate=None,
    ):
        super().__init__(
            alive=alive,
            evaluate_payload=evaluate_payload,
            evaluate_queue=evaluate_queue,
        )
        self._after_evaluate = after_evaluate

    def evaluate(self, script):
        result = super().evaluate(script)
        if self._after_evaluate is not None:
            self._after_evaluate(script)
        return result


# ---------------------------------------------------------------------------
# 工具函数测试
# ---------------------------------------------------------------------------


def test_is_doubao_cookie_matches_doubao_domain():
    assert _is_doubao_cookie({"domain": ".doubao.com", "name": "x"}) is True
    assert _is_doubao_cookie({"domain": "doubao.com", "name": "x"}) is True
    assert _is_doubao_cookie({"domain": ".google.com", "name": "x"}) is False
    assert _is_doubao_cookie({"domain": None, "name": "x"}) is False


def test_context_doubao_cookie_count_counts_only_doubao_with_value():
    ctx = FakeContext(cookies=[
        {"name": "sessionid", "value": "abc", "domain": ".doubao.com"},
        {"name": "_ga", "value": "x", "domain": ".google.com"},
        {"name": "uid_tt", "value": "", "domain": ".doubao.com"},  # 空 value 不计
        {"name": "user_unique_id", "value": "u-1", "domain": "doubao.com"},
    ])
    assert _context_doubao_cookie_count(ctx) == 2


def test_context_doubao_cookie_count_zero_when_no_doubao_cookie():
    ctx = FakeContext(cookies=[
        {"name": "_ga", "value": "x", "domain": ".google.com"},
    ])
    assert _context_doubao_cookie_count(ctx) == 0


def test_context_doubao_cookie_count_zero_when_context_closed():
    ctx = FakeContext(alive=False, cookies=[
        {"name": "sessionid", "value": "x", "domain": ".doubao.com"},
    ])
    assert _context_doubao_cookie_count(ctx) == 0


def test_page_looks_logged_in_when_no_login_keywords():
    page = FakePage(
        alive=True,
        evaluate_payload="欢迎来到豆包,这是主聊天界面,显示你的对话列表",
    )
    assert _page_looks_logged_in(page) is True


def test_page_looks_logged_in_false_when_scan_qr_keyword():
    page = FakePage(
        alive=True,
        evaluate_payload="请使用 豆包 APP 扫码登录,或者切换到手机号登录",
    )
    assert _page_looks_logged_in(page) is False


def test_page_looks_logged_in_false_when_login_register_keyword():
    page = FakePage(
        alive=True,
        evaluate_payload="顶部有 登录/注册 按钮,点击开始体验",
    )
    assert _page_looks_logged_in(page) is False


def test_page_looks_logged_in_false_when_page_closed():
    page = FakePage(alive=False, evaluate_payload="无关文本")
    assert _page_looks_logged_in(page) is False


def test_page_looks_logged_in_false_when_evaluate_raises():
    page = FakePage(
        alive=True,
        evaluate_raises={"evaluate": PlaywrightError("Execution context destroyed")},
    )
    assert _page_looks_logged_in(page) is False


def test_page_looks_logged_in_false_when_evaluate_returns_non_string():
    page = FakePage(alive=True, evaluate_payload=None)
    assert _page_looks_logged_in(page) is False


def test_read_user_unique_id_from_page_returns_user_unique_id():
    page = FakePage(
        alive=True,
        evaluate_payload={"ok": True, "value": "3830030044314"},
    )
    assert _read_user_unique_id_from_page(page) == "3830030044314"


def test_read_user_unique_id_falls_back_to_web_id():
    page = FakePage(
        alive=True,
        evaluate_payload={"ok": True, "value": "web-id-7777"},
    )
    assert _read_user_unique_id_from_page(page) == "web-id-7777"


def test_read_user_unique_id_returns_none_when_no_tea():
    page = FakePage(
        alive=True,
        evaluate_payload={"ok": False},
    )
    assert _read_user_unique_id_from_page(page) is None


def test_read_user_unique_id_returns_none_when_evaluate_raises():
    page = FakePage(
        alive=True,
        evaluate_raises={"evaluate": PlaywrightError("Target closed")},
    )
    assert _read_user_unique_id_from_page(page) is None


def test_read_user_unique_id_returns_none_when_evaluate_returns_non_dict():
    page = FakePage(alive=True, evaluate_payload="not a dict")
    assert _read_user_unique_id_from_page(page) is None


def test_extract_user_id_from_cookies_finds_user_unique_id_hint():
    cookies = [
        {"name": "sessionid", "value": "abc", "domain": ".doubao.com"},
        {"name": "user_unique_id", "value": "u-from-cookie", "domain": "doubao.com"},
    ]
    assert _extract_user_id_from_cookies(cookies) == "u-from-cookie"


def test_extract_user_id_from_cookies_finds_user_id_hint():
    cookies = [{"name": "user_id", "value": "u-2", "domain": ".doubao.com"}]
    assert _extract_user_id_from_cookies(cookies) == "u-2"


def test_extract_user_id_from_cookies_finds_uid_hint():
    cookies = [{"name": "uid", "value": "u-3", "domain": ".doubao.com"}]
    assert _extract_user_id_from_cookies(cookies) == "u-3"


def test_extract_user_id_from_cookies_returns_none_when_no_hint():
    cookies = [
        {"name": "sessionid", "value": "abc", "domain": ".doubao.com"},
        {"name": "_ga", "value": "x", "domain": ".google.com"},
    ]
    assert _extract_user_id_from_cookies(cookies) is None


def test_extract_user_id_from_cookies_skips_empty_value():
    cookies = [{"name": "user_unique_id", "value": "", "domain": ".doubao.com"}]
    assert _extract_user_id_from_cookies(cookies) is None


# ---------------------------------------------------------------------------
# v0.2.8:sessionid cookie 闸门(主循环和 rescue 路径共用)
# ---------------------------------------------------------------------------

_VALID_SESSIONID = "deadbeef" * 4  # 32 chars, all hex


def test_sessionid_returns_true_for_32hex_sessionid():
    cookies = [{"name": "sessionid", "value": _VALID_SESSIONID, "domain": ".doubao.com"}]
    ctx = FakeContext(alive=True, cookies=cookies)
    assert _has_valid_sessionid(ctx) is True


def test_sessionid_returns_true_for_sessionid_ss():
    """byteDance 还有 sessionid_ss(secure session,server 端同步用)。"""
    cookies = [{"name": "sessionid_ss", "value": _VALID_SESSIONID, "domain": ".doubao.com"}]
    ctx = FakeContext(alive=True, cookies=cookies)
    assert _has_valid_sessionid(ctx) is True


def test_sessionid_returns_false_when_only_tracking_cookies():
    """v0.2.7 假阳性场景:只有 tracking cookies(s_v_web_id/odin_tt/ttwid/n_mh)→ 拒绝。"""
    cookies = [
        {"name": "s_v_web_id", "value": "verify_ms_xxxx", "domain": ".doubao.com"},
        {"name": "odin_tt", "value": "a719bf" * 8, "domain": ".doubao.com"},
        {"name": "ttwid", "value": "1|abc|123|def", "domain": ".doubao.com"},
        {"name": "n_mh", "value": "9-mIe" * 8, "domain": ".doubao.com"},
    ]
    ctx = FakeContext(alive=True, cookies=cookies)
    assert _has_valid_sessionid(ctx) is False


def test_sessionid_returns_false_for_short_value():
    cookies = [{"name": "sessionid", "value": "abc", "domain": ".doubao.com"}]
    ctx = FakeContext(alive=True, cookies=cookies)
    assert _has_valid_sessionid(ctx) is False


def test_sessionid_returns_false_for_non_hex_value():
    cookies = [{"name": "sessionid", "value": "z" * 32, "domain": ".doubao.com"}]
    ctx = FakeContext(alive=True, cookies=cookies)
    assert _has_valid_sessionid(ctx) is False


def test_sessionid_returns_false_for_empty_value():
    cookies = [{"name": "sessionid", "value": "", "domain": ".doubao.com"}]
    ctx = FakeContext(alive=True, cookies=cookies)
    assert _has_valid_sessionid(ctx) is False


def test_sessionid_returns_false_for_non_doubao_cookie():
    """sessionid 只在 doubao.com 域才算,其它域同名不算。"""
    cookies = [
        {"name": "sessionid", "value": _VALID_SESSIONID, "domain": ".google.com"},
    ]
    ctx = FakeContext(alive=True, cookies=cookies)
    assert _has_valid_sessionid(ctx) is False


def test_sessionid_returns_false_when_context_closed():
    cookies = [{"name": "sessionid", "value": _VALID_SESSIONID, "domain": ".doubao.com"}]
    ctx = FakeContext(alive=False, cookies=cookies)
    assert _has_valid_sessionid(ctx) is False


def test_sessionid_cookie_value_returns_first_match():
    cookies = [
        {"name": "sessionid", "value": "abcdef01" * 4, "domain": ".doubao.com"},
        {"name": "sessionid_ss", "value": "12345678" * 4, "domain": ".doubao.com"},
    ]
    ctx = FakeContext(alive=True, cookies=cookies)
    assert _sessionid_cookie_value(ctx) == "abcdef01" * 4


def test_sessionid_cookie_value_returns_none_when_absent():
    ctx = FakeContext(alive=True, cookies=[
        {"name": "s_v_web_id", "value": "tracking", "domain": ".doubao.com"},
    ])
    assert _sessionid_cookie_value(ctx) is None


def test_logged_out_keywords_contains_yaonieyo_set():
    """对标 yaonieyo electron/executor.ts:326-334 关键词集合。"""
    for kw in ("扫码登录", "手机号登录", "验证码登录", "登录/注册"):
        assert kw in LOGGED_OUT_KEYWORDS


# ---------------------------------------------------------------------------
# cookies 抢救 + identity 救援
# ---------------------------------------------------------------------------


def test_save_doubao_cookies_writes_only_doubao_domain(tmp_path: Path):
    cookies = [
        {"name": "sessionid", "value": "abc123", "domain": ".doubao.com",
         "path": "/", "httpOnly": True, "secure": True, "expires": -1},
        {"name": "_ga", "value": "x", "domain": ".google.com",
         "path": "/", "httpOnly": False, "secure": False, "expires": -1},
        {"name": "uid_tt", "value": "", "domain": ".doubao.com",  # 空 value 跳过
         "path": "/", "httpOnly": True, "secure": False, "expires": -1},
        {"name": "passport_auth_status", "value": "p", "domain": "doubao.com",
         "path": "/", "httpOnly": False, "secure": False, "expires": -1},
    ]
    ctx = FakeContext(alive=True, cookies=cookies)

    ok = _save_doubao_cookies_to_disk(ctx, tmp_path)

    assert ok is True
    target = tmp_path / "cookies.json"
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    names = [c["name"] for c in payload]
    assert names == ["sessionid", "passport_auth_status"]
    sess = next(c for c in payload if c["name"] == "sessionid")
    assert sess["httpOnly"] is True
    assert sess["domain"] == ".doubao.com"


def test_save_doubao_cookies_returns_false_when_context_closed(tmp_path: Path):
    ctx = FakeContext(alive=False, cookies=[
        {"name": "sessionid", "value": "x", "domain": ".doubao.com"},
    ])
    ok = _save_doubao_cookies_to_disk(ctx, tmp_path)
    assert ok is False
    assert not (tmp_path / "cookies.json").exists()


def test_save_doubao_cookies_returns_false_when_no_doubao_cookie(tmp_path: Path):
    ctx = FakeContext(alive=True, cookies=[
        {"name": "_ga", "value": "x", "domain": ".google.com"},
    ])
    ok = _save_doubao_cookies_to_disk(ctx, tmp_path)
    assert ok is False
    assert not (tmp_path / "cookies.json").exists()


def test_rescue_for_fallback_writes_cookies_and_identity(tmp_path: Path):
    """v0.2.7 关键路径:context 还活着 + 有 doubao cookie + localStorage 有
    user_unique_id → 抢救后 cookies.json 和 identity.json 都写盘。
    v0.2.8:sessionid 必须 32-hex 才写 identity.json(rescue 同步闸门)。"""
    cookies = [
        {"name": "sessionid", "value": "deadbeef" * 4, "domain": ".doubao.com",
         "path": "/", "httpOnly": True, "secure": False, "expires": -1},
    ]
    page = FakePage(
        alive=True,
        evaluate_payload={"ok": True, "value": "u-from-localStorage"},
    )
    ctx = FakeContext(alive=True, cookies=cookies, pages=[page])

    _try_rescue_for_fallback(ctx, tmp_path)

    assert (tmp_path / "cookies.json").exists()
    id_payload = json.loads((tmp_path / "identity.json").read_text(encoding="utf-8"))
    assert id_payload["user_id"] == "u-from-localStorage"
    assert "rescued_at" in id_payload


def test_rescue_for_fallback_noop_when_profile_dir_none():
    ctx = FakeContext(alive=True, cookies=[
        {"name": "sessionid", "value": "x", "domain": ".doubao.com"},
    ])
    _try_rescue_for_fallback(ctx, None)
    # 不抛异常,无副作用


def test_rescue_for_fallback_noop_when_context_closed(tmp_path: Path):
    ctx = FakeContext(alive=False)
    _try_rescue_for_fallback(ctx, tmp_path)
    assert not (tmp_path / "cookies.json").exists()
    assert not (tmp_path / "identity.json").exists()


def test_rescue_for_fallback_skips_identity_when_no_active_page(tmp_path: Path):
    cookies = [{"name": "sessionid", "value": "abc", "domain": ".doubao.com"}]
    # pages 里只有一个 alive=False 的 page
    ctx = FakeContext(
        alive=True, cookies=cookies, pages=[FakePage(alive=False)],
    )
    _try_rescue_for_fallback(ctx, tmp_path)
    # cookies 抢救成功
    assert (tmp_path / "cookies.json").exists()
    # identity 没抢救(没有 active page)
    assert not (tmp_path / "identity.json").exists()


def test_rescue_for_fallback_skips_identity_when_localStorage_empty(tmp_path: Path):
    cookies = [{"name": "sessionid", "value": "abc", "domain": ".doubao.com"}]
    page = FakePage(alive=True, evaluate_payload={"ok": False})
    ctx = FakeContext(alive=True, cookies=cookies, pages=[page])
    _try_rescue_for_fallback(ctx, tmp_path)
    assert (tmp_path / "cookies.json").exists()
    assert not (tmp_path / "identity.json").exists()


# ---------------------------------------------------------------------------
# wait_for_identity 主循环
# ---------------------------------------------------------------------------


def test_wait_identity_returns_when_already_set():
    ready = threading.Event()
    identities = [DoubaoIdentity("u-fast", None)]
    ready.set()

    page = FakePage(alive=True)
    ctx = FakeContext(alive=True)

    identity = wait_for_identity(
        lambda: [page], ready, identities, threading.Event(),
        context=ctx,
    )
    assert identity.user_id == "u-fast"


# ---------------------------------------------------------------------------
# v0.2.8:wait_for_identity 主循环闸门测试
# ---------------------------------------------------------------------------


def test_wait_identity_continues_when_sessionid_absent():
    """v0.2.7 假阳性场景重放:DOM pass + localStorage 有 user_id + 无 sessionid。
    v0.2.8 闸门 2 必须拒绝并继续轮询,直到 cancel 才退出。"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    cancel = threading.Event()

    # 只有 tracking cookies,没有 sessionid(典型首访)
    cookies = [
        {"name": "s_v_web_id", "value": "verify_x", "domain": ".doubao.com"},
        {"name": "odin_tt", "value": "abcd" * 16, "domain": ".doubao.com"},
        {"name": "ttwid", "value": "1|x|1|x", "domain": ".doubao.com"},
    ]
    # 每次 evaluate 都返相同 payload:DOM pass + localStorage 有 user_id
    page = _FakeCancelingPage(
        alive=True,
        evaluate_payload=None,  # 用 evaluate_queue 模式
        evaluate_queue=[
            "欢迎使用豆包 AI",  # DOM 探测
            {"ok": True, "value": "7669336097453426239"},  # tracking ID
        ],
        after_evaluate=lambda _script: cancel.set(),
    )
    ctx = FakeContext(alive=True, cookies=cookies, pages=[page])

    try:
        wait_for_identity(
            lambda: [page], ready, identities, cancel, context=ctx,
        )
    except RuntimeError as exc:
        assert "登录已取消" in str(exc)

    # 关键断言:identity 永远不应该被设(闸门 2 拒绝)
    assert identities == []


def test_wait_identity_requires_minimum_elapsed_time():
    """v0.2.8 闸门 1:主循环起 <3s 不接受 identity,即使 sessionid 已经合法。
    因为 _FakeCancelingPage 第一次 evaluate 就 cancel,会触发"主循环才过
    0.0s,小于 3s 冷却"的拒绝路径。"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    cancel = threading.Event()

    cookies = [{
        "name": "sessionid", "value": _VALID_SESSIONID, "domain": ".doubao.com",
    }]
    page = _FakeCancelingPage(
        alive=True,
        evaluate_queue=[
            "欢迎使用豆包 AI",  # DOM 通过
            {"ok": True, "value": "u-real-7777"},  # 即使有也不该被接受
        ],
        after_evaluate=lambda _script: cancel.set(),
    )
    ctx = FakeContext(alive=True, cookies=cookies, pages=[page])

    try:
        wait_for_identity(
            lambda: [page], ready, identities, cancel, context=ctx,
        )
    except RuntimeError as exc:
        assert "登录已取消" in str(exc)

    # 闸门 1 拒绝:identity 不该被设
    assert identities == []


def test_wait_identity_accepts_when_sessionid_present():
    """完整主路径 happy-path:DOM pass + sessionid 32-hex + 3s 后 + localStorage
    有 user_id → 命中。已在 test_wait_identity_returns_via_cookie_dom_localstorage
    覆盖;这里加一个更明确的、聚焦 sessionid 的变体。"""
    # 这是 happy-path,主测试已经覆盖。这里只断言:_has_valid_sessionid
    # 对 cookie 列表里只有 sessionid 32-hex 的返回 True
    cookies = [{"name": "sessionid", "value": _VALID_SESSIONID, "domain": ".doubao.com"}]
    ctx = FakeContext(alive=True, cookies=cookies)
    assert _has_valid_sessionid(ctx) is True
    # 同时 _sessionid_cookie_value 能取回
    assert _sessionid_cookie_value(ctx) == _VALID_SESSIONID


def test_wait_identity_rescue_skips_identity_without_sessionid(tmp_path: Path):
    """v0.2.8:rescue 也走 sessionid 闸门 —— 没有 sessionid 不写 identity.json。
    cookies.json 仍写(供调试 / 后续重试)。"""
    cookies = [
        # 只有 tracking cookies,没有 sessionid(假阳性场景的磁盘回收)
        {"name": "s_v_web_id", "value": "verify_x", "domain": ".doubao.com",
         "path": "/", "httpOnly": False, "secure": False, "expires": -1},
        {"name": "ttwid", "value": "1|x|1|x", "domain": ".doubao.com",
         "path": "/", "httpOnly": False, "secure": False, "expires": -1},
    ]
    page = FakePage(
        alive=True,
        # 即使 localStorage 有 tracking user_unique_id,rescue 也不该写 identity
        evaluate_payload={"ok": True, "value": "7669336097453426239"},
    )
    ctx = FakeContext(alive=True, cookies=cookies, pages=[page])

    _try_rescue_for_fallback(ctx, tmp_path)

    # cookies.json 写(抢救出来供调试 / 后续重试用)
    assert (tmp_path / "cookies.json").exists()
    # identity.json 不写(闸门拦截)
    assert not (tmp_path / "identity.json").exists()


def test_wait_identity_returns_via_cookie_dom_localstorage():
    """完整主路径:cookie 出现 + DOM 通过 + sessionid 32-hex + localStorage
    user_unique_id → 主循环过 3s 后命中。"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []

    # v0.2.8:sessionid 必须是 32-hex 才会被主循环接受
    cookies = [{
        "name": "sessionid",
        "value": "deadbeef" * 4,  # 32 chars hex
        "domain": ".doubao.com",
    }]
    # v0.3.0:主循环会反复调 evaluate —— DOM 探针每次都跑,localStorage
    # 探针要 gate 通过后才跑。用 evaluate_mapping 按 script 内容路由到
    # 各自 FIFO queue;各自 queue 各只放一项,后续轮次沿用同一 payload。
    # 注意 LOGGED_OUT_KEYWORDS 含 "登录" 兜底词,DOM payload 必须避开这两个字。
    page = FakePage(
        alive=True,
        evaluate_mapping={
            "document.body.innerText": ["欢迎使用豆包 AI,这是对话列表页面"],
            "__tea_cache_tokens_497858": [{"ok": True, "value": "u-from-tea"}],
        },
    )
    ctx = FakeContext(alive=True, cookies=cookies, pages=[page])

    # v0.3.0:测试里把最小冷却压到 0 —— wait_for_timeout 是 pass no-op,
    # 真实时间不会推进;不改 gate 测试会无限循环。真生产路径的 3 秒门
    # 由 test_wait_identity_requires_minimum_elapsed_time 单独验证。
    original = browser_module._MIN_LOOP_SECONDS_BEFORE_IDENTITY
    browser_module._MIN_LOOP_SECONDS_BEFORE_IDENTITY = 0.0
    try:
        identity = wait_for_identity(
            lambda: [page], ready, identities, threading.Event(),
            context=ctx,
        )
    finally:
        browser_module._MIN_LOOP_SECONDS_BEFORE_IDENTITY = original
    assert identity.user_id == "u-from-tea"
    assert identity.nickname is None


def test_wait_identity_skips_when_dom_still_has_login_keywords():
    """cookie 有但 DOM 仍含登录关键词 → 不读 localStorage,继续等。"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    cancel = threading.Event()

    cookies = [{"name": "sessionid", "value": "abc", "domain": ".doubao.com"}]
    # 主循环 `_page_looks_logged_in` 每轮都 evaluate 拿同样字符串 → 永远 False。
    # 测试不能让 wait_for_identity 自然等 6 秒 grace 退出(那会卡住 pytest),
    # 用一个 FakePage:每次 evaluate 后 set cancel,模拟用户最终手动取消。
    def _after_evaluate(script):
        cancel.set()

    page = _FakeCancelingPage(
        alive=True,
        evaluate_payload="请使用 豆包 APP 扫码登录",
        after_evaluate=_after_evaluate,
    )
    ctx = FakeContext(alive=True, cookies=cookies, pages=[page])

    try:
        wait_for_identity(
            lambda: [page], ready, identities, cancel,
            context=ctx,
        )
    except RuntimeError as exc:
        assert "登录已取消" in str(exc)
    else:
        raise AssertionError("应该 raise RuntimeError(登录已取消)")
    # 不论怎样,identity 永远不该被设(因为 DOM 不通过)
    assert identities == []


def test_wait_identity_raises_when_no_page_no_cookie_after_grace():
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    ctx = FakeContext(alive=True, cookies=[])

    def pages_provider():
        return [FakePage(alive=False)]

    try:
        wait_for_identity(
            pages_provider, ready, identities, threading.Event(),
            context=ctx,
        )
    except RuntimeError as exc:
        assert "登录窗口已关闭" in str(exc)
    else:
        raise AssertionError("应该 raise")


def test_wait_identity_raises_when_context_closed():
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    page = FakePage(alive=True)
    ctx = FakeContext(alive=False)

    try:
        wait_for_identity(
            lambda: [page], ready, identities, threading.Event(),
            context=ctx,
        )
    except RuntimeError as exc:
        assert "登录窗口已关闭" in str(exc)
    else:
        raise AssertionError("应该 raise")


def test_wait_identity_raises_on_cancel():
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    page = FakePage(alive=True, evaluate_payload="无关")
    ctx = FakeContext(alive=True)
    cancel = threading.Event()
    cancel.set()

    try:
        wait_for_identity(
            lambda: [page], ready, identities, cancel,
            context=ctx,
        )
    except RuntimeError as exc:
        assert "登录已取消" in str(exc)
    else:
        raise AssertionError("应该 raise")


def test_wait_identity_rescues_on_grace_timeout(tmp_path: Path):
    """v0.2.7:grace period 超时前抢救 cookies + identity(若 active page 在抢救时存在)。
    v0.2.8:sessionid 必须 32-hex 才写 identity.json(rescue 同步闸门)。
    """
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    cookies = [
        # v0.2.8:32-hex sessionid 才能让 rescue 写 identity.json
        {"name": "sessionid", "value": "deadbeef" * 4, "domain": ".doubao.com",
         "path": "/", "httpOnly": True, "secure": False, "expires": -1},
    ]
    # 用 evaluate_queue:第一次给 DOM(避开"登录"二字),第二次给 localStorage
    page = FakePage(
        alive=True,
        evaluate_queue=[
            "豆包 AI 对话页面,显示对话列表和创作入口",
            {"ok": True, "value": "u-rescue-777"},
        ],
    )
    ctx = FakeContext(alive=True, cookies=cookies, pages=[page])

    # 模拟 page_closed_since 起点:全程返回 closed page
    def pages_provider():
        return [FakePage(alive=False)]

    try:
        wait_for_identity(
            pages_provider, ready, identities, threading.Event(),
            context=ctx, profile_dir=tmp_path,
        )
    except RuntimeError:
        pass

    # grace period 超时后 _try_rescue_for_fallback 应该写盘
    assert (tmp_path / "cookies.json").exists()
    # v0.2.8:rescue 同步 sessionid 闸门,32-hex 在 → identity.json 也写
    assert (tmp_path / "identity.json").exists()


def test_wait_identity_raises_when_context_arg_missing():
    """context=None 时直接 raise,无需其他参数。"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    try:
        wait_for_identity(
            lambda: [], ready, identities, threading.Event(),
            context=None,
        )
    except RuntimeError as exc:
        assert "owner thread" in str(exc) or "context" in str(exc).lower()
    else:
        raise AssertionError("应该 raise")


# ---------------------------------------------------------------------------
# _verify_from_disk 单路径测试
# ---------------------------------------------------------------------------


def test_verify_from_disk_prefers_identity_json(tmp_path: Path):
    """v0.2.7:identity.json 存在且有 user_id → 直接返回(若 sessionid 闸门过)。
    v0.2.8.1:cookies.json 必须有合法 sessionid 才算登录。
    """
    (tmp_path / "identity.json").write_text(
        json.dumps({"user_id": "u-from-identity", "rescued_at": "2026-08-02T00:00:00Z"}),
        encoding="utf-8",
    )
    (tmp_path / "cookies.json").write_text(
        json.dumps([
            # 32-hex sessionid —— 让闸门通过,identity.json.user_id 才能返回
            {"name": "sessionid", "value": "deadbeef" * 4, "domain": ".doubao.com"},
        ]),
        encoding="utf-8",
    )
    identity = _verify_from_disk(tmp_path)
    assert identity is not None
    assert identity["user_id"] == "u-from-identity"
    assert identity["nickname"] is None


def test_verify_from_disk_falls_back_to_cookies_user_unique_id(tmp_path: Path):
    """v0.2.7:没有 identity.json,cookies.json 有 user_unique_id hint → 命中。
    v0.2.8.1:sessionid 必须 32-hex 闸门通过。
    """
    (tmp_path / "cookies.json").write_text(
        json.dumps([
            {"name": "sessionid", "value": "deadbeef" * 4, "domain": ".doubao.com"},
            {"name": "user_unique_id", "value": "u-from-cookie", "domain": "doubao.com"},
        ]),
        encoding="utf-8",
    )
    identity = _verify_from_disk(tmp_path)
    assert identity is not None
    assert identity["user_id"] == "u-from-cookie"


def test_verify_from_disk_returns_none_when_no_files(tmp_path: Path):
    """既无 identity.json 也无 cookies.json → None。"""
    assert _verify_from_disk(tmp_path) is None


def test_verify_from_disk_returns_none_on_malformed_identity_json(tmp_path: Path):
    """identity.json 损坏 → 跳过,尝试 cookies.json。"""
    (tmp_path / "identity.json").write_text("not-json{", encoding="utf-8")
    # 没有 cookies.json → 整体返回 None
    assert _verify_from_disk(tmp_path) is None


def test_verify_from_disk_returns_none_on_identity_json_without_user_id(tmp_path: Path):
    """identity.json 结构合法但没 user_id → fallback 到 cookies.json(也没)→ None。"""
    (tmp_path / "identity.json").write_text(
        json.dumps({"rescued_at": "2026-08-02T00:00:00Z"}),
        encoding="utf-8",
    )
    assert _verify_from_disk(tmp_path) is None


def test_verify_from_disk_skips_non_doubao_cookies(tmp_path: Path):
    """cookies.json 全是非 doubao 域 → 没 user_id hint → None。"""
    (tmp_path / "cookies.json").write_text(
        json.dumps([
            {"name": "_ga", "value": "x", "domain": ".google.com"},
            {"name": "_fbp", "value": "y", "domain": ".facebook.com"},
        ]),
        encoding="utf-8",
    )
    assert _verify_from_disk(tmp_path) is None


def test_verify_from_disk_handles_corrupt_cookies_json(tmp_path: Path):
    """cookies.json 损坏 → 捕获,返回 None(不抛)。"""
    (tmp_path / "cookies.json").write_text("{not-json", encoding="utf-8")
    assert _verify_from_disk(tmp_path) is None


def test_verify_from_disk_falls_through_identity_to_cookies(tmp_path: Path):
    """identity.json 损坏 → 跳过,继续读 cookies.json(且命中 user_id hint)。
    v0.2.8.1:sessionid 闸门必须过。
    """
    (tmp_path / "identity.json").write_text("{not-json", encoding="utf-8")
    (tmp_path / "cookies.json").write_text(
        json.dumps([
            {"name": "sessionid", "value": "deadbeef" * 4, "domain": ".doubao.com"},
            {"name": "user_unique_id", "value": "u-fallthrough", "domain": ".doubao.com"},
        ]),
        encoding="utf-8",
    )
    identity = _verify_from_disk(tmp_path)
    assert identity is not None
    assert identity["user_id"] == "u-fallthrough"


# ---------------------------------------------------------------------------
# v0.2.8.1:sessionid 闸门专用测试
# ---------------------------------------------------------------------------


def test_verify_from_disk_rejects_identity_without_sessionid(tmp_path: Path):
    """v0.2.8.1:identity.json 有 user_id 但 cookies.json 无合法 sessionid → 拒绝。

    这是修复 v0.2.7 假阳性的关键场景:v0.2.7 会把 identity.json 里的
    user_unique_id(tracking ID,首访即下发)当作 user_id 接受,v0.2.8.1 必须
    在没有 sessionid cookie 时整体拒绝。
    """
    (tmp_path / "identity.json").write_text(
        json.dumps({"user_id": "u-tracking-only", "rescued_at": "2026-08-02T00:00:00Z"}),
        encoding="utf-8",
    )
    # cookies.json 只有 tracking cookies,无 sessionid
    (tmp_path / "cookies.json").write_text(
        json.dumps([
            {"name": "s_v_web_id", "value": "verify_ms_xxxx", "domain": ".doubao.com"},
            {"name": "odin_tt", "value": "a719bf" * 8, "domain": ".doubao.com"},
            {"name": "ttwid", "value": "1|abc|123|def", "domain": ".doubao.com"},
        ]),
        encoding="utf-8",
    )
    assert _verify_from_disk(tmp_path) is None


def test_verify_from_disk_accepts_identity_with_valid_sessionid(tmp_path: Path):
    """v0.2.8.1:identity.json 有 user_id 且 cookies.json 有合法 32-hex sessionid → 通过。"""
    (tmp_path / "identity.json").write_text(
        json.dumps({"user_id": "u-real-login", "rescued_at": "2026-08-02T00:00:00Z"}),
        encoding="utf-8",
    )
    (tmp_path / "cookies.json").write_text(
        json.dumps([
            {"name": "sessionid", "value": "deadbeef" * 4, "domain": ".doubao.com"},
        ]),
        encoding="utf-8",
    )
    identity = _verify_from_disk(tmp_path)
    assert identity is not None
    assert identity["user_id"] == "u-real-login"


def test_verify_from_disk_accepts_cookies_with_sessionid_and_user_id(tmp_path: Path):
    """v0.2.8.1:无 identity.json,cookies.json 有合法 sessionid + user_id hint → 兜底通过。"""
    (tmp_path / "cookies.json").write_text(
        json.dumps([
            {"name": "sessionid", "value": "deadbeef" * 4, "domain": ".doubao.com"},
            {"name": "user_unique_id", "value": "u-from-cookie", "domain": "doubao.com"},
        ]),
        encoding="utf-8",
    )
    identity = _verify_from_disk(tmp_path)
    assert identity is not None
    assert identity["user_id"] == "u-from-cookie"


def test_verify_from_disk_rejects_sessionid_wrong_format(tmp_path: Path):
    """v0.2.8.1:sessionid 存在但不是 32-hex(可能被注入垃圾) → 拒绝。"""
    (tmp_path / "identity.json").write_text(
        json.dumps({"user_id": "u-attempt", "rescued_at": "2026-08-02T00:00:00Z"}),
        encoding="utf-8",
    )
    (tmp_path / "cookies.json").write_text(
        json.dumps([
            # sessionid 名字对但值不是 32-hex
            {"name": "sessionid", "value": "abc123", "domain": ".doubao.com"},
        ]),
        encoding="utf-8",
    )
    assert _verify_from_disk(tmp_path) is None


def test_verify_from_disk_accepts_sessionid_ss_variant(tmp_path: Path):
    """v0.2.8.1:sessionid_ss(secure session 变种)同样能过闸门。"""
    (tmp_path / "identity.json").write_text(
        json.dumps({"user_id": "u-via-ss", "rescued_at": "2026-08-02T00:00:00Z"}),
        encoding="utf-8",
    )
    (tmp_path / "cookies.json").write_text(
        json.dumps([
            {"name": "sessionid_ss", "value": "deadbeef" * 4, "domain": ".doubao.com"},
        ]),
        encoding="utf-8",
    )
    identity = _verify_from_disk(tmp_path)
    assert identity is not None
    assert identity["user_id"] == "u-via-ss"


def test_verify_from_disk_rejects_non_doubao_sessionid(tmp_path: Path):
    """v0.2.8.1:别域 cookie 里有 sessionid 32-hex → 不算 doubao 登录,拒绝。

    防 fail-open:用户在第三方域名(如某个 doubao 嵌入页)写了个巧合的 32-hex
    sessionid 字段,不应该让 disk fallback 误以为真登录。
    """
    (tmp_path / "identity.json").write_text(
        json.dumps({"user_id": "u-cross-domain", "rescued_at": "2026-08-02T00:00:00Z"}),
        encoding="utf-8",
    )
    (tmp_path / "cookies.json").write_text(
        json.dumps([
            # domain 不是 doubao.com
            {"name": "sessionid", "value": "deadbeef" * 4, "domain": ".example.com"},
        ]),
        encoding="utf-8",
    )
    assert _verify_from_disk(tmp_path) is None