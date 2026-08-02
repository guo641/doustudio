import threading

from doupool.login.browser import _try_verify, wait_for_identity
from doupool.login.detector import DoubaoIdentity, DoubaoLoginDetector
from playwright.sync_api import Error as PlaywrightError


class EventPumpingPage:
    def __init__(self, ready, identities):
        self.ready = ready
        self.identities = identities
        self.wait_calls = 0
        self.evaluate_calls = 0
        self.closed = False

    def is_closed(self):
        return self.closed

    def evaluate(self, _script):
        self.evaluate_calls += 1
        return 0

    def wait_for_timeout(self, milliseconds):
        assert milliseconds == 250
        self.wait_calls += 1
        self.identities.append(DoubaoIdentity("user-1", "莲韵"))
        self.ready.set()


class FallbackContext:
    def __init__(self, identity):
        self.identity = identity
        self.calls = 0

    class _Request:
        def __init__(self, outer):
            self.outer = outer

        def get(self, url):
            self.outer.calls += 1
            if self.outer.identity is None:
                raise RuntimeError("not logged in")
            return _FakeResponse(self.outer.identity)

    request = property(lambda self: self._Request(self))

    def __init__(self, identity):
        self.identity = identity
        self.calls = 0
        self._request = self._Request(self)

    @property
    def request(self):
        return self._request


class _FakeResponse:
    def __init__(self, identity):
        self.identity = identity
        self.ok = True
        self.status = 200

    def json(self):
        return {"code": 0, "data": {"user": {"user_id": self.identity.user_id, "name": self.identity.nickname}}}


def test_wait_loop_pumps_playwright_events():
    ready = threading.Event()
    identities = []
    page = EventPumpingPage(ready, identities)

    identity = wait_for_identity(page, ready, identities, threading.Event())

    assert page.wait_calls == 1
    assert identity.user_id == "user-1"


def test_wait_loop_falls_back_to_verify_when_page_closed():
    """page 被关(用户手动关 / doubao 前端 navigation)→ fallback verify 拿到身份"""
    ready = threading.Event()
    identities = []
    page = EventPumpingPage(ready, identities)
    page.closed = True  # 模拟 page 已关
    context = FallbackContext(DoubaoIdentity("user-cookie", "cookie-昵称"))
    detector = DoubaoLoginDetector()

    identity = wait_for_identity(
        page, ready, identities, threading.Event(),
        detector=detector, context=context,
    )

    assert identity.user_id == "user-cookie"
    assert identity.nickname == "cookie-昵称"


def test_wait_loop_raises_when_page_closed_and_no_identity():
    """page 关闭且 cookie 也没登录 → raise 登录窗口已关闭"""
    ready = threading.Event()
    identities = []
    page = EventPumpingPage(ready, identities)
    page.closed = True
    context = FallbackContext(None)  # verify 返回 None
    detector = DoubaoLoginDetector()

    try:
        wait_for_identity(
            page, ready, identities, threading.Event(),
            detector=detector, context=context,
        )
    except RuntimeError as exc:
        assert "登录窗口已关闭" in str(exc)
    else:
        raise AssertionError("应该抛 RuntimeError")


def test_wait_loop_recovers_from_wait_timeout_error():
    """wait_for_timeout 抛 PlaywrightError 后,如果 identity_ready 已设,正常返回"""
    class CrashingPage(EventPumpingPage):
        def wait_for_timeout(self, milliseconds):
            self.wait_calls += 1
            self.identities.append(DoubaoIdentity("user-late", "迟到"))
            self.ready.set()
            # 抛错模拟 page 在 sleep 中被关
            raise PlaywrightError("Target page, context or browser has been closed")

    ready = threading.Event()
    identities = []
    page = CrashingPage(ready, identities)
    context = FallbackContext(None)

    identity = wait_for_identity(
        page, ready, identities, threading.Event(),
        detector=DoubaoLoginDetector(), context=context,
    )

    assert identity.user_id == "user-late"


def test_wait_loop_raises_when_wait_timeout_and_no_identity():
    """wait_for_timeout 抛错且 identity 还没拿到 → raise"""
    class CrashingPage(EventPumpingPage):
        def wait_for_timeout(self, milliseconds):
            # 不 set identity,只抛错
            raise PlaywrightError("Target page, context or browser has been closed")

    ready = threading.Event()
    identities = []
    page = CrashingPage(ready, identities)
    context = FallbackContext(None)

    try:
        wait_for_identity(
            page, ready, identities, threading.Event(),
            detector=DoubaoLoginDetector(), context=context,
        )
    except RuntimeError as exc:
        assert "登录窗口已关闭" in str(exc)
    else:
        raise AssertionError("应该抛 RuntimeError")


def test_try_verify_returns_identity_when_context_logged_in():
    identity = DoubaoIdentity("u-2", "昵称2")
    context = FallbackContext(identity)
    detector = DoubaoLoginDetector()

    got = _try_verify(detector, context)

    assert got is not None
    assert got.user_id == "u-2"


def test_try_verify_returns_none_when_context_not_logged_in():
    context = FallbackContext(None)
    detector = DoubaoLoginDetector()

    assert _try_verify(detector, context) is None
