from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class LoginState(StrEnum):
    CREATED = "created"
    LAUNCHING = "launching"
    WAITING_FOR_SCAN = "waiting_for_scan"
    VERIFYING = "verifying"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    # v0.2.20:扫码成功后,留给用户 30 秒在那个浏览器窗口里访问
    # doubao.com/chat/ 生成 WebMSSDK token 的窗口。keepalive 结束后
    # 才进入 SUCCEEDED 终态。
    KEEPALIVE = "keepalive"
    # 历史中间态值保留，兼容旧数据库/事件回放；当前登录流程不再触发。
    CAPTCHA_SOLVING = "captcha_solving"


TERMINAL_STATES = {
    LoginState.SUCCEEDED,
    LoginState.FAILED,
    LoginState.CANCELLED,
    LoginState.TIMED_OUT,
}

ALLOWED = {
    LoginState.CREATED: {LoginState.LAUNCHING, LoginState.CANCELLED},
    LoginState.LAUNCHING: {
        LoginState.WAITING_FOR_SCAN,
        LoginState.FAILED,
        LoginState.CANCELLED,
        # 启动阶段超时 / 用户取消,直接落 terminal,避免出现
        # "login 卡在 launching 永远不结束"的状态机死锁。
        LoginState.TIMED_OUT,
    },
    LoginState.WAITING_FOR_SCAN: {
        LoginState.VERIFYING,
        LoginState.FAILED,
        LoginState.CANCELLED,
        LoginState.TIMED_OUT,
    },
    LoginState.VERIFYING: {
        LoginState.SUCCEEDED,
        LoginState.WAITING_FOR_SCAN,
        LoginState.FAILED,
        LoginState.CANCELLED,
        LoginState.TIMED_OUT,
        # 历史状态转移保留以兼容旧事件；当前登录流程不再进入该状态。
        LoginState.CAPTCHA_SOLVING,
    },
    # v0.2.20:SUCCEEDED 可以暂时转 KEEPALIVE(扫码成功但浏览器还在),
    # KEEPALIVE 必须回到 SUCCEEDED 才算终态。cancel / failed 也允许
    # 从 KEEPALIVE 离开(用户在 30s 内点取消)。
    LoginState.SUCCEEDED: {
        LoginState.KEEPALIVE,
        LoginState.FAILED,
        LoginState.CANCELLED,
    },
    LoginState.KEEPALIVE: {
        LoginState.SUCCEEDED,
        LoginState.FAILED,
        LoginState.CANCELLED,
        # 历史状态转移保留以兼容旧事件；当前 keepalive 不再触发该状态。
        LoginState.CAPTCHA_SOLVING,
    },
    # 历史状态的终态转移保留以兼容旧事件。
    LoginState.CAPTCHA_SOLVING: {
        LoginState.SUCCEEDED,
        LoginState.FAILED,
        LoginState.CANCELLED,
        # 历史成功回转移保留以兼容旧事件。
        LoginState.KEEPALIVE,
    },
}


class InvalidLoginTransition(ValueError):
    pass


@dataclass(slots=True)
class LoginStateMachine:
    state: LoginState

    def transition(self, next_state: LoginState) -> None:
        if next_state not in ALLOWED.get(self.state, set()):
            raise InvalidLoginTransition(f"cannot transition {self.state} -> {next_state}")
        self.state = next_state
