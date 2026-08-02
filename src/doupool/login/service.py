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


_ACCOUNT_INFO_URL = "https://www.doubao.com/passport/web/account/info/"

_LOG = logging.getLogger("doupool.login")


def _verify_from_disk(profile_dir: Path) -> Mapping[str, str | None] | None:
    """
    v0.2.5 cookie-on-disk fallback。

    真实场景(用户 v0.2.4 日志 11:38:27):
        1. 豆包扫码成功 → cookie 已写到 Chromium 持久 profile
        2. context 同毫秒 dispose → 所有 Playwright API 全部失效
        3. wait_for_identity 抢救一次到 profile_dir/cookies.json
        4. service 层读 cookies.json + httpx 直接调 account/info

    不依赖 Playwright,只依赖 httpx;cookies.json 已经救到磁盘上就够。
    """
    cookies_file = profile_dir / "cookies.json"
    if not cookies_file.exists():
        return None
    try:
        cookies_list = json.loads(cookies_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        _LOG.warning("读 cookies.json 失败(%s):%s", cookies_file, exc)
        return None
    if not isinstance(cookies_list, list):
        return None
    cookie_header = "; ".join(
        f"{c.get('name')}={c.get('value')}"
        for c in cookies_list
        if c.get("name") and c.get("value") and "doubao.com" in (c.get("domain") or "")
    )
    if not cookie_header:
        return None
    try:
        import httpx
    except ImportError:
        _LOG.warning("httpx 不可用,无法走 disk fallback")
        return None
    try:
        resp = httpx.get(
            _ACCOUNT_INFO_URL,
            headers={
                "Cookie": cookie_header,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/151.0.0.0 Safari/537.36"
                ),
                "Accept": "*/*",
                "Referer": "https://www.doubao.com/",
            },
            timeout=10.0,
            follow_redirects=False,
        )
    except Exception as exc:
        _LOG.warning("disk fallback httpx 失败:%s", exc)
        return None
    if resp.status_code != 200:
        _LOG.warning("disk fallback account/info 返回 status=%s", resp.status_code)
        return None
    try:
        payload = resp.json()
    except Exception as exc:
        _LOG.warning("disk fallback 响应不是 JSON:%s", exc)
        return None
    if not isinstance(payload, dict) or payload.get("code") not in (0, None):
        _LOG.info("disk fallback account/info code=%s(非登录态)", payload.get("code") if isinstance(payload, dict) else None)
        return None
    data = payload.get("data") or {}
    user = data.get("user") if isinstance(data.get("user"), dict) else data
    user_id = user.get("user_id") or user.get("user_id_str") or user.get("sec_user_id") or data.get("user_id") or data.get("id")
    if not user_id:
        _LOG.info("disk fallback account/info 无 user_id")
        return None
    nickname = user.get("name") or user.get("screen_name") or data.get("name")
    _LOG.info("disk fallback 命中登录 user_id=%s nickname=%s", user_id, nickname)
    return {"user_id": str(user_id), "nickname": str(nickname) if nickname else None}


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
        # v0.2.5 改:profile_dir 改成持久路径,保留 cookie 抢救文件供 disk fallback。
        # 旧版 .tmp 临时目录每次结束都 rmtree,v0.2.4 场景下 cookie 在抢救后被删,
        # 软件始终读不到磁盘上的 doubao 登录态。
        persistent_dir = self.profiles_dir / "login" / attempt_id
        persistent_dir.mkdir(parents=True, exist_ok=True)

        def emit_from_thread(state: str, message: str) -> None:
            loop.call_soon_threadsafe(self._emit, attempt_id, state, message)

        # Playwright runner 跑在独立线程里。asyncio.wait_for 只能取消 await,
        # 不能真打断阻塞中的 browser。所以 timeout/shutdown 走两条路:
        # 1) 先 set cancel_event 让 runner 自己退出
        # 2) 通过 Future 拿到 runner 的执行线程,等线程真的 join 完
        #    才清理(不再 rmtree,保留 cookies.json 供 disk fallback)
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
            # 1) 通知 runner 自己退出
            cancellation.set()
            # 2) 等 runner 线程真结束(Playwright context.close() 跑完)
            self._await_runner_thread(runner_thread, runner_future)
            self._emit(attempt_id, "timed_out", "扫码登录已超时")
            # v0.2.5 不再 rmtree:保留 cookies.json 方便后续手动排查
        except asyncio.CancelledError:
            # 整个应用被 shutdown / 任务被取消,让 runner 自己也退
            cancellation.set()
            self._await_runner_thread(runner_thread, runner_future)
            self._emit(attempt_id, "cancelled", "登录已取消")
            raise
        except Exception as exc:
            # v0.2.5 新增:Playwright 路径全失败的兜底。
            # 错误是 "登录窗口已关闭" / "Target page...closed" / "Request context
            # disposed" 这一类 → 可能是 context 已经 dispose,但 cookie 早就写到
            # 持久 profile 里,wait_for_identity 抢救过 cookies.json。尝试用
            # httpx 直接调 account/info 兜底。
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
