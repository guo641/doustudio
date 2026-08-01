from __future__ import annotations

import socket
import threading
import time

import httpx
import uvicorn
import webview


def find_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class DesktopRuntime:
    def __init__(self, app, debug: bool = False):
        self.port = find_free_port()
        self.url = f"http://127.0.0.1:{self.port}"
        self.debug = debug
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="warning"))
        self.thread = threading.Thread(target=self.server.run, name="doupool-api")

    def run(self) -> None:
        self.thread.start()
        for _ in range(100):
            try:
                if httpx.get(f"{self.url}/api/health", timeout=0.2).status_code == 200:
                    break
            except httpx.HTTPError:
                time.sleep(0.1)
        else:
            raise RuntimeError("本地服务启动超时")
        webview.create_window("DoubaoManager", self.url, width=1280, height=820, min_size=(960, 640))
        try:
            webview.start(debug=self.debug)
        finally:
            self.server.should_exit = True
            self.thread.join(timeout=5)

