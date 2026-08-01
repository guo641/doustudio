import asyncio

import pytest

from doupool.db.models import Account
from doupool.login.service import LoginAlreadyRunning, LoginService, VerifiedLogin


class SuccessfulRunner:
    def run(self, attempt_id, profile_dir, emit, cancel_event):
        emit("waiting_for_scan", "等待扫码")
        emit("verifying", "正在验证")
        return VerifiedLogin({"user_id": "u-1", "nickname": "莲韵"}, str(profile_dir))


class BlockingRunner:
    def run(self, attempt_id, profile_dir, emit, cancel_event):
        emit("waiting_for_scan", "等待扫码")
        cancel_event.wait(2)
        raise RuntimeError("cancelled")


@pytest.mark.asyncio
async def test_successful_login_emits_terminal_event(repository, tmp_path):
    service = LoginService(repository, SuccessfulRunner(), tmp_path / "profiles", timeout=2)
    attempt = service.start()
    states = []
    async for event in service.events(attempt.id):
        states.append(event.state)
    await service.wait(attempt.id)

    assert states[-1] == "succeeded"
    assert Account.get().doubao_user_id == "u-1"


@pytest.mark.asyncio
async def test_only_one_interactive_login(repository, tmp_path):
    service = LoginService(repository, BlockingRunner(), tmp_path / "profiles", timeout=2)
    first = service.start()
    await asyncio.sleep(0.05)
    with pytest.raises(LoginAlreadyRunning):
        service.start()
    await service.cancel(first.id)
    await service.wait(first.id)
