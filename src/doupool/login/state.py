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

