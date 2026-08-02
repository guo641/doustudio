from __future__ import annotations

import logging
import threading
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError, sync_playwright

from .detector import DoubaoIdentity, DoubaoLoginDetector, ResponseMeta
from .service import VerifiedLogin

_LOG = logging.getLogger("doupool.login")


def _try_verify(detector: DoubaoLoginDetector, context) -> DoubaoIdentity | None:
    """
    兜底:用 cookie 直接调 /passport/web/account/info/ 验证是否已登录。

    解决两个真实场景:
    1. doubao 登录成功走的是我们 observe 没覆盖的路径 → on_response 没触发
    2. 用户扫码成功后 doubao 前端 navigation 关闭了原始 page → 没法再
       等下一次 response,但 cookie 已经写入 profile,context.request 还能用
    """
    try:
        return detector.verify(context)
    except Exception:
        _LOG.exception("verify 兜底调用失败")
        return None


def _retry_verify(
    detector: DoubaoLoginDetector,
    context,
    cancel_event: threading.Event,
    retries: int = 3,
    backoff: float = 0.1,
) -> DoubaoIdentity | None:
    """
    重试 verify,处理以下真实场景:
    1. on_response 队列里有未 dispatch 的响应 → wait_for_identity 在 except 块
       检查时 identity_ready 还是 False,但很快就会被 on_response 处理
    2. context.request.get() 在 page 刚关但 context 还在时可能瞬时抛 TargetClosedError
       一次失败不代表真没登录,需要再试
    3. doubao 写 cookie 后 1-2 秒内 /account/info 才稳定可读,直接 verify 可能命中
       旧 cookie → 返回 None,稍后重试就成功

    Returns: 首个拿到的 DoubaoIdentity,或 None(全部失败)
    """
    for attempt in range(retries):
        if cancel_event.is_set():
            return None
        identity = _try_verify(detector, context)
        if identity:
            return identity
        if attempt < retries - 1:
            # 用 cancel_event.wait 而不是 time.sleep,这样 cancel 时立即退出
            cancel_event.wait(timeout=backoff)
    return None


def _cookie_poll_loop(
    context,
    identity_ready: threading.Event,
    identities: list[DoubaoIdentity],
    identities_lock: threading.Lock,
    detector: DoubaoLoginDetector,
    cancel_event: threading.Event,
    poll_interval: float = 0.5,
) -> None:
    """
    独立兜底通道:周期性检查 context.cookies(),发现 doubao.com 域 cookie 出现
    时立即触发 verify。这条路径**不依赖 on_response 事件分发**,即使 page 已被
    doubao 前端关掉,只要 context 还在 + cookie 已写入 → poll_interval 内必然
    命中。

    Playwright sync API 的事件分发跟 page.close 是同一个线程,close 消息到达
    dispatcher 时未处理的 response 事件会被 Connection.cleanup() 短路吞掉,
    导致 on_response 路径完全失效——这条线程绕开了整个事件分发机制。
    """
    _LOG.info("cookie poll: 启动 (interval=%.1fs)", poll_interval)
    while not cancel_event.is_set() and not identity_ready.is_set():
        try:
            cookies = context.cookies()
        except Exception as exc:
            _LOG.info("cookie poll: context 已关闭 (%s),退出", exc)
            return
        # 任意 doubao.com 域 cookie 出现 → 已登录
        has_doubao = any("doubao.com" in (c.get("domain") or "") for c in cookies)
        if has_doubao:
            _LOG.info("cookie poll: 检测到 doubao.com cookie,触发 verify")
            identity = _try_verify(detector, context)
            if identity:
                with identities_lock:
                    if not identities:
                        identities.append(identity)
                        identity_ready.set()
                _LOG.info("cookie poll: 命中 identity user_id=%s", identity.user_id)
                return
            # cookie 出现但 verify 拿不到 → 可能是 cookie 刚写、account/info
            # 还没准备好,下个 poll 再试
            _LOG.debug("cookie poll: cookie 已存在但 verify 未拿到,稍后重试")
        cancel_event.wait(timeout=poll_interval)
    _LOG.info("cookie poll: 已退出(cancel_event=%s, identity_ready=%s)",
              cancel_event.is_set(), identity_ready.is_set())


def wait_for_identity(
    page,
    identity_ready: threading.Event,
    identities: list[DoubaoIdentity],
    cancel_event: threading.Event,
    detector: DoubaoLoginDetector | None = None,
    context=None,
    fallback_interval: float = 2.0,
):
    """
    等登录成功的 identity,直到 cancel_event / 超时 / page 关闭。

    返回首个拿到的 DoubaoIdentity。

    错误容忍:
    - page.wait_for_timeout 可能在 page 已关时抛 PlaywrightError,这里捕获后
      检查 identity_ready 状态,而不是直接让异常冒泡把整个 runner 弄崩
    - page 关闭 / 抛出时,如果 detector + context 可用,**重试多次** fallback
      verify —— 用户已经登录但 on_response 没匹配上(常见的 page-close race)
    """
    last_verify_at = 0.0
    while not cancel_event.is_set():
        if identity_ready.is_set() and identities:
            return identities[0]

        # page 已被关(用户手动关 / doubao 前端 navigation 触发)→ 重试 verify
        try:
            page_alive = not page.is_closed()
        except PlaywrightError:
            page_alive = False
        if not page_alive:
            if detector is not None and context is not None:
                identity = _retry_verify(detector, context, cancel_event)
                if identity:
                    return identity
            raise RuntimeError("登录窗口已关闭")

        try:
            page.wait_for_timeout(250)
        except PlaywrightError as exc:
            # 在 sleep 期间 page 被关 → 不要再抛,先重试用 cookie 验证
            if identity_ready.is_set() and identities:
                return identities[0]
            if detector is not None and context is not None:
                identity = _retry_verify(detector, context, cancel_event)
                if identity:
                    return identity
            raise RuntimeError(f"登录窗口已关闭:{exc}") from exc

        # 周期性 fallback:即使 page 还活着,也每隔 fallback_interval 用 cookie 验证
        # 一次,处理 detector.observe 没覆盖的真实登录路径
        now = 0
        try:
            if not page.is_closed():
                now = page.evaluate("() => Date.now()")
        except PlaywrightError:
            now = 0
        if (
            detector is not None
            and context is not None
            and now
            and (now - last_verify_at) >= fallback_interval * 1000
        ):
            last_verify_at = now
            identity = _try_verify(detector, context)
            if identity:
                identities.append(identity)
                identity_ready.set()
                return identities[0]

        if identity_ready.is_set() and identities:
            return identities[0]
    raise RuntimeError("登录已取消")


class PlaywrightLoginRunner:
    def __init__(self, detector: DoubaoLoginDetector | None = None):
        self.detector = detector or DoubaoLoginDetector()

    def run(self, attempt_id, profile_dir: Path, emit, cancel_event: threading.Event):
        identity_ready = threading.Event()
        identities: list[DoubaoIdentity] = []
        # 保护 on_response 与 fallback verify 之间的 identities 读写竞争
        identities_lock = threading.Lock()
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=False,
                viewport={"width": 1100, "height": 760},
            )
            page = context.pages[0] if context.pages else context.new_page()

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
                with identities_lock:
                    if not identities:
                        identities.append(identity)
                        identity_ready.set()

            context.on("response", on_response)

            # 启动 cookie 轮询线程作为独立兜底通道。
            # on_response 走 Playwright sync API 的事件分发,page.close 跟 response
            # 在同一线程,doubao navigation 关掉原始 page 时 Connection.cleanup()
            # 会短路吞掉未 dispatch 的 response 事件。这条线程绕开事件分发,只要
            # context 还活着 + cookie 已写入 → 500ms 内必然命中。
            cookie_poll_thread = threading.Thread(
                target=_cookie_poll_loop,
                args=(
                    context,
                    identity_ready,
                    identities,
                    identities_lock,
                    self.detector,
                    cancel_event,
                ),
                name=f"cookie-poll-{attempt_id}",
                daemon=True,
            )
            cookie_poll_thread.start()

            try:
                page.goto("https://www.doubao.com/", wait_until="domcontentloaded")
            except PlaywrightError as exc:
                raise RuntimeError(f"无法打开豆包登录页:{exc}") from exc
            emit("waiting_for_scan", "请在豆包窗口中扫码登录")
            try:
                identity = wait_for_identity(
                    page,
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
