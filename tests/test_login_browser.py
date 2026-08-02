import threading
import time

from doupool.login.browser import (
    _context_has_doubao_cookie,
    _is_doubao_cookie,
    _safe_page_verify,
    _safe_request_verify,
    _try_one_verify,
    wait_for_identity,
)
from doupool.login.detector import DoubaoIdentity, DoubaoLoginDetector
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
    def __init__(self, alive=True, cookies=None, request_identity=None):
        self._alive = alive
        self._cookies = cookies or []
        self._request_identity = request_identity
        self._request_calls = 0

    def is_closed(self):
        return not self._alive

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