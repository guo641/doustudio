import pytest

from doupool.login.state import InvalidLoginTransition, LoginState, LoginStateMachine


def test_rejects_invalid_transition():
    machine = LoginStateMachine(LoginState.CREATED)
    with pytest.raises(InvalidLoginTransition):
        machine.transition(LoginState.SUCCEEDED)


def test_allows_normal_scan_login_path():
    machine = LoginStateMachine(LoginState.CREATED)
    for state in (
        LoginState.LAUNCHING,
        LoginState.WAITING_FOR_SCAN,
        LoginState.VERIFYING,
        LoginState.SUCCEEDED,
    ):
        machine.transition(state)
    assert machine.state is LoginState.SUCCEEDED


def test_keepalive_round_trip_after_succeeded():
    """v0.2.20:扫码成功 → KEEPALIVE(等用户在浏览器里访问主页)→ SUCCEEDED。"""
    machine = LoginStateMachine(LoginState.CREATED)
    for state in (
        LoginState.LAUNCHING,
        LoginState.WAITING_FOR_SCAN,
        LoginState.VERIFYING,
        LoginState.SUCCEEDED,
        LoginState.KEEPALIVE,
        LoginState.SUCCEEDED,
    ):
        machine.transition(state)
    assert machine.state is LoginState.SUCCEEDED


def test_keepalive_can_be_cancelled():
    """v0.2.20:用户在 30s keepalive 内点取消 → KEEPALIVE → CANCELLED。"""
    machine = LoginStateMachine(LoginState.SUCCEEDED)
    machine.transition(LoginState.KEEPALIVE)
    machine.transition(LoginState.CANCELLED)
    assert machine.state is LoginState.CANCELLED


def test_keepalive_cannot_skip_verifying():
    """v0.2.20:KEEPALIVE 必须从 SUCCEEDED 转,不能从 WAITING_FOR_SCAN 直接跳。"""
    machine = LoginStateMachine(LoginState.WAITING_FOR_SCAN)
    with pytest.raises(InvalidLoginTransition):
        machine.transition(LoginState.KEEPALIVE)

