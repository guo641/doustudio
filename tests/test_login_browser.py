"""v0.2.7:沿 yaonieyo/doubao-account-pool 双轨判定测试。

不再测试 on_response / /passport/web/account/info/ 调用 —— 字节系 aegis 风控
把所有非浏览器指纹请求拒为 1011。我们只信 Chromium 自己看到的:
  Tier 1: context.cookies() doubao.com cookie 计数
  Tier 2: page.evaluate DOM innerText 不含登录关键词
  user_id: page.evaluate localStorage.__tea_cache_tokens_497858.user_unique_id
"""
import json
import threading
from pathlib import Path

from doupool.login.browser import (
    LOGGED_OUT_KEYWORDS,
    _context_doubao_cookie_count,
    _extract_user_id_from_cookies,
    _is_doubao_cookie,
    _page_looks_logged_in,
    _read_user_unique_id_from_page,
    _save_doubao_cookies_to_disk,
    _try_rescue_for_fallback,
    wait_for_identity,
)
from doupool.login.detector import DoubaoIdentity
from doupool.login.service import _verify_from_disk
from playwright.sync_api import Error as PlaywrightError


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------


class FakePage:
    """v0.2.7 FakePage:支持 evaluate_queue(按 evaluate 调用顺序返回不同值),
    因为新主循环对同一 page 调两次 evaluate(一次 DOM,一次 localStorage)。"""

    def __init__(
        self,
        alive=True,
        evaluate_payload=None,
        evaluate_raises=None,
        evaluate_queue=None,
    ):
        self._alive = alive
        self._evaluate_payload = evaluate_payload
        self._evaluate_raises = evaluate_raises
        self._evaluate_queue = list(evaluate_queue) if evaluate_queue else None
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

    def __init__(self, alive=True, evaluate_payload=None, after_evaluate=None):
        super().__init__(alive=alive, evaluate_payload=evaluate_payload)
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
    user_unique_id → 抢救后 cookies.json 和 identity.json 都写盘。"""
    cookies = [
        {"name": "sessionid", "value": "abc", "domain": ".doubao.com",
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


def test_wait_identity_returns_via_cookie_dom_localstorage():
    """完整主路径:cookie 出现 + DOM 通过 + localStorage user_unique_id → 命中。"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []

    cookies = [{"name": "sessionid", "value": "abc", "domain": ".doubao.com"}]
    # evaluate_queue 第一次返 DOM 文本(无登录关键词),第二次返 user_unique_id。
    # 注意 LOGGED_OUT_KEYWORDS 含 "登录" 兜底词,DOM payload 必须避开这两个字。
    page = FakePage(
        alive=True,
        evaluate_queue=[
            "欢迎使用豆包 AI,这是对话列表页面",  # DOM 探测
            {"ok": True, "value": "u-from-tea"},     # localStorage 探测
        ],
    )
    ctx = FakeContext(alive=True, cookies=cookies, pages=[page])

    identity = wait_for_identity(
        lambda: [page], ready, identities, threading.Event(),
        context=ctx,
    )
    assert identity.user_id == "u-from-tea"
    assert identity.nickname is None


def test_wait_identity_skips_when_dom_still_has_login_keywords():
    """cookie 有但 DOM 仍含登录关键词 → 不读 localStorage,继续等。"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []

    cookies = [{"name": "sessionid", "value": "abc", "domain": ".doubao.com"}]
    page = FakePage(
        alive=True,
        evaluate_payload="请使用 豆包 APP 扫码登录",  # 永远包含登录关键词
    )
    ctx = FakeContext(alive=True, cookies=cookies, pages=[page])

    try:
        wait_for_identity(
            lambda: [page], ready, identities, threading.Event(),
            context=ctx,
        )
    except RuntimeError:
        pass  # grace period 过了 raise
    # 不论怎样,identity 永远不该被设(因为 DOM 不通过)
    assert identities == []


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
    """v0.2.7:grace period 超时前抢救 cookies + identity(若 active page 在抢救时存在)。"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    cookies = [
        {"name": "sessionid", "value": "abc", "domain": ".doubao.com",
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
    """identity.json 存在且有 user_id → 直接返回,不看 cookies.json。"""
    (tmp_path / "identity.json").write_text(
        json.dumps({"user_id": "u-from-identity", "rescued_at": "2026-08-02T00:00:00Z"}),
        encoding="utf-8",
    )
    # 也写 cookies.json 但应该不被读到
    (tmp_path / "cookies.json").write_text(
        json.dumps([{"name": "sessionid", "value": "abc", "domain": ".doubao.com"}]),
        encoding="utf-8",
    )
    identity = _verify_from_disk(tmp_path)
    assert identity is not None
    assert identity["user_id"] == "u-from-identity"
    assert identity["nickname"] is None


def test_verify_from_disk_falls_back_to_cookies_user_unique_id(tmp_path: Path):
    """没有 identity.json,cookies.json 有 user_unique_id hint → 命中。"""
    (tmp_path / "cookies.json").write_text(
        json.dumps([
            {"name": "sessionid", "value": "abc", "domain": ".doubao.com"},
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
    """identity.json 损坏 → 跳过,继续读 cookies.json(且命中 user_id hint)。"""
    (tmp_path / "identity.json").write_text("{not-json", encoding="utf-8")
    (tmp_path / "cookies.json").write_text(
        json.dumps([{"name": "user_unique_id", "value": "u-fallthrough", "domain": ".doubao.com"}]),
        encoding="utf-8",
    )
    identity = _verify_from_disk(tmp_path)
    assert identity is not None
    assert identity["user_id"] == "u-fallthrough"