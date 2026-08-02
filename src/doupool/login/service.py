from __future__ import annotations

import asyncio
import logging
import shutil
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from doupool.db.repository import AccountRepository

from .state import LoginState, LoginStateMachine, TERMINAL_STATES


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
        temp_dir = self.profiles_dir / ".tmp" / attempt_id
        temp_dir.mkdir(parents=True, exist_ok=True)

        def emit_from_thread(state: str, message: str) -> None:
            loop.call_soon_threadsafe(self._emit, attempt_id, state, message)

        # Playwright runner 跑在独立线程里。asyncio.wait_for 只能取消 await,
        # 不能真打断阻塞中的 browser。所以 timeout/shutdown 走两条路:
        # 1) 先 set cancel_event 让 runner 自己退出
        # 2) 通过 Future 拿到 runner 的执行线程,等线程真的 join 完
        #    才 rmtree 临时 profile 目录,避免 Chromium 还在读时被删。
        runner_future: asyncio.Future[VerifiedLogin] = loop.create_future()
        cancellation = self._cancellations[attempt_id]

        def _runner_wrapper() -> None:
            try:
                result = self.runner.run(
                    attempt_id,
                    temp_dir,
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
            # 1) 通知 runner 自己退出
            cancellation.set()
            # 2) 等 runner 线程真结束(Playwright context.close() 跑完)
            self._await_runner_thread(runner_thread, runner_future)
            self._emit(attempt_id, "timed_out", "扫码登录已超时")
            shutil.rmtree(temp_dir, ignore_errors=True)
        except asyncio.CancelledError:
            # 整个应用被 shutdown / 任务被取消,让 runner 自己也退
            cancellation.set()
            self._await_runner_thread(runner_thread, runner_future)
            self._emit(attempt_id, "cancelled", "登录已取消")
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise
        except Exception as exc:
            state = "cancelled" if cancellation.is_set() else "failed"
            message = "登录已取消" if state == "cancelled" else f"登录失败：{exc}"
            self._emit(attempt_id, state, message)
            # 普通失败 runner 已经退出,但保险起见还是 join 一下
            self._await_runner_thread(runner_thread, runner_future, timeout=5)
            shutil.rmtree(temp_dir, ignore_errors=True)
        finally:
            self._active_attempt_id = None

    @staticmethod
    def _await_runner_thread(
        thread: threading.Thread,
        future: asyncio.Future,
        timeout: float = 30.0,
    ) -> None:
        """
        等 runner 线程真结束。Playwright 在 sync API 里阻塞,如果我们不等它
        就 rmtree profile_dir,Chromium 会拿已删的 Cookie 文件继续读写,报
        'WebContentsDelegate: Permission denied' 或随机崩溃。
        """
        # 给 runner 一点时间响应 cancel_event
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
