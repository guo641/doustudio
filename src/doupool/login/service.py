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


def _identity_from_account_info_payload(payload: object) -> Mapping[str, str | None] | None:
    """
    从浏览器内 fetch /passport/web/account/info/ 的响应 JSON 提取 identity。
    三种结构都支持(doubao 不同接口/版本可能略有差异):
    1. {"code":0,"data":{"user":{"user_id":"X","name":"Y"}}}
    2. {"code":0,"data":{"user_id":"X","name":"Y"}}
    3. {"data":{"user_id":"X","name":"Y"}}
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("code") not in (0, None):
        return None
    data = payload.get("data") or {}
    user = data.get("user") if isinstance(data.get("user"), dict) else data
    if not isinstance(user, dict):
        return None
    user_id = (
        user.get("user_id")
        or user.get("user_id_str")
        or user.get("sec_user_id")
        or data.get("user_id")
        or data.get("id")
    )
    if not user_id:
        return None
    nickname = user.get("name") or user.get("screen_name") or data.get("name")
    return {"user_id": str(user_id), "nickname": str(nickname) if nickname else None}


def _verify_via_persistent_context(
    profile_dir: Path,
) -> Mapping[str, str | None] | None:
    """
    v0.2.6 第二道保险:用同一份 profile_dir 重新起一次 Playwright
    persistent context,让 Chromium 进程自身加载 user_data_dir 内
    cookies(若磁盘 SQLite 还没被 flush,会用 v0.2.5 的 cookies.json
    通过 storage_state 注入),然后通过 context.request.get
    /passport/web/account/info/ 验证。

    直接对标 MediaCrawler / f2 模式 —— Chromium 进程发请求天然继承
    TLS JA3 / UA / Sec-Ch-Ua / Cookie 时效,这是 v0.2.5 httpx 路径
    失败的根本原因(API_RESEARCH 第 5 条社区共识:httpx JA3 跟 Chromium
    不一样,触发服务端 1011)。
    """
    try:
        from playwright.sync_api import (
            Error as PlaywrightError,
            sync_playwright,
        )
    except ImportError:
        _LOG.warning("in-browser verify: playwright 不可用,跳过")
        return None
    if not profile_dir.exists():
        return None
    storage_state_path = profile_dir / "storage_state.json"
    headless_args = ["--disable-gpu", "--no-sandbox"]
    try:
        with sync_playwright() as p:
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                headless=True,
                args=headless_args,
                storage_state=(
                    str(storage_state_path) if storage_state_path.exists() else None
                ),
            )
            try:
                response = context.request.get(_ACCOUNT_INFO_URL, timeout=10.0)
                if response.status_code != 200:
                    _LOG.warning(
                        "in-browser verify: account/info 返回 status=%s",
                        response.status_code,
                    )
                    return None
                payload = response.json()
            except PlaywrightError as exc:
                _LOG.warning("in-browser verify: request 失败: %s", exc)
                return None
            except Exception as exc:
                _LOG.warning("in-browser verify: 异常: %s", exc)
                return None
            finally:
                try:
                    context.close()
                except PlaywrightError:
                    pass
    except PlaywrightError as exc:
        _LOG.warning("in-browser verify: 启动 Chromium 失败: %s", exc)
        return None
    return _identity_from_account_info_payload(
        payload if "payload" in locals() else None
    )


def _verify_from_disk(profile_dir: Path) -> Mapping[str, str | None] | None:
    """
    v0.2.6 disk fallback。两条路,优先级递减:

    **路径 1:account_info.json(浏览器内 fetch 的真实响应)**
        browser.py 在 Playwright context 还活着时,通过 page.evaluate(fetch)
        主动调 /passport/web/account/info/ 并把响应写到 profile_dir/account_info.json。
        这是最权威的兜底 —— 在浏览器进程内 fetch 自带浏览器指纹 + HttpOnly
        cookie + sec-ch-ua,能过 aegis 风控(v0.2.5 disk fallback 用 httpx 调
        account/info 始终返回 1011 用户未登录,根因正是被 aegis 拒)。

    **路径 2:cookies.json + httpx(老方案,仅作兜底)**
        仅在 account_info.json 不存在时使用。aegis 风控可能拒,可能成功,
        但不是首选 —— 用户实际命中路径 1 已经能解决。

    不依赖 Playwright 运行中,只依赖磁盘上抢救出来的文件。
    """
    # 路径 1:account_info.json —— 浏览器内 fetch 的真实响应
    account_info_file = profile_dir / "account_info.json"
    if account_info_file.exists():
        try:
            payload = json.loads(account_info_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning("读 account_info.json 失败(%s):%s", account_info_file, exc)
        else:
            identity = _identity_from_account_info_payload(payload)
            if identity:
                _LOG.info("disk fallback(浏览器内 fetch)命中登录 user_id=%s nickname=%s",
                          identity["user_id"], identity.get("nickname"))
                return identity
            _LOG.info("disk fallback account_info.json 不是登录态")
    # 路径 2:cookies.json + httpx —— 老兜底,aegis 风控可能拒
    cookies_file = profile_dir / "cookies.json"
    if cookies_file.exists():
        try:
            cookies_list = json.loads(cookies_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _LOG.warning("读 cookies.json 失败(%s):%s", cookies_file, exc)
            cookies_list = None
        if isinstance(cookies_list, list):
            cookie_header = "; ".join(
                f"{c.get('name')}={c.get('value')}"
                for c in cookies_list
                if c.get("name") and c.get("value")
                and "doubao.com" in (c.get("domain") or "")
            )
            if cookie_header:
                try:
                    import httpx
                except ImportError:
                    _LOG.warning("httpx 不可用,跳过 httpx fallback")
                else:
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
                    else:
                        if resp.status_code == 200:
                            try:
                                payload = resp.json()
                            except Exception as exc:
                                _LOG.warning("disk fallback 响应不是 JSON:%s", exc)
                            else:
                                identity = _identity_from_account_info_payload(payload)
                                if identity:
                                    _LOG.info("disk fallback(httpx)命中登录 user_id=%s nickname=%s",
                                              identity["user_id"], identity.get("nickname"))
                                    return identity
                                _LOG.info("disk fallback account/info 无 user_id(可能被 aegis 风控拒)")
                        else:
                            _LOG.warning("disk fallback account/info 返回 status=%s",
                                         resp.status_code)
    # 路径 3:复用 profile_dir 重新起 Chromium —— 字节系 aegis 风控 httpx 几乎
    # 必拒,但 Chromium 进程自身发请求继承 TLS JA3 / UA / sec-ch-ua,能过。
    # 这是 v0.2.5 disk fallback httpx 始终 1011 的根本修复。代价是重新起一次
    # Chromium(~2 秒),失败也不影响其他路径。
    identity = _verify_via_persistent_context(profile_dir)
    if identity:
        return identity
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
