import threading
import time

from doupool.login.browser import (
    _cookie_poll_loop,
    _retry_verify,
    _try_verify,
    wait_for_identity,
)
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


# ---------------------------------------------------------------------------
# Round 6:cookie poll daemon + retry verify
# ---------------------------------------------------------------------------


class FakeCookieContext:
    """模拟 Playwright BrowserContext,只暴露 cookies()。detector.verify 走 _try_verify
    时被 detector.verify 替换成 lambda 短路掉,所以这里不需要实现 request"""

    def __init__(self, cookies, identity=None):
        self._cookies = cookies
        self._identity = identity

    def cookies(self):
        return list(self._cookies)


def test_cookie_poll_loop_sets_identity_when_doubao_cookies_appear():
    """doubao.com cookie 出现 → 触发 verify 并 set identity_ready"""
    cookies = [
        {"name": "sessionid", "domain": ".doubao.com", "value": "xxx"},
    ]
    context = FakeCookieContext(
        cookies,
        identity=DoubaoIdentity("user-poll", "轮询命中"),
    )
    # verify 直接走 detector,但 detector.verify 调 request_api.get;
    # 给一个让 detector.verify 拿到 identity 的包装
    detector = DoubaoLoginDetector()
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    lock = threading.Lock()
    cancel = threading.Event()

    # 替换 detector.verify → 直接返回 identity,跳过真实 HTTP
    detector.verify = lambda _ctx: DoubaoIdentity("user-poll", "轮询命中")  # type: ignore[assignment]

    _cookie_poll_loop(
        context, ready, identities, lock,
        detector, cancel, poll_interval=0.05,
    )

    assert ready.is_set()
    assert identities and identities[0].user_id == "user-poll"


def test_cookie_poll_loop_exits_when_context_closed():
    """cookies() 抛异常 → 线程退出,不挂住"""
    class ClosedContext:
        def cookies(self):
            raise RuntimeError("context has been closed")

    detector = DoubaoLoginDetector()
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    lock = threading.Lock()
    cancel = threading.Event()

    # 应该正常退出,不抛
    _cookie_poll_loop(
        ClosedContext(), ready, identities, lock,
        detector, cancel, poll_interval=0.05,
    )

    assert not ready.is_set()
    assert identities == []


def test_cookie_poll_loop_ignores_non_doubao_cookies():
    """没 doubao.com cookie → 不触发 verify,identity_ready 仍 False"""
    cookies = [{"name": "_ga", "domain": ".google.com", "value": "x"}]
    context = FakeCookieContext(cookies)
    detector = DoubaoLoginDetector()
    detector.verify = lambda _ctx: None  # type: ignore[assignment]
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    lock = threading.Lock()
    cancel = threading.Event()

    # 后台跑 poll loop,验证 verify 没被调用
    t = threading.Thread(
        target=_cookie_poll_loop,
        args=(context, ready, identities, lock, detector, cancel),
        kwargs={"poll_interval": 0.05},
        daemon=True,
    )
    t.start()
    time.sleep(0.2)  # 让它 poll 几轮
    cancel.set()  # 触发退出
    t.join(timeout=1.0)

    assert not ready.is_set()
    assert identities == []
    assert not t.is_alive(), "poll loop 应该在 cancel 后退出"


def test_retry_verify_returns_first_success():
    """verify 前两次返回 None,第三次成功 → 返回 identity"""

    class FlakyVerifyDetector:
        def __init__(self):
            self.calls = 0

        def verify(self, _ctx):
            self.calls += 1
            if self.calls < 3:
                return None
            return DoubaoIdentity("u-retry", "重试命中")

    detector = FlakyVerifyDetector()
    context = FakeCookieContext([])

    identity = _retry_verify(
        detector, context, threading.Event(),
        retries=3, backoff=0.01,
    )

    assert identity is not None
    assert identity.user_id == "u-retry"
    assert detector.calls == 3


def test_retry_verify_returns_none_when_all_fail():
    """verify 始终返回 None → 返回 None(不抛)"""
    class AlwaysFailDetector:
        def verify(self, _ctx):
            return None

    detector = AlwaysFailDetector()
    context = FakeCookieContext([])

    assert _retry_verify(
        detector, context, threading.Event(),
        retries=3, backoff=0.01,
    ) is None


def test_wait_identity_retries_verify_before_raising_on_page_close():
    """page 已关且 verify 多次失败 → 调多次 verify 后才 raise"""
    call_count = {"n": 0}

    class CountingDetector:
        def verify(self, _ctx):
            call_count["n"] += 1
            return None  # 永远拿不到

    detector = CountingDetector()
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    page = EventPumpingPage(ready, identities)
    page.closed = True
    context = FakeCookieContext([])

    try:
        wait_for_identity(
            page, ready, identities, threading.Event(),
            detector=detector, context=context,
        )
    except RuntimeError as exc:
        assert "登录窗口已关闭" in str(exc)
    else:
        raise AssertionError("应该抛 RuntimeError")

    # retry 3 次,verify 应该被调用 3 次
    assert call_count["n"] == 3


def test_wait_identity_recovers_when_verify_succeeds_on_retry():
    """page 已关,但 verify 重试第 2 次成功 → 正常返回 identity"""
    call_count = {"n": 0}

    class SucceedOnSecondDetector:
        def verify(self, _ctx):
            call_count["n"] += 1
            if call_count["n"] < 2:
                return None
            return DoubaoIdentity("u-late", "迟到但成功")

    detector = SucceedOnSecondDetector()
    ready = threading.Event()
    identities: list[DoubaoIdentity] = []
    page = EventPumpingPage(ready, identities)
    page.closed = True
    context = FakeCookieContext([])

    identity = wait_for_identity(
        page, ready, identities, threading.Event(),
        detector=detector, context=context,
    )

    assert identity.user_id == "u-late"
    assert call_count["n"] == 2
