from __future__ import annotations

import asyncio
import json
import logging
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from doupool.db.repository import AccountRepository

from .state import LoginState, LoginStateMachine, TERMINAL_STATES


def _extract_user_id_from_cookies(cookies: list[dict]) -> str | None:
    """从 doubao.com 域 cookie 列表中按 hint 顺序找 user_id。

    service 层使用副本,避免与 doupool.login.browser 形成循环 import
    (browser.py 又依赖 service 的 VerifiedLogin)。
    """
    for hint in ("user_unique_id", "user_id", "uid"):
        for c in cookies:
            if c.get("name") == hint and c.get("value"):
                return str(c["value"])
    return None


class LoginAlreadyRunning(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LoginEvent:
    attempt_id: str
    state: str
    message: str


@dataclass(frozen=True, slots=True)
class VerifiedLogin:
    identity: Mapping[str, str | None]
    profile_dir: str


_LOG = logging.getLogger("doupool.login")


def _verify_from_disk(profile_dir: Path) -> Mapping[str, str | None] | None:
    """v0.2.7 disk fallback:单路径,完全不调 /passport/web/account/info/。

    优先级:
      1. profile_dir/identity.json —— browser.py 在 context 死之前通过
         page.evaluate 读 localStorage.__tea_cache_tokens_497858.user_unique_id
         写下的。最权威(Chromium 进程自己读,继承完整浏览器指纹,无 aegis 风险)。
      2. profile_dir/cookies.json 里 user_unique_id / user_id / uid cookie 兜底
         (字节通常不把 user_id 放进 cookie,命中率低,留作最后一道防线)。

    v0.2.6 之前的三条路径(浏览器内 fetch account_info.json / httpx 重发 /
    重启 Chromium)全部下线 —— 字节系 aegis 风控把所有非浏览器指纹请求拒为
    1011(用户未登录),唯一权威判定只能在 Chromium 自己进程内做(已合并到
    browser.py:wait_for_identity 主循环)。
    """
    id_file = profile_dir / "identity.json"
    if id_file.exists():
        try:
            payload = json.loads(id_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning("读 identity.json 失败(%s):%s", id_file, exc)
        else:
            if isinstance(payload, dict) and payload.get("user_id"):
                uid = str(payload["user_id"])
                _LOG.info("disk fallback(identity.json)命中 user_id=%s", uid)
                return {"user_id": uid, "nickname": None}
            _LOG.info("disk fallback identity.json 不是登录态,fallback 到 cookies.json")

    cookies_file = profile_dir / "cookies.json"
    if cookies_file.exists():
        try:
            cookies = json.loads(cookies_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning("读 cookies.json 失败(%s):%s", cookies_file, exc)
            return None
        if isinstance(cookies, list):
            uid = _extract_user_id_from_cookies(cookies)
            if uid:
                _LOG.info("disk fallback(cookies)命中 user_id=%s", uid)
                return {"user_id": uid, "nickname": None}
    return None


class LoginRunner(Protocol):
    def run(
        self,
        attempt_id: str,
        profile_dir: Path,
        emit: Callable[[str, str], None],
        cancel_event: threading.Event,
    ) -> VerifiedLogin: ...


class LoginService:
    def __init__(
        self,
        repository: AccountRepository,
        runner: LoginRunner,
        profiles_dir: Path,
        timeout: float = 300,
    ):
        self.repository = repository
        self.runner = runner
        self.profiles_dir = Path(profiles_dir)
        self.timeout = timeout
        self._active_attempt_id: str | None = None
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._queues: dict[str, asyncio.Queue[LoginEvent]] = {}
        self._cancellations: dict[str, threading.Event] = {}
        self._machines: dict[str, LoginStateMachine] = {}

    def start(self, account_id: str | None = None):
        if self._active_attempt_id is not None:
            raise LoginAlreadyRunning("已有登录窗口正在运行")
        attempt = self.repository.create_login_attempt(account_id)
        self._active_attempt_id = attempt.id
        self._queues[attempt.id] = asyncio.Queue()
        self._cancellations[attempt.id] = threading.Event()
        self._machines[attempt.id] = LoginStateMachine(LoginState.CREATED)
        self._tasks[attempt.id] = asyncio.create_task(self._run(attempt.id))
        return attempt

    def _emit(self, attempt_id: str, state_value: str, message: str) -> None:
        state = LoginState(state_value)
        machine = self._machines[attempt_id]
        if machine.state != state:
            machine.transition(state)
        self.repository.set_attempt_state(attempt_id, state.value)
        self._queues[attempt_id].put_nowait(LoginEvent(attempt_id, state.value, message))

    async def _run(self, attempt_id: str) -> None:
        loop = asyncio.get_running_loop()
        persistent_dir = self.profiles_dir / "login" / attempt_id
        persistent_dir.mkdir(parents=True, exist_ok=True)

        def emit_from_thread(state: str, message: str) -> None:
            loop.call_soon_threadsafe(self._emit, attempt_id, state, message)

        runner_future: asyncio.Future[VerifiedLogin] = loop.create_future()
        cancellation = self._cancellations[attempt_id]

        def _runner_wrapper() -> None:
            try:
                result = self.runner.run(
                    attempt_id,
                    persistent_dir,
                    emit_from_thread,
                    cancellation,
                )
                loop.call_soon_threadsafe(runner_future.set_result, result)
            except BaseException as exc:  # noqa: BLE001 - 跨线程桥接
                loop.call_soon_threadsafe(runner_future.set_exception, exc)

        runner_thread = threading.Thread(
            target=_runner_wrapper,
            name=f"login-runner-{attempt_id}",
            daemon=True,
        )
        runner_thread.start()

        try:
            self._emit(attempt_id, "launching", "正在启动豆包登录窗口")
            result = await asyncio.wait_for(
                asyncio.shield(runner_future),
                timeout=self.timeout,
            )
            if self._machines[attempt_id].state is LoginState.WAITING_FOR_SCAN:
                self._emit(attempt_id, "verifying", "正在验证账号")
            self.repository.complete_login(attempt_id, result.identity, result.profile_dir)
            self._machines[attempt_id].transition(LoginState.SUCCEEDED)
            self._queues[attempt_id].put_nowait(
                LoginEvent(attempt_id, "succeeded", "登录成功")
            )
        except (TimeoutError, asyncio.TimeoutError):
            cancellation.set()
            self._await_runner_thread(runner_thread, runner_future)
            self._emit(attempt_id, "timed_out", "扫码登录已超时")
        except asyncio.CancelledError:
            cancellation.set()
            self._await_runner_thread(runner_thread, runner_future)
            self._emit(attempt_id, "cancelled", "登录已取消")
            raise
        except Exception as exc:
            # v0.2.7:Playwright 路径全失败 → disk fallback。identity.json 是
            # browser.py 在 context 死之前通过 page.evaluate 抢救的 localStorage
            # user_unique_id,权威性等同 Chromium 进程内判定。
            state = "cancelled" if cancellation.is_set() else "failed"
            message = "登录已取消" if state == "cancelled" else f"登录失败:{exc}"
            self._await_runner_thread(runner_thread, runner_future, timeout=5)
            identity = _verify_from_disk(persistent_dir)
            if identity:
                _LOG.info("disk fallback 命中,登录成功 attempt=%s", attempt_id)
                self.repository.complete_login(attempt_id, identity, str(persistent_dir))
                self._machines[attempt_id].transition(LoginState.SUCCEEDED)
                self._queues[attempt_id].put_nowait(
                    LoginEvent(attempt_id, "succeeded", "登录成功")
                )
                return
            self._emit(attempt_id, state, message)
        finally:
            self._active_attempt_id = None

    @staticmethod
    def _await_runner_thread(
        thread: threading.Thread,
        future: asyncio.Future,
        timeout: float = 30.0,
    ) -> None:
        """等 runner 线程真结束。Playwright 在 sync API 里阻塞,如果我们不等它
        就 rmtree profile_dir,Chromium 会拿已删的 Cookie 文件继续读写,报
        'WebContentsDelegate: Permission denied' 或随机崩溃。"""
        thread.join(timeout=timeout)
        if thread.is_alive():
            logging.getLogger("doupool.login").warning(
                "login runner thread 未能在 %s 秒内退出,继续清理临时目录", timeout
            )

    async def events(self, attempt_id: str) -> AsyncIterator[LoginEvent]:
        queue = self._queues[attempt_id]
        while True:
            event = await queue.get()
            yield event
            if LoginState(event.state) in TERMINAL_STATES:
                break

    async def cancel(self, attempt_id: str) -> None:
        cancellation = self._cancellations.get(attempt_id)
        if cancellation:
            cancellation.set()

    async def wait(self, attempt_id: str) -> None:
        task = self._tasks.get(attempt_id)
        if task:
            await task

    async def shutdown(self) -> None:
        for cancellation in self._cancellations.values():
            cancellation.set()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)