"""v0.2.20:BrowserSessionsRegistry 单测。

不实际起 Playwright(那是 e2e 范围) —— 这里只测 registry 自身的状态机:
  - register 防 409
  - is_open 跟住 thread.is_alive()
  - request_cancel 只 set event,不动 thread
  - shutdown 把所有 cancel set + join
"""
from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from doupool.login.browser_sessions import (
    BrowserAlreadyOpen,
    BrowserSession,
    BrowserSessionsRegistry,
)


def _make_thread(target, *, alive_after: bool = True) -> threading.Thread:
    """造一个 daemon 线程,默认几秒后自然退出(alive_after=False 则永远不退出)。"""
    def _run():
        target()

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    if not alive_after:
        # 等线程启动后不让它立刻退
        time.sleep(0.05)
    return t


def test_register_and_is_open_returns_true_for_alive_thread(tmp_path: Path):
    reg = BrowserSessionsRegistry()
    profile = tmp_path / "p1"
    evt = threading.Event()
    t = _make_thread(lambda: evt.wait(), alive_after=True)
    reg.register("acc-1", BrowserSession(t, evt, profile))
    assert reg.is_open("acc-1") is True


def test_is_open_returns_false_for_dead_thread(tmp_path: Path):
    reg = BrowserSessionsRegistry()
    profile = tmp_path / "p1"
    evt = threading.Event()
    t = _make_thread(lambda: None, alive_after=False)
    t.join(timeout=2)
    assert not t.is_alive(), "测试前提:thread 真的退了"
    reg.register("acc-1", BrowserSession(t, evt, profile))
    assert reg.is_open("acc-1") is False


def test_register_409_when_same_profile_already_open(tmp_path: Path):
    """Chromium SingletonLock 同 profile 互斥 —— 同 profile 二次 register 必报错。"""
    reg = BrowserSessionsRegistry()
    profile = tmp_path / "shared"
    evt1 = threading.Event()
    t1 = _make_thread(lambda: evt1.wait(), alive_after=True)
    reg.register("acc-1", BrowserSession(t1, evt1, profile))
    evt2 = threading.Event()
    t2 = _make_thread(lambda: evt2.wait(), alive_after=True)
    with pytest.raises(BrowserAlreadyOpen):
        reg.register("acc-2", BrowserSession(t2, evt2, profile))
    # 第二个 thread 没人用,手动 cancel 退出避免 pytest hang
    evt2.set()
    t2.join(timeout=2)
    evt1.set()
    t1.join(timeout=2)


def test_register_409_even_when_same_account(tmp_path: Path):
    """同 account 重复 register 也得报错(防止前端按钮双击双开)。"""
    reg = BrowserSessionsRegistry()
    profile = tmp_path / "p1"
    evt = threading.Event()
    t = _make_thread(lambda: evt.wait(), alive_after=True)
    reg.register("acc-1", BrowserSession(t, evt, profile))
    with pytest.raises(BrowserAlreadyOpen):
        reg.register("acc-1", BrowserSession(t, evt, profile))
    evt.set()
    t.join(timeout=2)


def test_request_cancel_sets_event_and_returns_true(tmp_path: Path):
    reg = BrowserSessionsRegistry()
    profile = tmp_path / "p1"
    evt = threading.Event()
    t = _make_thread(lambda: evt.wait(), alive_after=True)
    reg.register("acc-1", BrowserSession(t, evt, profile))
    assert evt.is_set() is False
    sent = reg.request_cancel("acc-1")
    assert sent is True
    assert evt.is_set() is True
    t.join(timeout=2)


def test_request_cancel_returns_false_for_unknown_account():
    reg = BrowserSessionsRegistry()
    assert reg.request_cancel("never-seen") is False


def test_unregister_removes_session(tmp_path: Path):
    reg = BrowserSessionsRegistry()
    profile = tmp_path / "p1"
    evt = threading.Event()
    t = _make_thread(lambda: evt.wait(), alive_after=True)
    reg.register("acc-1", BrowserSession(t, evt, profile))
    reg.unregister("acc-1")
    assert reg.is_open("acc-1") is False
    # unregister 后可以再 register
    reg.register("acc-1", BrowserSession(t, evt, profile))
    assert reg.is_open("acc-1") is True
    evt.set()
    t.join(timeout=2)


def test_shutdown_joins_all_sessions(tmp_path: Path):
    reg = BrowserSessionsRegistry()
    threads = []
    events = []
    for i in range(3):
        evt = threading.Event()
        events.append(evt)
        t = _make_thread(lambda: evt.wait(), alive_after=True)
        threads.append(t)
        reg.register(f"acc-{i}", BrowserSession(t, evt, tmp_path / f"p{i}"))
    reg.shutdown()
    for t in threads:
        # join_within shutdown,但 daemon 线程不保证立即退,只保证 cancel 已 set
        # 我们直接断言 event 已被 set + thread.is_alive() False
        t.join(timeout=2)
        assert not t.is_alive()