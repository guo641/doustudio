import json
import threading
import time
from pathlib import Path

from doupool.login.browser import (
    _context_has_doubao_cookie,
    _is_doubao_cookie,
    _safe_page_verify,
    _safe_request_verify,
    _save_account_info_to_disk,
    _save_doubao_cookies_to_disk,
    _try_one_verify,
    _try_rescue_cookies,
    wait_for_identity,
)
from doupool.login.detector import DoubaoIdentity, DoubaoLoginDetector
from doupool.login.service import _verify_from_disk
from playwright.sync_api import Error as PlaywrightError


# ---------------------------------------------------------------------------
# 假对象
# ---------------------------------------------------------------------------


class FakePage:
    """page 假对象,可控制 is_closed / evaluate / url"""

    def __init__(self, alive=True, evaluate_payload=None, evaluate_raises=None):
        self._alive = alive
        self._evaluate_payload = evaluate_payload
        self._evaluate_raises = evaluate_raises
        self.url = "https://www.doubao.com/"

    def is_closed(self):
        if self._evaluate_raises and "is_closed" in self._evaluate_raises:
            raise self._evaluate_raises["is_closed"]
        return not self._alive

    def evaluate(self, _script):
        if self._evaluate_raises and "evaluate" in self._evaluate_raises:
            raise self._evaluate_raises["evaluate"]
        return self._evaluate_payload

    def wait_for_timeout(self, ms):
        # 测试里不要真 sleep;只是让 pump 一次
        pass


class FakeContext:
    def __init__(self, alive=True, cookies=None, request_identity=None, pages=None):
        self._alive = alive
        self._cookies = cookies or []
        self._request_identity = request_identity
        self._request_calls = 0
        # 默认有一个 alive page,让 _save_account_info_to_disk 走通。
        # 测试里想测"无 active page"路径就显式传 pages=[] 或 pages=[FakePage(alive=False)]
        self._pages = pages if pages is not None else [FakePage(alive=True)]

    def is_closed(self):
        return not self._alive

    @property
    def pages(self):
        """Playwright BrowserContext.pages —— 返回当前打开的 page 列表(快照)。
        _save_account_info_to_disk 用 hasattr(context, "pages") 判断后,
        再 [_page_is_alive(p) for p in context.pages] 过滤。"""
        return list(self._pages)

    def cookies(self):
        if not self._alive:
            raise PlaywrightError("Target page, context or browser has been closed")
        return list(self._cookies)

    @property
    def request(self):
        """detector.verify() 期望 .request.get(url),所以是属性 + 可调对象。"""
        outer = self

        class _Req:
            def get(self, url):
                outer._request_calls += 1
                if not outer._alive:
                    raise PlaywrightError("Request context disposed")
                if outer._request_identity is None:
                    raise PlaywrightError("account/info code=-1")
                from doupool.login.detector import ACCOUNT_INFO_URL
                assert url == ACCOUNT_INFO_URL
                return _FakeResponse(outer._request_identity)

        return _Req()

    def wait_for_event(self, event_name, timeout=None):
        """主循环在没有 active page 时用 context.wait_for_event('page', timeout) pump 事件。
        测试里不需要真等,只是模拟一次空转,直接返回 None。"""
        return None


class _FakeResponse:
    def __init__(self, identity):
        self.identity = identity
        self.ok = True
        self.status = 200

    def json(self):
        return {
            "code": 0,
            "data": {
                "user": {
                    "user_id": self.identity.user_id,
                    "name": self.identity.nickname,
                }
            },
        }


# ---------------------------------------------------------------------------
# 工具函数测试
# ---------------------------------------------------------------------------


def test_is_doubao_cookie_matches_doubao_domain():
    assert _is_doubao_cookie({"domain": ".doubao.com", "name": "x"}) is True
    assert _is_doubao_cookie({"domain": "doubao.com", "name": "x"}) is True
    assert _is_doubao_cookie({"domain": ".google.com", "name": "x"}) is False
    assert _is_doubao_cookie({"domain": None, "name": "x"}) is False


def test_context_has_doubao_cookie_detects_login_state():
    ctx = FakeContext(cookies=[
        {"name": "sessionid", "domain": ".doubao.com", "value": "x"},
    ])
    assert _context_has_doubao_cookie(ctx) is True


def test_context_has_doubao_cookie_false_when_only_third_party():
    ctx = FakeContext(cookies=[
        {"name": "_ga", "domain": ".google.com", "value": "x"},
    ])
    assert _context_has_doubao_cookie(ctx) is False


def test_context_has_doubao_cookie_false_when_context_closed():
    ctx = FakeContext(alive=False, cookies=[])
    assert _context_has_doubao_cookie(ctx) is False  # 不抛异常


def test_safe_request_verify_returns_identity_when_context_alive():
    identity = DoubaoIdentity("u-req", "request-命中")
    ctx = FakeContext(request_identity=identity)
    detector = DoubaoLoginDetector()

    got = _safe_request_verify(detector, ctx)

    assert got is not None
    assert got.user_id == "u-req"
    assert ctx._request_calls == 1


def test_safe_request_verify_returns_none_when_context_closed():
    ctx = FakeContext(alive=False, request_identity=DoubaoIdentity("x", "y"))
    detector = DoubaoLoginDetector()

    assert _safe_request_verify(detector, ctx) is None
    assert ctx._request_calls == 0  # 早退,不调 request


def test_safe_page_verify_returns_identity_from_payload():
    identity_payload = {
        "code": 0,
        "data": {"user": {"user_id": "u-page", "name": "page-命中"}},
    }
    page = FakePage(evaluate_payload=identity_payload)
    detector = DoubaoLoginDetector()

    got = _safe_page_verify(detector, page)

    assert got is not None
    assert got.user_id == "u-page"
    assert got.nickname == "page-命中"


def test_safe_page_verify_returns_none_on_navigation_destroyed():
    page = FakePage(evaluate_raises={"evaluate": PlaywrightError("Execution context destroyed")})
    detector = DoubaoLoginDetector()

    assert _safe_page_verify(detector, page) is None


def test_safe_page_verify_returns_none_when_page_closed():
    page = FakePage(alive=False, evaluate_payload={"code": 0, "data": {}})
    detector = DoubaoLoginDetector()

    assert _safe_page_verify(detector, page) is None


def test_safe_page_verify_returns_none_on_fetch_error_payload():
    page = FakePage(evaluate_payload={"__err": "Failed to fetch"})
    detector = DoubaoLoginDetector()

    assert _safe_page_verify(detector, page) is None


def test_try_one_verify_prefers_page_evaluate():
    """page.evaluate 命中 → 不调 context.request,只返回 page 路径的 identity"""
    page_payload = {
        "code": 0,
        "data": {"user": {"user_id": "u-page", "name": "page"}},
    }
    page = FakePage(evaluate_payload=page_payload)
    ctx = FakeContext(
        alive=True,
        cookies=[{"name": "sessionid", "domain": ".doubao.com"}],
        request_identity=DoubaoIdentity("u-req", "req"),  # 不应被调
    )
    detector = DoubaoLoginDetector()

    identity = _try_one_verify(detector, [page], ctx)

    assert identity is not None
    assert identity.user_id == "u-page"
    assert ctx._request_calls == 0


def test_try_one_verify_falls_back_to_context_request():
    """page.evaluate 失败 + cookie 已写入 → fallback 到 context.request"""
    page = FakePage(evaluate_raises={"evaluate": PlaywrightError("Target closed")})
    ctx = FakeContext(
        alive=True,
        cookies=[{"name": "sessionid", "domain": ".doubao.com"}],
        request_identity=DoubaoIdentity("u-req", "req-fallback"),
    )
    detector = DoubaoLoginDetector()

    identity = _try_one_verify(detector, [page], ctx)

    assert identity is not None
    assert identity.user_id == "u-req"


def test_try_one_verify_returns_none_when_no_cookie_no_identity():
    page = FakePage(evaluate_payload={"__err": "fetch failed"})
    ctx = FakeContext(alive=True, cookies=[], request_identity=None)
    detector = DoubaoLoginDetector()

    assert _try_one_verify(detector, [page], ctx) is None


# ---------------------------------------------------------------------------
# wait_for_identity 行为测试
# ---------------------------------------------------------------------------


def test_wait_identity_returns_immediately_when_on_response_already_set():
    ready = threading.Event()
    identities = [DoubaoIdentity("u-fast", "on-response 先到")]
    ready.set()

    page = FakePage(alive=True)
    ctx = FakeContext(alive=True)

    identity = wait_for_identity(
        lambda: [page], ready, identities, threading.Event(),
        detector=DoubaoLoginDetector(), context=ctx,
    )

    assert identity.user_id == "u-fast"


def test_wait_identity_returns_via_page_evaluate():
    """正常路径:active page + evaluate 命中 → 返回 identity"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    identity_payload = {
        "code": 0,
        "data": {"user": {"user_id": "u-page-eval", "name": "page-eval"}},
    }
    page = FakePage(alive=True, evaluate_payload=identity_payload)
    ctx = FakeContext(alive=True)

    identity = wait_for_identity(
        lambda: [page], ready, identities, threading.Event(),
        detector=DoubaoLoginDetector(), context=ctx,
    )

    assert identity.user_id == "u-page-eval"


def test_wait_identity_returns_via_context_request_after_page_closed():
    """原 page 已关 + cookie 已写入 + context 还在 → grace period 内 verify 命中"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []

    class _Ctx(FakeContext):
        def cookies(self):
            return [{"name": "sessionid", "domain": ".doubao.com"}]

        @property
        def request(self):
            outer = self

            class Req:
                def get(self, url):
                    outer._request_calls += 1
                    return _FakeResponse(DoubaoIdentity("u-grace", "grace"))

            return Req()

    ctx = _Ctx(alive=True, request_identity=DoubaoIdentity("u-grace", "grace"))

    def pages_provider():
        # 第一轮返回 closed page(模拟 navigation 已关),后续保持这样
        return [FakePage(alive=False)]

    identity = wait_for_identity(
        pages_provider, ready, identities, threading.Event(),
        detector=DoubaoLoginDetector(), context=ctx,
    )

    assert identity.user_id == "u-grace"


def test_wait_identity_raises_when_no_page_no_cookie_after_grace():
    """无 active page + 没 cookie + grace 过了 → raise"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    ctx = FakeContext(alive=True, cookies=[])

    def pages_provider():
        return [FakePage(alive=False)]

    try:
        wait_for_identity(
            pages_provider, ready, identities, threading.Event(),
            detector=DoubaoLoginDetector(), context=ctx,
        )
    except RuntimeError as exc:
        assert "登录窗口已关闭" in str(exc)
    else:
        raise AssertionError("应该 raise RuntimeError")


def test_wait_identity_raises_when_context_closed():
    """context 直接关掉 → raise"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    page = FakePage(alive=True, evaluate_payload={"code": 0, "data": {}})
    ctx = FakeContext(alive=False)

    try:
        wait_for_identity(
            lambda: [page], ready, identities, threading.Event(),
            detector=DoubaoLoginDetector(), context=ctx,
        )
    except RuntimeError as exc:
        assert "登录窗口已关闭" in str(exc)
    else:
        raise AssertionError("应该 raise RuntimeError")


def test_wait_identity_resets_grace_when_new_page_appears():
    """原 page 关掉后,新 page 出现 → grace 重置,不立即 raise"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    ctx = FakeContext(alive=True, cookies=[
        {"name": "sessionid", "domain": ".doubao.com"},
    ])

    # 用一个状态机模拟:前几轮返回 closed page,后几轮返回 alive page
    state = {"round": 0, "identity_returned_at": None}

    def pages_provider():
        state["round"] += 1
        if state["round"] <= 2:
            return [FakePage(alive=False)]
        # 第 3 轮起:新 page 上线 + evaluate 命中
        if state["identity_returned_at"] is None:
            state["identity_returned_at"] = state["round"]
        return [FakePage(
            alive=True,
            evaluate_payload={
                "code": 0,
                "data": {"user": {"user_id": "u-newpage", "name": "新 page 命中"}},
            },
        )]

    identity = wait_for_identity(
        pages_provider, ready, identities, threading.Event(),
        detector=DoubaoLoginDetector(), context=ctx,
    )

    assert identity.user_id == "u-newpage"
    # 关键:前两轮没 raise,而是等到了第 3 轮新 page
    assert state["identity_returned_at"] is not None
    assert state["identity_returned_at"] >= 3


def test_wait_identity_raises_on_cancel():
    """cancel_event.set() → raise 登录已取消"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    page = FakePage(alive=True, evaluate_payload={"__err": "fetch"})
    ctx = FakeContext(alive=True)
    cancel = threading.Event()
    cancel.set()  # 立即 cancel

    try:
        wait_for_identity(
            lambda: [page], ready, identities, cancel,
            detector=DoubaoLoginDetector(), context=ctx,
        )
    except RuntimeError as exc:
        assert "登录已取消" in str(exc) or "登录窗口已关闭" in str(exc)
    else:
        raise AssertionError("应该 raise")


# ---------------------------------------------------------------------------
# v0.2.5 cookie 抢救 + disk fallback 测试
# ---------------------------------------------------------------------------


def test_save_doubao_cookies_writes_only_doubao_domain(tmp_path: Path):
    """_save_doubao_cookies_to_disk 只写 doubao.com 域 cookie,且不写空 value"""
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
    # google 域被过滤,空 value 的 uid_tt 被过滤
    assert names == ["sessionid", "passport_auth_status"]
    # 保留关键字段
    sess = next(c for c in payload if c["name"] == "sessionid")
    assert sess["httpOnly"] is True
    assert sess["domain"] == ".doubao.com"


def test_save_doubao_cookies_returns_false_when_context_closed(tmp_path: Path):
    """context 已死 → cookies() 抛 PlaywrightError → 返回 False,不写盘"""
    ctx = FakeContext(alive=False, cookies=[
        {"name": "sessionid", "value": "x", "domain": ".doubao.com"},
    ])

    ok = _save_doubao_cookies_to_disk(ctx, tmp_path)

    assert ok is False
    assert not (tmp_path / "cookies.json").exists()


def test_save_doubao_cookies_returns_false_when_no_doubao_cookie(tmp_path: Path):
    """context 还活着但没有 doubao 域 cookie → 返回 False"""
    ctx = FakeContext(alive=True, cookies=[
        {"name": "_ga", "value": "x", "domain": ".google.com"},
    ])

    ok = _save_doubao_cookies_to_disk(ctx, tmp_path)

    assert ok is False
    assert not (tmp_path / "cookies.json").exists()


def test_try_rescue_cookies_is_noop_when_profile_dir_none():
    """profile_dir=None 时 _try_rescue_cookies 直接早退,不动 context"""
    ctx = FakeContext(alive=True, cookies=[
        {"name": "sessionid", "value": "x", "domain": ".doubao.com"},
    ])

    _try_rescue_cookies(ctx, None)  # 不抛异常,也不写盘

    assert ctx._request_calls == 0  # 没动 request


def test_wait_identity_rescues_cookies_before_raising_when_context_closed(tmp_path: Path):
    """v0.2.5 关键路径:context 同毫秒 dispose → raise 之前抢救一次 cookies"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    page = FakePage(alive=True)
    # context 已死 + cookies() 会抛 PlaywrightError(FakeContext 默认 alive=False 时抛)
    ctx = FakeContext(alive=False)

    try:
        wait_for_identity(
            lambda: [page], ready, identities, threading.Event(),
            detector=DoubaoLoginDetector(), context=ctx,
            profile_dir=tmp_path,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("应该 raise")

    # cookies.json 不应被写入(context 已死,抢救必然失败)
    assert not (tmp_path / "cookies.json").exists()


def test_wait_identity_rescues_cookies_on_grace_timeout(tmp_path: Path):
    """grace period 超时前抢救一次 cookies(此时 context 还活着)"""
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    cookies = [
        {"name": "sessionid", "value": "abc", "domain": ".doubao.com",
         "path": "/", "httpOnly": True, "secure": False, "expires": -1},
    ]
    ctx = FakeContext(alive=True, cookies=cookies)

    try:
        wait_for_identity(
            lambda: [FakePage(alive=False)], ready, identities,
            threading.Event(),
            detector=DoubaoLoginDetector(), context=ctx,
            profile_dir=tmp_path,
        )
    except RuntimeError:
        pass
    else:
        raise AssertionError("应该 raise")

    # grace period 超时后抢救应成功
    target = tmp_path / "cookies.json"
    assert target.exists()
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert any(c["name"] == "sessionid" for c in payload)


# ---------------------------------------------------------------------------
# _verify_from_disk 测试(httpx fallback)
# ---------------------------------------------------------------------------


def test_verify_from_disk_returns_none_when_no_cookies_file(tmp_path: Path):
    """cookies.json 不存在 → 返回 None(不启动 Chromium)"""
    import doupool.login.service as svc_mod
    original = svc_mod._verify_via_persistent_context
    svc_mod._verify_via_persistent_context = lambda *a, **kw: None
    try:
        assert _verify_from_disk(tmp_path) is None
    finally:
        svc_mod._verify_via_persistent_context = original


def test_verify_from_disk_returns_none_on_malformed_json(tmp_path: Path):
    """cookies.json 损坏 → 返回 None,不抛异常"""
    (tmp_path / "cookies.json").write_text("not-json{", encoding="utf-8")
    import doupool.login.service as svc_mod
    original = svc_mod._verify_via_persistent_context
    svc_mod._verify_via_persistent_context = lambda *a, **kw: None
    try:
        assert _verify_from_disk(tmp_path) is None
    finally:
        svc_mod._verify_via_persistent_context = original


def test_verify_from_disk_httpx_call(monkeypatch, tmp_path: Path):
    """cookies.json + httpx mock 调通 → 返回 identity mapping"""
    cookies = [
        {"name": "sessionid", "value": "abc", "domain": ".doubao.com"},
        {"name": "uid_tt", "value": "u-xyz", "domain": ".doubao.com"},
    ]
    (tmp_path / "cookies.json").write_text(
        json.dumps(cookies), encoding="utf-8"
    )

    class _FakeResp:
        status_code = 200

        def json(self):
            return {
                "code": 0,
                "data": {"user": {"user_id": "99999", "name": "disk-命中"}},
            }

    captured: dict[str, str] = {}

    def fake_get(url, headers=None, timeout=None, follow_redirects=None):
        captured["url"] = url
        captured["cookie"] = headers["Cookie"]
        return _FakeResp()

    import httpx
    monkeypatch.setattr(httpx, "get", fake_get)

    # 阻止路径 3 启动 Chromium(测试环境跑不起来)
    import doupool.login.service as svc_mod
    original = svc_mod._verify_via_persistent_context
    svc_mod._verify_via_persistent_context = lambda *a, **kw: None
    try:
        identity = _verify_from_disk(tmp_path)
    finally:
        svc_mod._verify_via_persistent_context = original

    assert identity is not None
    assert identity["user_id"] == "99999"
    assert identity["nickname"] == "disk-命中"
    assert "sessionid=abc" in captured["cookie"]
    assert "uid_tt=u-xyz" in captured["cookie"]
    assert "passport/web/account/info/" in captured["url"]


def test_verify_from_disk_returns_none_on_non_200(monkeypatch, tmp_path: Path):
    """httpx 返回 401/302 → 返回 None"""
    (tmp_path / "cookies.json").write_text(
        json.dumps([{"name": "x", "value": "y", "domain": ".doubao.com"}]),
        encoding="utf-8",
    )

    class _FakeResp:
        status_code = 401

        def json(self):
            return {}

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResp())

    import doupool.login.service as svc_mod
    original = svc_mod._verify_via_persistent_context
    svc_mod._verify_via_persistent_context = lambda *a, **kw: None
    try:
        result = _verify_from_disk(tmp_path)
    finally:
        svc_mod._verify_via_persistent_context = original
    assert result is None


def test_verify_from_disk_returns_none_when_account_info_code_nonzero(monkeypatch, tmp_path: Path):
    """account/info 返回 code=-1(未登录)→ 返回 None"""
    (tmp_path / "cookies.json").write_text(
        json.dumps([{"name": "x", "value": "y", "domain": ".doubao.com"}]),
        encoding="utf-8",
    )

    class _FakeResp:
        status_code = 200

        def json(self):
            return {"code": -1, "message": "not login"}

    import httpx
    monkeypatch.setattr(httpx, "get", lambda *a, **kw: _FakeResp())

    import doupool.login.service as svc_mod
    original = svc_mod._verify_via_persistent_context
    svc_mod._verify_via_persistent_context = lambda *a, **kw: None
    try:
        result = _verify_from_disk(tmp_path)
    finally:
        svc_mod._verify_via_persistent_context = original
    assert result is None


def test_verify_from_disk_skips_non_doubao_cookies(monkeypatch, tmp_path: Path):
    """只有 google 域 cookie → 没 Cookie 头 → 返回 None"""
    (tmp_path / "cookies.json").write_text(
        json.dumps([{"name": "_ga", "value": "x", "domain": ".google.com"}]),
        encoding="utf-8",
    )

    called = {"count": 0}

    import httpx
    def fake_get(*a, **kw):
        called["count"] += 1
        raise AssertionError("应该不调 httpx")

    monkeypatch.setattr(httpx, "get", fake_get)

    import doupool.login.service as svc_mod
    original = svc_mod._verify_via_persistent_context
    svc_mod._verify_via_persistent_context = lambda *a, **kw: None
    try:
        result = _verify_from_disk(tmp_path)
    finally:
        svc_mod._verify_via_persistent_context = original
    assert result is None
    assert called["count"] == 0


# ---------------------------------------------------------------------------
# v0.2.6 _save_account_info_to_disk 测试
# ---------------------------------------------------------------------------


def test_save_account_info_returns_false_when_no_active_page(tmp_path: Path):
    """没有 active page → 返回 False,不写盘"""
    ctx = FakeContext(alive=True, pages=[])

    ok = _save_account_info_to_disk(ctx, tmp_path)

    assert ok is False
    assert not (tmp_path / "account_info.json").exists()


def test_save_account_info_returns_false_when_context_closed(tmp_path: Path):
    """context 已死 → 返回 False"""
    ctx = FakeContext(alive=False, pages=[FakePage(alive=True)])

    ok = _save_account_info_to_disk(ctx, tmp_path)

    assert ok is False
    assert not (tmp_path / "account_info.json").exists()


def test_save_account_info_writes_json_on_success(tmp_path: Path):
    """page.evaluate 返回 status=200 + code=0 → 写 account_info.json"""
    identity_payload = {
        "__status": 200,
        "__body": json.dumps({
            "code": 0,
            "data": {"user": {"user_id": "u-fetch", "name": "fetch-命中"}},
        }, ensure_ascii=False),
    }
    page = FakePage(alive=True, evaluate_payload=identity_payload)
    ctx = FakeContext(alive=True, pages=[page])

    ok = _save_account_info_to_disk(ctx, tmp_path)

    assert ok is True
    target = tmp_path / "account_info.json"
    assert target.exists()
    saved = json.loads(target.read_text(encoding="utf-8"))
    assert saved["data"]["user"]["user_id"] == "u-fetch"


def test_save_account_info_returns_false_on_non_200(tmp_path: Path):
    """fetch 返回 status != 200 → 不写盘"""
    page = FakePage(alive=True, evaluate_payload={
        "__status": 1011,
        "__body": '{"error_code":1011,"message":"用户未登录"}',
    })
    ctx = FakeContext(alive=True, pages=[page])

    ok = _save_account_info_to_disk(ctx, tmp_path)

    assert ok is False
    assert not (tmp_path / "account_info.json").exists()


def test_save_account_info_returns_false_on_code_nonzero(tmp_path: Path):
    """fetch 返回 200 但 code != 0(被 aegis 风控拒,仍是 200 但 code=-1)→ 不写盘"""
    page = FakePage(alive=True, evaluate_payload={
        "__status": 200,
        "__body": '{"code":-1,"message":"用户未登录","error_code":1011}',
    })
    ctx = FakeContext(alive=True, pages=[page])

    ok = _save_account_info_to_disk(ctx, tmp_path)

    assert ok is False
    assert not (tmp_path / "account_info.json").exists()


def test_save_account_info_returns_false_on_fetch_error_payload(tmp_path: Path):
    """fetch 在浏览器内抛错 → payload.__err 存在 → 不写盘"""
    page = FakePage(alive=True, evaluate_payload={"__err": "Failed to fetch"})
    ctx = FakeContext(alive=True, pages=[page])

    ok = _save_account_info_to_disk(ctx, tmp_path)

    assert ok is False
    assert not (tmp_path / "account_info.json").exists()


def test_save_account_info_returns_false_on_non_json_body(tmp_path: Path):
    """fetch 返回 200 但 body 不是 JSON → 不写盘"""
    page = FakePage(alive=True, evaluate_payload={
        "__status": 200,
        "__body": "<html>not json</html>",
    })
    ctx = FakeContext(alive=True, pages=[page])

    ok = _save_account_info_to_disk(ctx, tmp_path)

    assert ok is False
    assert not (tmp_path / "account_info.json").exists()


# ---------------------------------------------------------------------------
# _verify_from_disk 路径 1 (account_info.json) 测试
# ---------------------------------------------------------------------------


def test_verify_from_disk_prefers_account_info_over_httpx(tmp_path: Path):
    """account_info.json 存在 + 是登录态 → 优先用,不调 httpx 也不起 Chromium"""
    (tmp_path / "account_info.json").write_text(
        json.dumps({
            "code": 0,
            "data": {"user": {"user_id": "u-browser-fetch", "name": "browser-命中"}},
        }),
        encoding="utf-8",
    )

    # 阻止路径 3 启动 Chromium(测试环境没装浏览器跑不起来)
    import doupool.login.service as svc_mod
    original = svc_mod._verify_via_persistent_context
    svc_mod._verify_via_persistent_context = lambda *a, **kw: None
    try:
        identity = _verify_from_disk(tmp_path)
    finally:
        svc_mod._verify_via_persistent_context = original

    assert identity is not None
    assert identity["user_id"] == "u-browser-fetch"
    assert identity["nickname"] == "browser-命中"


def test_verify_from_disk_returns_none_when_account_info_not_login(tmp_path: Path):
    """account_info.json 存在但 code != 0 → 视为过期,继续走路径 2/3"""
    (tmp_path / "account_info.json").write_text(
        json.dumps({"code": -1, "message": "用户未登录"}),
        encoding="utf-8",
    )
    import doupool.login.service as svc_mod
    original = svc_mod._verify_via_persistent_context
    svc_mod._verify_via_persistent_context = lambda *a, **kw: None
    try:
        assert _verify_from_disk(tmp_path) is None
    finally:
        svc_mod._verify_via_persistent_context = original