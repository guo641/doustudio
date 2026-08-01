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

