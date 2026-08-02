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
    - page 关闭 / 抛出时,如果 detector + context 可用,fallback 调一次
      /passport/web/account/info/ —— 用户已经登录但 on_response 没匹配上
    """
    last_verify_at = 0.0
    while not cancel_event.is_set():
        if identity_ready.is_set() and identities:
            return identities[0]

        # page 已被关(用户手动关 / doubao 前端 navigation 触发)→ 尝试 fallback verify
        try:
            page_alive = not page.is_closed()
        except PlaywrightError:
            page_alive = False
        if not page_alive:
            if detector is not None and context is not None:
                identity = _try_verify(detector, context)
                if identity:
                    return identity
            raise RuntimeError("登录窗口已关闭")

        try:
            page.wait_for_timeout(250)
        except PlaywrightError as exc:
            # 在 sleep 期间 page 被关 → 不要再抛,先尝试用 cookie 验证
            if identity_ready.is_set() and identities:
                return identities[0]
            if detector is not None and context is not None:
                identity = _try_verify(detector, context)
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
                        # 响应不是 JSON(可能 HTML 错误页),不致命
                        return
                    identity = self.detector.identity_from_response(meta, payload)
                except Exception:
                    _LOG.exception("登录响应处理异常,继续等待下一条")
                    return
                if identity:
                    with identities_lock:
                        if not identities:
                            identities.append(identity)
                            identity_ready.set()

            context.on("response", on_response)
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
