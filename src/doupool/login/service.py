from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from doupool.db.repository import AccountRepository

from .state import LoginState, LoginStateMachine, TERMINAL_STATES


# v0.2.8.1:sessionid 闸门常量副本 —— 不能从 .browser 导入,会与
# browser.py 顶部的 `from .service import VerifiedLogin` 形成循环 import。
# 这两份必须与 browser.py 同名常量保持同步:byteDance 改 sessionid 格式时,
# 改 browser.SESSIONID_VALUE_PATTERN 后,这里也要跟着改一行。
_SESSIONID_NAME_HINTS = ("sessionid", "sessionid_ss")
_SESSIONID_VALUE_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def _is_doubao_cookie(cookie: dict) -> bool:
    """对标 browser._is_doubao_cookie,小工具不足以拆公共模块。"""
    return "doubao.com" in (cookie.get("domain") or "")


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


def _has_valid_sessionid_in_cookies(cookies: list[dict]) -> bool:
    """v0.2.8.1:disk fallback 闸门。

    与 browser._has_valid_sessionid 同一规则,但接收纯 dict 列表
    (从 cookies.json 读出来的)而非 Playwright context。sessionid /
    sessionid_ss 必须 32-hex 才算登录,这是字节系唯一可信的登录凭证。
    tracking cookies(s_v_web_id/odin_tt/ttwid/n_mh)首访即下发,
    即便不是 cookie、即便出现在 cookies.json 里也不算登录。
    """
    for c in cookies:
        if not _is_doubao_cookie(c):
            continue
        name = c.get("name", "")
        value = c.get("value", "")
        if name in _SESSIONID_NAME_HINTS and _SESSIONID_VALUE_PATTERN.match(value or ""):
            return True
    return False


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
    """v0.2.7 + v0.2.8.1 disk fallback:加 sessionid 闸门。

    v0.2.7 的实现从 identity.json 拿到 user_id 就直接接受 —— 但 v0.2.7 救援
    路径会写入 tracking ID(首访 doubao.com 时 localStorage.__tea_cache_tokens_497858
    里 19 位 user_unique_id),这是 byteDance 全站通用 analytics token,与登录
    无关。v0.2.8.1 加 sessionid 闸门:cookies.json 必须有 32-hex sessionid 才
    算登录,tracking 态直接拒绝。

    优先级(全部要过 sessionid 闸门):
      1. 读 cookies.json → 必须有合法 sessionid(否则整路径拒绝,无论
         identity.json 写了什么)→ 否则 v0.2.7 写入的 tracking 会复活
      2. cookies.json + identity.json 都有 → 取 identity.json.user_id
      3. cookies.json 有合法 sessionid + cookies 有 user_unique_id 兜底
         cookie → 取 cookie hint(字节通常不写这种 cookie,命中率低)

    v0.2.6 之前的三条路径(浏览器内 fetch account_info.json / httpx 重发 /
    重启 Chromium)全部下线 —— 字节系 aegis 风控把所有非浏览器指纹请求拒为
    1011(用户未登录),唯一权威判定只能在 Chromium 自己进程内做(已合并到
    browser.py:wait_for_identity 主循环)。
    """
    cookies_file = profile_dir / "cookies.json"
    cookies: list[dict] = []
    if cookies_file.exists():
        try:
            cookies = json.loads(cookies_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning("读 cookies.json 失败(%s):%s", cookies_file, exc)
            return None
        if not isinstance(cookies, list):
            _LOG.warning("cookies.json 顶层不是 list,放弃 fallback")
            return None

    # v0.2.8.1:sessionid 闸门 —— tracking cookies 写在 cookies.json 里也算
    if not _has_valid_sessionid_in_cookies(cookies):
        _LOG.warning(
            "disk fallback: cookies.json 无合法 sessionid(32-hex),"
            "判定为 tracking / 未登录,拒绝 fallback"
        )
        return None

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