from __future__ import annotations

import logging
import threading
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from .detector import DoubaoIdentity, DoubaoLoginDetector, ResponseMeta
from .service import VerifiedLogin

_LOG = logging.getLogger("doupool.login")


# Playwright sync API 的所有调用必须在创建 sync_playwright() 的同一 OS thread / greenlet
# 内完成(它是 gevent 协程实现,跨线程会触发 "Cannot switch to a different thread")。
# 所以 v0.2.4 放弃 v0.2.3 的 daemon thread cookie_poll_loop,改在 runner owner thread
# 内用 page.wait_for_timeout() pump 事件 + context.on('page')/on('framenavigated')/
# on('close') 监听页面生命周期。

# 在主循环里几次 verify 失败后、还要等多长时间才认定登录失败。
# 真实场景:doubao navigation 关掉原始 page 后,新 page 可能 1-3 秒后才出现,
# account/info fetch 也要等 cookie 真正生效,所以保留 grace period。
GRACE_PERIOD_SECONDS = 6.0
LOOP_TICK_MS = 250  # 主循环 sleep 间隔(同时 pump Playwright 事件)
VERIFY_RETRY_BACKOFF = 0.3  # verify 失败后再次尝试的退避


def _is_doubao_cookie(cookie: dict) -> bool:
    """判断 cookie 是否来自 doubao 域。"""
    return "doubao.com" in (cookie.get("domain") or "")


def _context_has_doubao_cookie(context) -> bool:
    """
    在 owner thread 内调 context.cookies() —— 这是 Playwright sync API 文档推荐的
    权威 cookie 来源。document.cookie 不可用,因为:
    1. HttpOnly cookie 不可见(sessionid / uid_tt / passport_auth_status 通常都是)
    2. domain / path / secure / partition 限制不一定匹配当前文档
    """
    try:
        cookies = context.cookies()
    except PlaywrightError as exc:
        _LOG.debug("context.cookies() 不可用: %s", exc)
        return False
    return any(_is_doubao_cookie(c) for c in cookies)


def _context_is_alive(context) -> bool:
    """Playwright 1.54+ 提供 is_closed(),用来做 TOCTOU 提示(evaluate/request
    外层仍必须 try/except,因为 is_closed 和真正调用之间还有时间窗)。"""
    try:
        return not context.is_closed()
    except PlaywrightError:
        return False


def _page_is_alive(page) -> bool:
    try:
        return not page.is_closed()
    except PlaywrightError:
        return False


def _safe_request_verify(detector: DoubaoLoginDetector, context):
    """
    用 context.request.get(/passport/web/account/info/) 拿 identity。
    context 关掉时会抛 Request context disposed / Target page...;都视为
    "暂时拿不到",不要让一个非确定性错误干掉整个 grace period。
    """
    if not _context_is_alive(context):
        return None
    try:
        return detector.verify(context)
    except PlaywrightError as exc:
        _LOG.warning("context.request.get 抛 PlaywrightError: %s", exc)
        return None
    except Exception:
        _LOG.exception("verify() 异常")
        return None


def _safe_page_verify(detector: DoubaoLoginDetector, page) -> DoubaoIdentity | None:
    """
    通过 page.evaluate() 在浏览器里 fetch /passport/web/account/info/ 拿 identity。
    浏览器自动带 HttpOnly cookie,这是最权威的检测方式 —— 不受 page.is_closed TOCTOU
    限制,因为 evaluate 抛错会被外层 catch。

    navigation 中 evaluate 会抛 'Execution context destroyed' / 'Target closed',
    每次独立 catch,只重试可恢复错误,不要吞整个循环。
    """
    if not _page_is_alive(page):
        return None
    try:
        payload = page.evaluate(
            """
            async () => {
                try {
                    const resp = await fetch(
                        'https://www.doubao.com/passport/web/account/info/',
                        { credentials: 'include' }
                    );
                    if (!resp.ok) return null;
                    return await resp.json();
                } catch (e) {
                    return { __err: String(e) };
                }
            }
            """
        )
    except PlaywrightError as exc:
        _LOG.debug("page.evaluate(account_info) 抛 PlaywrightError: %s", exc)
        return None
    if not isinstance(payload, dict):
        return None
    if "__err" in payload:
        _LOG.debug("page.evaluate fetch 失败: %s", payload.get("__err"))
        return None
    return detector.identity_from_response(
        ResponseMeta(
            "https://www.doubao.com/passport/web/account/info/",
            200,
            "GET",
        ),
        payload,
    )


def wait_for_identity(
    pages_provider,
    identity_ready: threading.Event,
    identities: list[DoubaoIdentity],
    cancel_event: threading.Event,
    detector: DoubaoLoginDetector | None = None,
    context=None,
):
    """
    在 owner thread 内等登录成功的 identity。

    pages_provider: callable () -> list[Page],返回当前 context 下所有 active page
                    (包括 navigation 后新开 / popup)。用它避免长期持有旧 page 引用,
                    防止旧 page 被关后误判。

    关键设计:
    - **不依赖** on_response 事件分发(它会被 Connection.cleanup() 短路吞)
    - **不依赖** 单一 page 引用(因为 doubao navigation 会换 page)
    - **不依赖** daemon 线程 cookie poll(gevent 跨线程会死)
    - **依赖** page.wait_for_timeout(250) 在 owner thread 内 pump 事件 +
      on('page')/on('framenavigated')/on('close') 同步更新 active_pages

    容错:
    - 每次 evaluate/request 都独立 try/except,只重试可恢复错误
    - page 关闭后给 GRACE_PERIOD_SECONDS 时间,等新 page 出现 / cookie 生效 / verify 成功
    - grace period 内仍拿不到 → raise RuntimeError
    """
    if detector is None or context is None:
        raise RuntimeError("wait_for_identity 必须在 owner thread 调用且需要 detector+context")

    last_verify_attempt = 0.0  # 上次尝试 verify 的 wall time
    page_closed_since: float | None = None  # 第一次检测到原 page 关闭的时间

    while not cancel_event.is_set():
        # 1. on_response 已经拿到 identity → 立刻返回
        if identity_ready.is_set() and identities:
            return identities[0]

        active = list(pages_provider())
        any_alive = any(_page_is_alive(p) for p in active)
        has_doubao_cookie = _context_has_doubao_cookie(context)
        context_alive = _context_is_alive(context)

        # 2. context 已经彻底死了(用户关了浏览器 / 我们 close 了 context)
        #    无法再 verify,只能接受已捕获的 identity(若有)
        if not context_alive:
            _LOG.warning("wait_for_identity: context 已关闭,放弃 verify")
            if identities:
                return identities[0]
            raise RuntimeError("登录窗口已关闭")

        # 3. 周期性 verify(基于 wall time,不依赖 page 计时器)
        #    只要有 active page 或者 cookie 已写入,就持续尝试。
        now = _monotonic()
        verify_due = (now - last_verify_attempt) >= VERIFY_RETRY_BACKOFF

        if verify_due and (any_alive or has_doubao_cookie):
            last_verify_attempt = now
            identity = _try_one_verify(detector, active, context)
            if identity:
                with _lock_identities(identities, identity_ready):
                    if not identities:
                        identities.append(identity)
                        identity_ready.set()
                _LOG.info("wait_for_identity: 命中 identity user_id=%s", identity.user_id)
                return identities[0]

        # 4. page 关闭追踪
        if not any_alive:
            if page_closed_since is None:
                page_closed_since = now
                _LOG.info("wait_for_identity: 没有 active page,开始 grace period (%.1fs)",
                          GRACE_PERIOD_SECONDS)
            elif (now - page_closed_since) >= GRACE_PERIOD_SECONDS:
                # grace 过了还没新 page 也没拿到 → 真的失败
                _LOG.warning("wait_for_identity: grace period 超时 (%.1fs)",
                             GRACE_PERIOD_SECONDS)
                if identities:
                    return identities[0]
                raise RuntimeError("登录窗口已关闭")
        else:
            # 有 page 回来了(新 page)→ 重置 grace
            if page_closed_since is not None:
                _LOG.info("wait_for_identity: 新 page 出现,重置 grace period")
            page_closed_since = None

        # 5. pump Playwright 事件。这是 sync API 的关键 —— wait_for_timeout 内部
        #    在 poll dispatcher,任何 on('page')/on('framenavigated')/on('close')
        #    注册的回调会在这个调用期间被 dispatch。
        try:
            if active and any_alive:
                active[0].wait_for_timeout(LOOP_TICK_MS)
            else:
                # 没有 active page 时不能调 page.wait_for_timeout,会抛 Target closed。
                # 退到 context.wait_for_event('page', timeout) —— 它会在 owner thread
                # 内等待新 page,期间 pump 事件;timeout 后返回 None 不算异常。
                try:
                    context.wait_for_event("page", timeout=LOOP_TICK_MS / 1000.0)
                except PlaywrightError as exc:
                    _LOG.debug("context.wait_for_event('page') 失败: %s", exc)
        except PlaywrightError as exc:
            # page 在 sleep 中被关 —— 不 raise,下一轮循环重新检查 page_alive
            _LOG.debug("wait_for_timeout 抛 PlaywrightError: %s", exc)

        # 6. on_response 可能在 pump 期间被 dispatch
        if identity_ready.is_set() and identities:
            return identities[0]

    raise RuntimeError("登录已取消")


def _try_one_verify(detector: DoubaoLoginDetector, active_pages, context):
    """
    三步 verify,每步独立 try/except:
    1. 对每个 active page 调 page.evaluate(fetch account/info) —— 最权威(浏览器带 HttpOnly cookie)
    2. context.request.get(/account/info/) —— 兜底
    3. 都拿不到时,**只要 cookie 已写入**就保留 grace(由外层处理),不立即失败
    """
    # 1. page.evaluate
    for page in active_pages:
        identity = _safe_page_verify(detector, page)
        if identity:
            return identity

    # 2. context.request
    if _context_has_doubao_cookie(context):
        identity = _safe_request_verify(detector, context)
        if identity:
            return identity

    return None


class _LockCtx:
    """最小互斥:保护 identities 写入与 identity_ready.set()"""

    def __init__(self, identities, identity_ready):
        self._lock = threading.Lock()

    def __enter__(self):
        self._lock.acquire()
        return self

    def __exit__(self, *exc):
        self._lock.release()
        return False


def _lock_identities(identities, identity_ready):
    return _LockCtx(identities, identity_ready)


def _monotonic() -> float:
    import time
    return time.monotonic()


class PlaywrightLoginRunner:
    def __init__(self, detector: DoubaoLoginDetector | None = None):
        self.detector = detector or DoubaoLoginDetector()

    def run(self, attempt_id, profile_dir: Path, emit, cancel_event: threading.Event):
        identity_ready = threading.Event()
        identities: list[DoubaoIdentity] = []
        active_pages: list = []  # 由 context.on('page') 维护
        active_pages_lock = threading.Lock()

        def add_page(page):
            with active_pages_lock:
                if page not in active_pages:
                    active_pages.append(page)
                    _LOG.info("active page added: %s url=%s", id(page), page.url)

        def remove_page(page):
            with active_pages_lock:
                if page in active_pages:
                    active_pages.remove(page)
                    _LOG.info("active page removed: %s", id(page))

        def get_active():
            with active_pages_lock:
                # 过滤掉已关的(可能 on('close') 还没派发)
                return [p for p in active_pages if _page_is_alive(p)]

        def on_response(response):
            try:
                meta = ResponseMeta(
                    response.url, response.status, response.request.method
                )
                if not self.detector.observe(meta):
                    return
                try:
                    payload = response.json()
                except Exception:
                    _LOG.debug("on_response: %s 不是 JSON,跳过", response.url)
                    return
                identity = self.detector.identity_from_response(meta, payload)
            except Exception:
                _LOG.exception("登录响应处理异常,继续等待下一条")
                return
            if identity is None:
                _LOG.debug("on_response: %s 未提取到 identity", response.url)
                return
            _LOG.info("on_response: 检测到登录响应 user_id=%s url=%s",
                      identity.user_id, response.url)
            if not identities:
                identities.append(identity)
                identity_ready.set()

        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=False,
                viewport={"width": 1100, "height": 760},
            )

            # 必须在 goto 之前注册所有 page 监听,否则 navigation 触发的新 page
            # / 新 framenavigated 会丢。
            context.on("page", add_page)
            context.on("response", on_response)

            initial_page = context.pages[0] if context.pages else context.new_page()
            add_page(initial_page)
            initial_page.on("close", lambda _: remove_page(initial_page))
            initial_page.on(
                "framenavigated",
                lambda _: _LOG.debug("initial page framenavigated url=%s", initial_page.url),
            )

            try:
                initial_page.goto("https://www.doubao.com/", wait_until="domcontentloaded")
            except PlaywrightError as exc:
                raise RuntimeError(f"无法打开豆包登录页:{exc}") from exc
            emit("waiting_for_scan", "请在豆包窗口中扫码登录")

            try:
                identity = wait_for_identity(
                    get_active,
                    identity_ready,
                    identities,
                    cancel_event,
                    detector=self.detector,
                    context=context,
                )
                emit("verifying", "已检测到登录，正在确认账号")
                return VerifiedLogin(identity.as_mapping(), str(profile_dir))
            finally:
                try:
                    context.close()
                except PlaywrightError:
                    pass