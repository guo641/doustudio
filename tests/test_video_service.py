import asyncio

import pytest

from doupool.db.models import Account
from doupool.video.protocol import DoubaoRateLimited
from doupool.video.service import VideoTaskService


class SuccessfulVideoRunner:
    def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
        update(status="generating", conversation_id="conversation-1")
        return {
            "remote_task_id": "remote-1",
            "result_url": "https://example.test/video.mp4",
            "cover_url": "https://example.test/cover.jpg",
        }


class StaticSettings:
    def get(self):
        return {"daily_quota": 5, "quota_reset_time": "00:00", "max_concurrency": 1}


@pytest.mark.asyncio
async def test_service_runs_and_persists_video_result(repository, temp_profile):
    Account.create(
        id="account-1", display_name="测试账号", doubao_user_id="user-1", profile_dir=temp_profile
    )
    service = VideoTaskService(repository, SuccessfulVideoRunner(), StaticSettings(), account_poll_interval=0.01)

    task = service.start("一只猫在草地上行走", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    assert saved.status == "succeeded"
    assert saved.conversation_id == "conversation-1"
    assert saved.remote_task_id == "remote-1"
    assert Account.get_by_id("account-1").video_quota_used == 1


@pytest.mark.asyncio
async def test_service_keeps_task_queued_without_an_available_account(repository):
    service = VideoTaskService(repository, SuccessfulVideoRunner(), StaticSettings(), account_poll_interval=0.01)
    task = service.start("测试", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.sleep(0.03)
    assert repository.get_video_task(task.id).status == "queued"
    await service.shutdown()


class FailoverRunner:
    def __init__(self):
        self.calls = []

    def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
        self.calls.append(str(profile_dir))
        if len(self.calls) == 1:
            raise DoubaoRateLimited("rate limited")
        update(status="generating", conversation_id="conversation-2")
        return {"remote_task_id":"remote-2", "result_url":"https://example.test/2.mp4"}


@pytest.mark.asyncio
async def test_service_cools_limited_account_and_fails_over(repository, tmp_path):
    first = tmp_path / "first"; first.mkdir()
    second = tmp_path / "second"; second.mkdir()
    Account.create(id="account-1", display_name="账号一", doubao_user_id="user-1", profile_dir=str(first))
    Account.create(id="account-2", display_name="账号二", doubao_user_id="user-2", profile_dir=str(second))
    runner = FailoverRunner()
    service = VideoTaskService(repository, runner, StaticSettings(), account_poll_interval=0.01)

    task = service.start("测试换号", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    assert saved.status == "succeeded"
    assert saved.account.id == "account-2"
    assert Account.get_by_id("account-1").video_quota_used == 5
    assert Account.get_by_id("account-1").video_limited_until is not None


@pytest.mark.asyncio
async def test_service_resumes_persisted_queued_tasks(repository, temp_profile):
    Account.create(id="resume-account", display_name="恢复账号", doubao_user_id="resume-user", profile_dir=temp_profile)
    task = repository.create_video_task(None, "恢复任务", "seedance_v2.0_mini", "1:1", 5)
    service = VideoTaskService(repository, SuccessfulVideoRunner(), StaticSettings(), account_poll_interval=0.01)

    await service.resume_queued()
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    assert repository.get_video_task(task.id).status == "succeeded"


class CapturingRunner:
    def __init__(self):
        self.kwargs = None

    def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
        self.kwargs = kwargs
        update(status="generating", conversation_id="conversation-i2v")
        return {"remote_task_id": "remote-i2v", "result_url": "https://example.test/i2v.mp4"}


@pytest.mark.asyncio
async def test_service_persists_and_runs_i2v_task(repository, temp_profile, tmp_path):
    Account.create(
        id="account-i2v", display_name="图生账号", doubao_user_id="user-i2v", profile_dir=temp_profile
    )
    runner = CapturingRunner()
    service = VideoTaskService(
        repository, runner, StaticSettings(), account_poll_interval=0.01, assets_dir=tmp_path
    )
    # 1x1 png
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )

    task = service.start(
        "动起来",
        "seedance_v2.0_mini",
        "9:16",
        5,
        mode="i2v",
        images=[{"name": "demo.png", "data_base64": png_b64}],
    )
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    assert saved.status == "succeeded"
    assert saved.mode == "i2v"
    assert saved.image_paths and "demo.png" in saved.image_paths
    assert runner.kwargs["mode"] == "i2v"
    assert runner.kwargs["image_paths"] and runner.kwargs["image_paths"][0].endswith("demo.png")


@pytest.mark.asyncio
async def test_service_accepts_up_to_nine_i2v_images(repository, temp_profile, tmp_path):
    Account.create(
        id="account-multi", display_name="多图账号", doubao_user_id="user-multi", profile_dir=temp_profile
    )
    runner = CapturingRunner()
    service = VideoTaskService(
        repository, runner, StaticSettings(), account_poll_interval=0.01, assets_dir=tmp_path
    )
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    images = [{"name": f"demo-{i}.png", "data_base64": png_b64} for i in range(9)]

    task = service.start(
        "九图", "seedance_v2.0_mini", "1:1", 5, mode="i2v", images=images
    )
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    assert saved.status == "succeeded"
    assert len(runner.kwargs["image_paths"]) == 9


def test_service_rejects_ten_i2v_images(repository, tmp_path):
    service = VideoTaskService(
        repository, CapturingRunner(), StaticSettings(), assets_dir=tmp_path
    )
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    images = [{"name": f"demo-{i}.png", "data_base64": png_b64} for i in range(10)]
    with pytest.raises(ValueError, match="9"):
        service.start("超限", "seedance_v2.0_mini", "1:1", 5, mode="i2v", images=images)
