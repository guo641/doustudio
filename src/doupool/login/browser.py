from __future__ import annotations

import threading
from pathlib import Path

from playwright.sync_api import sync_playwright

from .detector import DoubaoLoginDetector, ResponseMeta
from .service import VerifiedLogin


def wait_for_identity(page, identity_ready, identities, cancel_event):
    while not cancel_event.is_set():
        if identity_ready.is_set():
            return identities[0]
        if page.is_closed():
            raise RuntimeError("登录窗口已关闭")
        page.wait_for_timeout(250)
        if identity_ready.is_set():
            return identities[0]
    raise RuntimeError("登录已取消")


class PlaywrightLoginRunner:
    def __init__(self, detector: DoubaoLoginDetector | None = None):
        self.detector = detector or DoubaoLoginDetector()

    def run(self, attempt_id, profile_dir: Path, emit, cancel_event: threading.Event):
        identity_ready = threading.Event()
        identities = []
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=False,
                viewport={"width": 1100, "height": 760},
            )
            page = context.pages[0] if context.pages else context.new_page()

            def on_response(response):
                meta = ResponseMeta(response.url, response.status, response.request.method)
                if self.detector.observe(meta):
                    try:
                        identity = self.detector.identity_from_response(meta, response.json())
                    except Exception:
                        identity = None
                    if identity and not identities:
                        identities.append(identity)
                        identity_ready.set()

            context.on("response", on_response)
            page.goto("https://www.doubao.com/", wait_until="domcontentloaded")
            emit("waiting_for_scan", "请在豆包窗口中扫码登录")
            try:
                identity = wait_for_identity(page, identity_ready, identities, cancel_event)
                emit("verifying", "已检测到登录，正在确认账号")
                return VerifiedLogin(identity.as_mapping(), str(profile_dir))
            finally:
                context.close()
