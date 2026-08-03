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
        return {
            "daily_quota_mini": 5, "daily_quota_v2": 5, "daily_quota_std": 5,
            "quota_reset_time": "00:00", "max_concurrency": 1,
        }

    def get_daily_quotas(self):
        return {"mini": 5, "v2": 5, "std": 5}


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
    assert Account.get_by_id("account-1").video_quota_used_mini == 1


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
    assert Account.get_by_id("account-1").video_quota_used_mini == 5
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


# ---------- v0.2.9 retry-result 端点 ----------

class RecheckRunner:
    """记录 recheck_result 调用,可配置返回。"""
    def __init__(self, return_value):
        self.return_value = return_value
        self.calls = []

    def run(self, *args, **kwargs):
        raise RuntimeError("normal run not used in retry-result tests")

    def recheck_result(self, profile_dir, conversation_id, update, cancel_event, **kwargs):
        self.calls.append({"profile_dir": str(profile_dir), "conversation_id": conversation_id})
        update(status="rechecking")
        if isinstance(self.return_value, Exception):
            raise self.return_value
        return self.return_value


@pytest.mark.asyncio
async def test_retry_result_rejects_missing_task(repository):
    service = VideoTaskService(repository, RecheckRunner(None), StaticSettings())
    with pytest.raises(ValueError, match="任务不存在"):
        await service.schedule_retry_result("does-not-exist")


@pytest.mark.asyncio
async def test_retry_result_rejects_task_without_conversation_id(repository, temp_profile):
    Account.create(
        id="acc-noconv", display_name="no conv", doubao_user_id="u", profile_dir=temp_profile
    )
    task = repository.create_video_task("acc-noconv", "测试", "seedance_v2.0_mini", "1:1", 5)
    # 没经过 generate,conversation_id 是 None
    service = VideoTaskService(repository, RecheckRunner(None), StaticSettings())
    with pytest.raises(ValueError, match="conversation_id"):
        await service.schedule_retry_result(task.id)


@pytest.mark.asyncio
async def test_retry_result_rejects_when_original_account_missing(repository, temp_profile):
    """v0.2.9:原账号被删 / profile_dir 不存在时,绝不能 fallback 到别的账号
    重解析(风控 session 跨账号会直接 1011)。"""
    import shutil

    Account.create(
        id="acc-gone", display_name="已删", doubao_user_id="u-gone", profile_dir=temp_profile
    )
    task = repository.create_video_task(
        "acc-gone", "测试", "seedance_v2.0_mini", "1:1", 5
    )
    task.conversation_id = "conversation-x"
    task.save()
    # 模拟"profile_dir 被删" —— 这是 aegis 风控能拒绝浏览器启动的常见原因,
    # 常见于用户手动清理磁盘 / 卸载重装。账号还在 DB 里但 Chromium 已没东西可读。
    shutil.rmtree(temp_profile, ignore_errors=True)
    service = VideoTaskService(repository, RecheckRunner(None), StaticSettings())
    with pytest.raises(ValueError, match="原账号不可用"):
        await service.schedule_retry_result(task.id)


@pytest.mark.asyncio
async def test_retry_result_refreshes_succeeded_task_without_charging_quota(
    repository, temp_profile
):
    """核心契约:重解析成功后,account.video_quota_used 不变(不二次扣豆包额度)。"""
    Account.create(
        id="acc-r", display_name="acc", doubao_user_id="u", profile_dir=temp_profile
    )
    account = Account.get_by_id("acc-r")
    account.video_quota_used_mini = 2  # 假装此前已扣过两次
    account.save()

    task = repository.create_video_task("acc-r", "测试", "seedance_v2.0_mini", "1:1", 5)
    task.conversation_id = "conversation-r"
    task.status = "succeeded"
    task.result_url = "https://old.example/video.mp4"
    task.save()

    new_result = {
        "result_url": "https://new.example/video.mp4",
        "cover_url": "https://new.example/cover.jpg",
    }
    runner = RecheckRunner(new_result)
    service = VideoTaskService(repository, runner, StaticSettings())

    returned = await service.schedule_retry_result(task.id)
    assert returned.id == task.id

    await asyncio.wait_for(service._retry_tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    assert saved.status == "succeeded"
    assert saved.result_url == "https://new.example/video.mp4"
    assert saved.cover_url == "https://new.example/cover.jpg"
    # 关键:不扣额度
    assert Account.get_by_id("acc-r").video_quota_used_mini == 2
    assert runner.calls and runner.calls[0]["conversation_id"] == "conversation-r"
    assert runner.calls[0]["profile_dir"] == temp_profile


@pytest.mark.asyncio
async def test_retry_result_records_timeout_when_runner_returns_none(repository, temp_profile):
    """recheck_result 返回 None(还在生成中)时,task 回退到 generating
    状态,留个 error_message 说明"重解析超时"——不要强行标 failed,
    万一老 succeeded 状态本来就有的旧 result_url 还在。"""
    Account.create(
        id="acc-timeout", display_name="t", doubao_user_id="u", profile_dir=temp_profile
    )
    task = repository.create_video_task(
        "acc-timeout", "t", "seedance_v2.0_mini", "1:1", 5
    )
    task.conversation_id = "conversation-t"
    task.save()

    runner = RecheckRunner(None)
    service = VideoTaskService(repository, runner, StaticSettings())

    await service.schedule_retry_result(task.id)
    await asyncio.wait_for(service._retry_tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    assert saved.status == "generating"
    assert "重解析超时" in saved.error_message


@pytest.mark.asyncio
async def test_retry_result_records_error_message_when_runner_raises(repository, temp_profile):
    """recheck_result 抛错(风控拒 / 网络挂 / browser crash)时,不强行改
    task 状态,只在 error_message 留痕,这样 succeeded 的旧 result_url
    还能让用户下载。"""
    Account.create(
        id="acc-raise", display_name="r", doubao_user_id="u", profile_dir=temp_profile
    )
    task = repository.create_video_task(
        "acc-raise", "x", "seedance_v2.0_mini", "1:1", 5
    )
    task.conversation_id = "conversation-x"
    task.status = "succeeded"
    task.result_url = "https://still-here.example/video.mp4"
    task.save()

    runner = RecheckRunner(RuntimeError("豆包结果接口返回 HTTP 1011"))
    service = VideoTaskService(repository, runner, StaticSettings())

    await service.schedule_retry_result(task.id)
    await asyncio.wait_for(service._retry_tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    # 状态保留 succeeded(没拿到新 result 不算失败)
    assert saved.status == "succeeded"
    assert saved.result_url == "https://still-here.example/video.mp4"
    assert "1011" in saved.error_message


@pytest.mark.asyncio
async def test_retry_result_rejects_concurrent_call(repository, temp_profile):
    """同一 task 已有 retry 在跑时,再调一次必须 409,避免双开浏览器打架。"""
    import threading

    Account.create(
        id="acc-conc", display_name="c", doubao_user_id="u", profile_dir=temp_profile
    )
    task = repository.create_video_task(
        "acc-conc", "x", "seedance_v2.0_mini", "1:1", 5
    )
    task.conversation_id = "conversation-c"
    task.save()

    # 用一个会永久 block 的事件来模拟"重解析还在跑"
    block = threading.Event()

    class BlockingRunner(RecheckRunner):
        def recheck_result(self, profile_dir, conversation_id, update, cancel_event, **kwargs):
            update(status="rechecking")
            block.wait(timeout=10)
            return None

    service = VideoTaskService(repository, BlockingRunner(None), StaticSettings())

    await service.schedule_retry_result(task.id)
    # 第一次已调度,第二次应该拒绝
    with pytest.raises(RuntimeError, match="已有 retry-result"):
        await service.schedule_retry_result(task.id)
    block.set()  # 让第一次收尾
    service._retry_tasks[task.id].cancel()


# ---------- v0.2.9 per-model quota 隔离 ----------

class MiniOnlyLimitedRunner:
    """v0.2.9:模型互不影响 — 跑 std 任务时,mini 桶满的账号应该仍可被选中。"""
    def __init__(self):
        self.calls = []

    def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
        self.calls.append(model)
        update(status="generating", conversation_id="conv-iso")
        return {
            "remote_task_id": "remote-iso",
            "result_url": "https://example.test/iso.mp4",
        }


@pytest.mark.asyncio
async def test_quota_per_model_isolation_in_repository(repository, temp_profile, tmp_path):
    """v0.2.9:不同 model 的 quota 独立计数 — repo 层硬契约。"""
    quotas = {"mini": 2, "v2": 2, "std": 2}
    # 账号 A:mini 桶用完,v2/std 桶全空
    Account.create(
        id="a", display_name="A", doubao_user_id="ua",
        profile_dir=str(tmp_path / "a"), video_quota_used_mini=2,
    )
    # 账号 B:所有桶都空
    Account.create(
        id="b", display_name="B", doubao_user_id="ub",
        profile_dir=str(tmp_path / "b"),
    )

    # std 任务:账号 A 的 std 桶是 0 → 应优先被选
    picked_std = repository.choose_available_account(quotas, model="seedance_v2.0_std")
    assert picked_std.id == "a"
    # mini 任务:账号 A 的 mini 桶已满 → 退到 B
    picked_mini = repository.choose_available_account(quotas, model="seedance_v2.0_mini")
    assert picked_mini.id == "b"


@pytest.mark.asyncio
async def test_service_charges_correct_bucket_per_model(repository, temp_profile):
    """v0.2.9:不同 model 的任务只扣对应桶,不串号。"""
    Account.create(
        id="acc-iso", display_name="iso", doubao_user_id="u-iso", profile_dir=temp_profile
    )
    runner = MiniOnlyLimitedRunner()
    service = VideoTaskService(
        repository, runner, StaticSettings(), account_poll_interval=0.01
    )

    # 第一个 mini 任务
    task_mini = service.start("mini 任务", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task_mini.id], timeout=2)

    # 第二个 std 任务
    task_std = service.start("std 任务", "seedance_v2.0_std", "1:1", 5)
    await asyncio.wait_for(service._tasks[task_std.id], timeout=2)

    account = Account.get_by_id("acc-iso")
    # mini 桶只 +1(只跑了 1 个 mini 任务)
    assert account.video_quota_used_mini == 1
    # std 桶只 +1(只跑了 1 个 std 任务)
    assert account.video_quota_used_std == 1
    # v2 桶没碰
    assert account.video_quota_used_v2 == 0
    # runner 真的按 model 跑
    assert runner.calls == ["seedance_v2.0_mini", "seedance_v2.0_std"]
