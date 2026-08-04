"""v0.2.20:跟踪已打开的 profile 浏览器会话(供「📂 打开浏览器」按钮复用)。

单个 Chromium 进程对同一个 profile_dir 有 Lockfile 互斥(Chromium 会创建
SingletonLock),所以同一 profile_dir **最多同时存在一个 Playwright 实例**。
本模块负责:

- registry[(account_id, profile_dir)] -> BrowserSession
  记录当前活跃的 Playwright 窗口;start 前查重,start 后塞进去,窗口关闭后清掉。
- 提供给 API 层用,API 层负责启线程调用 Playwright,registry 只做锁和状态。

设计上不依赖任何 asyncio —— sync Playwright 在 threadpool 里跑,registry
自身是纯线程安全数据结构。FastAPI 端点负责 asyncio.to_thread 派发。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

_LOG = logging.getLogger("doupool.login.browser_sessions")


@dataclass(frozen=True, slots=True)
class BrowserSession:
    """一个正在打开的浏览器窗口。

    thread:持有 Playwright 的后台线程(daemon,窗口被关时它会自然退出)。
    cancel:API cancel 调用 set 后,runner 线程在下一个 wait_for_timeout 切片
           检测到事件就主动 context.close()。
    profile_dir:此窗口绑定的 profile 目录(用于诊断日志)。
    """

    thread: threading.Thread
    cancel: threading.Event
    profile_dir: Path


class BrowserSessionsRegistry:
    """profile_dir -> BrowserSession 的全局注册表。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # 用 profile_dir 字符串作 key —— 一个 profile 只能开一个窗口
        self._by_profile: dict[str, BrowserSession] = {}
        # account_id -> profile_dir 反向索引,方便 cancel API 直接查
        self._profile_by_account: dict[str, str] = {}

    def is_open(self, account_id: str) -> bool:
        profile_key = self._profile_by_account.get(account_id)
        if not profile_key:
            return False
        with self._lock:
            session = self._by_profile.get(profile_key)
            return session is not None and session.thread.is_alive()

    def get_profile_key(self, account_id: str) -> Optional[str]:
        return self._profile_by_account.get(account_id)

    def register(self, account_id: str, session: BrowserSession) -> None:
        profile_key = str(session.profile_dir)
        with self._lock:
            existing = self._by_profile.get(profile_key)
            if existing is not None and existing.thread.is_alive():
                raise BrowserAlreadyOpen(
                    f"账号 {account_id} 的浏览器窗口已打开,先关掉再开新的"
                )
            self._by_profile[profile_key] = session
            self._profile_by_account[account_id] = profile_key

    def unregister(self, account_id: str) -> None:
        profile_key = self._profile_by_account.pop(account_id, None)
        if not profile_key:
            return
        with self._lock:
            self._by_profile.pop(profile_key, None)

    def request_cancel(self, account_id: str) -> bool:
        """API cancel 调用 —— set cancel event 让 runner 自然 close context。

        返回 True = 已发出 cancel 信号(无论窗口此刻是否真在关)。
        """
        profile_key = self._profile_by_account.get(account_id)
        if not profile_key:
            return False
        with self._lock:
            session = self._by_profile.get(profile_key)
            if session is None:
                return False
            session.cancel.set()
            return True

    def shutdown(self) -> None:
        """App 退出时 cancel 所有打开的窗口。"""
        with self._lock:
            sessions = list(self._by_profile.values())
        for session in sessions:
            session.cancel.set()
        for session in sessions:
            session.thread.join(timeout=5)


class BrowserAlreadyOpen(RuntimeError):
    pass


# 全局单例 —— FastAPI 进程内一个就够了,不用 DI。
_REGISTRY = BrowserSessionsRegistry()


def get_browser_sessions_registry() -> BrowserSessionsRegistry:
    return _REGISTRY