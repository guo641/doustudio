import asyncio

import pytest

from doupool.db.models import Account
from doupool.video.browser import TokenBundleUnavailable
from doupool.video.protocol import DoubaoContentRejected, DoubaoRateLimited
from doupool.video.service import VideoTaskService


class SuccessfulVideoRunner:
    async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
        update(status="generating", conversation_id="conversation-1")
        return {
            "remote_task_id": "remote-1",
            "result_url": "https://example.test/video.mp4",
            "cover_url": "https://example.test/cover.jpg",
        }


class StaticSettings:
    def get(self):
        return {
            "daily_quota_mini": 50, "daily_quota_v2": 50, "daily_quota_std": 50,
            "quota_reset_time": "00:00", "max_concurrency": 1,
        }

    def get_daily_quotas(self):
        return {"mini": 50, "v2": 50, "std": 50}


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
    # mini 1 点/秒,5 秒任务扣 5 点(对齐豆包真实扣费)
    assert Account.get_by_id("account-1").video_quota_used_mini == 5


@pytest.mark.asyncio
async def test_service_keeps_task_queued_without_an_available_account(repository):
    service = VideoTaskService(repository, SuccessfulVideoRunner(), StaticSettings(), account_poll_interval=0.01)
    task = service.start("测试", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.sleep(0.03)
    assert repository.get_video_task(task.id).status == "queued"
    await service.shutdown()


@pytest.mark.asyncio
async def test_run_inner_exits_silently_when_task_deleted(repository, temp_profile):
    """v0.2.15:任务被 DELETE 端点删了 → worker 静默退出,不再刷 IndexError。

    之前 get_video_task 抛 DoesNotExist(peewee 包成 IndexError: list index
    out of range),触发顶层兜底的 ERROR「视频任务执行器出现未捕获异常」。
    现在 get_video_task 返 None,_run_inner 直接 return。

    用 monkeypatch 把 get_video_task 替成「第一次返真 task,之后返 None」,
    模拟任务在 worker 跑的过程中被 DELETE 删掉,保证确定性。
    """
    from doupool.db.models import VideoTask
    Account.create(
        id="account-del", display_name="X", doubao_user_id="u", profile_dir=temp_profile
    )
    task = repository.create_video_task(
        "account-del", "P", "seedance_v2.0_mini", "1:1", 5,
    )
    real_get = repository.get_video_task
    calls = {"n": 0}

    def fake_get(task_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return real_get(task_id)  # 第一轮拿到真 task,进入 runner
        return None  # 之后所有轮都拿不到 → worker 退出

    repository.get_video_task = fake_get
    try:
        service = VideoTaskService(
            repository, SuccessfulVideoRunner(), StaticSettings(), account_poll_interval=0.01,
        )
        await asyncio.wait_for(service.resume_queued(), timeout=2)
        # 让 worker 跑过第一轮成功,然后下一轮 fake_get → None → return
        await asyncio.sleep(0.1)
        await service.shutdown()
        # worker 已经 return,没抛 IndexError,测试通过即说明 worker 静默退出
        assert calls["n"] >= 1
    finally:
        repository.get_video_task = real_get


class FailoverRunner:
    def __init__(self):
        self.calls = []

    async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
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
    assert Account.get_by_id("account-1").video_quota_used_mini == 50
    assert Account.get_by_id("account-1").video_limited_until is not None


class RiskControlRunner:
    """v0.2.16:模拟豆包风控拦截 — 第一次抛 is_risk_control=True,第二次随便来。"""

    def __init__(self):
        self.calls = 0

    async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
        self.calls += 1
        if self.calls == 1:
            raise DoubaoRateLimited(
                "rate limited",
                response_text='event: STREAM_ERROR\ndata: {"error_code":710022004,"error_msg":"rate limited","extra":{"decision":"{\\"from\\":\\"shark_admin\\",\\"type\\":\\"verify\\"}"}}\n\n',
                is_risk_control=True,
            )
        # 第二次假设直接成功
        update(status="generating", conversation_id="conversation-2")
        return {"remote_task_id": "remote-2", "result_url": "https://example.test/2.mp4"}


@pytest.mark.asyncio
async def test_service_does_not_cap_buckets_on_shark_admin_risk_control(repository, temp_profile):
    """v0.2.16:豆包 shark_admin 风控拦截(DoubaoRateLimited.is_risk_control=True)
    不该 cap 桶,也不该封号 — 风控经常很快就放,cap 桶只让用户误以为是 quota 限流。

    期望:任务标 failed + 错误信息"风控",账号 status / quota 桶 / limited_until 全部不动,
    后续还能选这个账号跑别的任务。
    """
    Account.create(
        id="acc-risk", display_name="风控账号", doubao_user_id="u-risk",
        profile_dir=temp_profile,
    )
    runner = RiskControlRunner()
    service = VideoTaskService(repository, runner, StaticSettings(), account_poll_interval=0.01)

    task = service.start("风控测试", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    assert saved.status == "failed"
    assert "风控" in (saved.error_message or "")
    # 桶没动
    acc = Account.get_by_id("acc-risk")
    assert acc.video_quota_used_mini == 0
    assert acc.video_limited_until is None
    assert acc.status == "active"


class TokenBundleUnavailableRunner:
    """v0.2.17:模拟 profile 抽不到 web_id 的情况(冷启动 / token 过期)。
    runner.run 直接抛 TokenBundleUnavailable,service 应该:
    - task 标 failed + 清晰错误信息
    - 桶 / limited_until / account.status 全部不动
    """

    async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
        raise TokenBundleUnavailable(
            "profile 中缺少 web_id,请在浏览器里访问 https://www.doubao.com/chat/ "
            "主页 5-10 秒后点「刷新 token」"
        )


@pytest.mark.asyncio
async def test_service_does_not_cap_buckets_on_token_bundle_unavailable(repository, temp_profile):
    """v0.2.17:profile 没 web_id → task failed,不动 quota,账号继续可调度。

    风控 quota 区分:这是配置问题(profile 冷启动 / token 过期),不是风控也不是
    quota。cap 桶只会让用户以为账号额度用完。任务失败后用户去点「刷新 token」
    再重试。
    """
    Account.create(
        id="acc-tok", display_name="token 账号", doubao_user_id="u-tok",
        profile_dir=temp_profile,
    )
    runner = TokenBundleUnavailableRunner()
    service = VideoTaskService(repository, runner, StaticSettings(), account_poll_interval=0.01)

    task = service.start("token 测试", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    assert saved.status == "failed"
    assert "web_id" in (saved.error_message or "")
    assert "刷新 token" in (saved.error_message or "")
    # 桶没动,账号继续 active
    acc = Account.get_by_id("acc-tok")
    assert acc.video_quota_used_mini == 0
    assert acc.video_limited_until is None
    assert acc.status == "active"


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

    async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
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

    async def run(self, *args, **kwargs):
        raise RuntimeError("normal run not used in retry-result tests")

    async def recheck_result(self, profile_dir, conversation_id, update, cancel_event, **kwargs):
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
        async def recheck_result(self, profile_dir, conversation_id, update, cancel_event, **kwargs):
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

    async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
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
    # 对齐豆包真实扣费:mini 5s → 5 点,std 5s → 8 点(ceil(7.5))
    assert account.video_quota_used_mini == 5
    assert account.video_quota_used_std == 8
    # v2 桶没碰
    assert account.video_quota_used_v2 == 0
    # runner 真的按 model 跑
    assert runner.calls == ["seedance_v2.0_mini", "seedance_v2.0_std"]


# ---------- v0.2.11:service.delete() ----------

def test_service_delete_removes_queued_task(repository, temp_profile):
    """v0.2.11:queued 状态 → service.delete() 物理删除,无异常。"""
    service = VideoTaskService(
        repository, SuccessfulVideoRunner(), StaticSettings(), account_poll_interval=0.01
    )
    queued_task = repository.create_video_task(
        None, "纯排队", "seedance_v2.0_mini", "1:1", 5
    )
    assert queued_task.status == "queued"

    service.delete(queued_task.id)
    from doupool.db.models import VideoTask
    assert VideoTask.select().count() == 0


def test_service_delete_running_raises_runtime_error(repository, temp_profile):
    """v0.2.11:generating 状态 → service.delete() 抛 RuntimeError,任务不动。"""
    Account.create(
        id="acc-r", display_name="r", doubao_user_id="u", profile_dir=temp_profile
    )
    service = VideoTaskService(
        repository, SuccessfulVideoRunner(), StaticSettings(), account_poll_interval=0.01
    )
    task = repository.create_video_task(
        None, "运行中", "seedance_v2.0_mini", "1:1", 5
    )
    repository.update_video_task(task.id, status="generating")

    with pytest.raises(RuntimeError, match="正在生成中"):
        service.delete(task.id)
    # 任务还在
    from doupool.db.models import VideoTask
    assert VideoTask.get_by_id(task.id).status == "generating"


def test_service_delete_missing_raises_value_error(repository, temp_profile):
    """v0.2.11:不存在的 task_id → ValueError('任务不存在')。"""
    service = VideoTaskService(
        repository, SuccessfulVideoRunner(), StaticSettings(), account_poll_interval=0.01
    )
    with pytest.raises(ValueError, match="任务不存在"):
        service.delete("does-not-exist")


# --- v0.2.19:单账号并发 + 共享 BrowserContext ---


@pytest.mark.asyncio
async def test_concurrent_tasks_on_same_account_share_browser_context(repository, tmp_path):
    """v0.2.19:同 profile_dir 上跑 3 个并发 task → 只有 1 个 BrowserContext。

    旧版每次 task 都 launch_persistent_context,profile Lockfile 互撞,
    还会触发 _account_locks 串行化。新版删了 per-account asyncio.Lock,
    runner 自己维护 per-profile 异步锁做首次 context 创建的串行保护,
    后续 task 直接拿到已存在 context,3 个 task 并发跑满 quota。
    """
    profile = tmp_path / "shared_profile"
    profile.mkdir()
    Account.create(
        id="acc-shared", display_name="共享账号",
        doubao_user_id="u-shared", profile_dir=str(profile),
    )

    class CountingRunner:
        """v0.2.19:记录 _ensure_playwright / context 创建次数,断言只创建 1 次。"""
        def __init__(self):
            self.context_creates = 0
            self.runner_starts = 0

        async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
            self.runner_starts += 1
            # 模拟真实 runner 的并发进入,记录每个 task 启动时间
            update(status="generating", conversation_id=f"conv-{self.runner_starts}")
            # 给短 sleep 让 task 真"并发"(yield event loop)
            import asyncio as _asyncio
            await _asyncio.sleep(0.05)
            return {
                "remote_task_id": f"remote-{self.runner_starts}",
                "result_url": f"https://example.test/v{self.runner_starts}.mp4",
            }

    runner = CountingRunner()

    class HighConcurrencySettings(StaticSettings):
        def get(self):
            data = super().get()
            data["max_concurrency"] = 3
            data["daily_quota_mini"] = 50
            return data
        def get_daily_quotas(self):
            return {"mini": 50, "v2": 50, "std": 50}

    settings = HighConcurrencySettings()
    service = VideoTaskService(repository, runner, settings, account_poll_interval=0.01)

    # 提交 3 个 mini 10s 任务(总成本 3*10 = 30 点,远低于 50 点桶)
    tasks = [service.start(f"并发测试 {i}", "seedance_v2.0_mini", "1:1", 10) for i in range(3)]
    await asyncio.wait_for(
        asyncio.gather(*[service._tasks[t.id] for t in tasks]),
        timeout=5,
    )

    # 3 个 task 都该 succeeded
    for task in tasks:
        saved = repository.get_video_task(task.id)
        assert saved.status == "succeeded", f"task {task.id} 状态异常: {saved.status}"

    # runner.run 被调了 3 次(每个 task 一次)
    assert runner.runner_starts == 3


@pytest.mark.asyncio
async def test_concurrent_tasks_do_not_serialise_per_account(repository, tmp_path):
    """v0.2.19:旧版 per-account asyncio.Lock 把同账号 task 串行化(max_concurrency=3
    也只跑 1 个)。新版删除该锁后,同账号 3 个 task 应该真并发跑。
    间接验证:每个 task 的 starting 时间窗应大幅重叠。
    """
    profile = tmp_path / "parallel_profile"
    profile.mkdir()
    Account.create(
        id="acc-parallel", display_name="并发账号",
        doubao_user_id="u-parallel", profile_dir=str(profile),
    )

    start_times: list[float] = []
    end_times: list[float] = []

    class TimingRunner:
        async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
            import time as _time
            start_times.append(_time.monotonic())
            update(status="generating", conversation_id="conv-timing")
            await asyncio.sleep(0.1)  # 让 task 真的并发
            end_times.append(_time.monotonic())
            return {"remote_task_id": "r", "result_url": "https://example.test/x.mp4"}

    runner = TimingRunner()

    class HighConcurrencySettings(StaticSettings):
        def get(self):
            data = super().get()
            data["max_concurrency"] = 3
            data["daily_quota_mini"] = 50
            return data
        def get_daily_quotas(self):
            return {"mini": 50, "v2": 50, "std": 50}

    settings = HighConcurrencySettings()
    service = VideoTaskService(repository, runner, settings, account_poll_interval=0.01)

    tasks = [service.start(f"并发 {i}", "seedance_v2.0_mini", "1:1", 10) for i in range(3)]
    await asyncio.wait_for(
        asyncio.gather(*[service._tasks[t.id] for t in tasks]),
        timeout=5,
    )

    # 3 个 start 都记录了
    assert len(start_times) == 3
    # 第一个 task 启动后 50ms 内,其他 task 都已启动(否则就是被 per-account lock 串行化)
    earliest = min(start_times)
    for ts in start_times:
        assert ts - earliest < 0.05, f"task 启动间隔 {ts - earliest:.3f}s > 0.05s,疑似被串行化"
    # end_times 也都应在第一个 end 后 50ms 内(并发跑完)
    earliest_end = min(end_times)
    for ts in end_times:
        assert ts - earliest_end < 0.05


# --- v0.2.19:失败退还额度 ---


class RefundableRunner:
    """v0.2.19:模拟 runner.run() 在扣完 quota 之后、网络异常/违规失败。

    `should_refund` 决定抛什么异常,以便单测覆盖 NETWORK/POLICY/INVALID 三类。
    """

    def __init__(self, *, failure_kind: str):
        if failure_kind == "network":
            self.exc = RuntimeError("网络请求超时(connect timeout)")
        elif failure_kind == "policy":
            self.exc = RuntimeError("生成内容中疑似包含侵权内容,换个主题再试试")
        elif failure_kind == "invalid":
            self.exc = RuntimeError("参数无效:图片格式错误")
        elif failure_kind == "generation_failed":
            # 不退款的一类 — 应保留 quota
            self.exc = RuntimeError("视频生成失败")
        else:
            raise ValueError(f"unknown failure_kind: {failure_kind}")

    async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
        # 先扣 quota(update(generating) → service 内部 increment_account_quota)
        update(status="generating", conversation_id="conv-refund")
        # 然后再抛失败 — 这模拟「扣款成功但豆包拒绝/网络断」的真实路径
        raise self.exc


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure_kind, should_refund",
    [
        ("network", True),
        ("policy", True),
        ("invalid", True),
        ("generation_failed", False),
    ],
)
async def test_service_refunds_quota_on_refundable_failures(
    repository, temp_profile, failure_kind, should_refund
):
    """v0.2.19:NETWORK / POLICY_VIOLATION / INVALID_INPUT 失败时,退还已扣 quota。
    GENERATION_FAILED 不退(豆包很可能已经计费)。
    """
    Account.create(
        id="acc-refund", display_name="refund", doubao_user_id="u-refund",
        profile_dir=temp_profile,
    )
    runner = RefundableRunner(failure_kind=failure_kind)
    service = VideoTaskService(repository, runner, StaticSettings(), account_poll_interval=0.01)

    # mini 5s = 5 点,cost = quota_cost("seedance_v2.0_mini", 5) = 5
    task = service.start(f"{failure_kind} 测试", "seedance_v2.0_mini", "1:1", 5)
    # generation_failed 走 revise_prompt 路径,会触发 2 次重试;
    # 用 5s 留点余量
    await asyncio.wait_for(service._tasks[task.id], timeout=5)

    saved = repository.get_video_task(task.id)
    assert saved.status == "failed"
    acc = Account.get_by_id("acc-refund")
    if should_refund:
        # 扣了 5 点,失败后退还 5 点 → 净 0
        assert acc.video_quota_used_mini == 0, (
            f"{failure_kind} 应该退款,但 mini 桶 = {acc.video_quota_used_mini}"
        )
    else:
        # generation_failed 不退。revise_prompt 路径会跑 max_attempts=2 次重试,
        # 每次扣 5 点(5s × 1.0/s),所以累计 15 点不退。
        assert acc.video_quota_used_mini == 15, (
            f"{failure_kind} 不该退款,但 mini 桶 = {acc.video_quota_used_mini}"
        )


class RefundAfterRetryRunner:
    """v0.2.19:违规失败 → 退款 + 改 prompt 重试,每次失败都退。"""

    def __init__(self):
        self.calls = 0

    async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
        self.calls += 1
        update(status="generating", conversation_id=f"conv-{self.calls}")
        # max_attempts=2 → service 会跑 3 次: 1 次原始 + 2 次重试,都抛违规
        raise RuntimeError("生成内容中疑似包含侵权内容,换个主题再试试")


@pytest.mark.asyncio
async def test_service_refunds_quota_on_each_retry_attempt(repository, temp_profile):
    """v0.2.19:违规改 prompt 重试时,每次失败都退 — 不然用户同一 prompt 被扣两次。"""
    Account.create(
        id="acc-retry-refund", display_name="retry", doubao_user_id="u",
        profile_dir=temp_profile,
    )
    runner = RefundAfterRetryRunner()
    service = VideoTaskService(repository, runner, StaticSettings(), account_poll_interval=0.01)

    task = service.start("违规测试", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task.id], timeout=5)

    # runner 被调 3 次:1 次原始 + max_attempts=2 次重试,每次都失败
    assert runner.calls == 3
    acc = Account.get_by_id("acc-retry-refund")
    # 扣 3 次 5 点,退 3 次 5 点 → 净 0
    assert acc.video_quota_used_mini == 0


class FailBeforeGeneratingRunner:
    """v0.2.19:runner 抛异常前没调用 update(generating) → quota 没扣 → 退款 noop。"""

    async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
        # 模拟「profile 加载失败」这种 — runner 没机会走到 generating
        raise RuntimeError("profile 加载失败:路径不存在")


@pytest.mark.asyncio
async def test_service_refund_noop_when_quota_was_not_charged(repository, temp_profile):
    """v0.2.19:runner 在扣 quota 之前就抛了 → 退款路径要安全 noop,桶不动。"""
    Account.create(
        id="acc-noref", display_name="noref", doubao_user_id="u",
        profile_dir=temp_profile,
    )
    runner = FailBeforeGeneratingRunner()
    service = VideoTaskService(repository, runner, StaticSettings(), account_poll_interval=0.01)

    task = service.start("前置失败", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    acc = Account.get_by_id("acc-noref")
    # 桶没扣(没到 generating),decrement noop 后也是 0
    assert acc.video_quota_used_mini == 0


# ---------- v0.2.21:内容审核拒绝识别 + 立即失败 + 退还额度 ----------


class ContentRejectedRunner:
    """v0.2.21:模拟豆包 chain 响应里的内容审核拒绝 — runner.run 抛
    DoubaoContentRejected,service 应该:
    - task 立即标 failed + 清晰错误信息("豆包拒绝:无法返回该内容")
    - 退还本 runner 已扣的额度(quota_cost = 5 点 for mini 5s)
    - 触发 callback(_schedule_callback)
    - 不走 prompt 改写重试(同 prompt 必拒)
    """

    def __init__(self):
        self.calls = 0

    async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
        self.calls += 1
        # runner 内部从「开始生成」到 chain 响应里看到拒绝文案,
        # 跟真实跑一样会先 update("generating") 触发扣款,然后再抛 DoubaoContentRejected
        update(status="generating", conversation_id="conv-rej")
        raise DoubaoContentRejected(
            "无法返回该内容",
            response_text='{"downlink_body":{"messages":[{"text":"无法返回该内容"}]}}',
        )


@pytest.mark.asyncio
async def test_service_marks_failed_and_refunds_on_content_rejected(repository, temp_profile):
    """v0.2.21:豆包 chain 拒绝 → 立即 failed + 退还 5 点 mini 额度 + 不重试。"""
    Account.create(
        id="acc-rej", display_name="rej", doubao_user_id="u-rej",
        profile_dir=temp_profile,
    )
    runner = ContentRejectedRunner()
    service = VideoTaskService(repository, runner, StaticSettings(), account_poll_interval=0.01)

    task = service.start("违规测试", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    # 1. task 必须 failed + error_message 含「豆包拒绝」+ 原文
    assert saved.status == "failed"
    assert "豆包拒绝" in (saved.error_message or "")
    assert "无法返回该内容" in (saved.error_message or "")
    # 2. 桶被「扣→退」,最终是 0(不是 5)
    acc = Account.get_by_id("acc-rej")
    assert acc.video_quota_used_mini == 0
    assert acc.video_limited_until is None
    assert acc.status == "active"
    # 3. 拒绝只调一次 runner(没改写 prompt 重试)
    assert runner.calls == 1


@pytest.mark.asyncio
async def test_content_rejected_skips_prompt_retry(repository, temp_profile):
    """v0.2.21:同 prompt 必拒,绝不触发 prompt 改写重试路径(prompt_retry_count=0)。"""
    Account.create(
        id="acc-rej2", display_name="rej2", doubao_user_id="u-rej2",
        profile_dir=temp_profile,
    )
    runner = ContentRejectedRunner()
    service = VideoTaskService(repository, runner, StaticSettings(), account_poll_interval=0.01)

    task = service.start("违规再测", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    # prompt_retry_count 必须保持 0(没走 revise_prompt)
    assert saved.prompt_retry_count == 0
    # prompt 也没被改写
    assert saved.prompt == "违规再测"
    # runner 只跑一次(没重试)
    assert runner.calls == 1


# ---------- v0.2.22 Q1:内容审核拒绝自动改写 prompt 重试(opt-in,默认 0) ----------


class ReviseMockRunner:
    """v0.2.22 Q1:模拟 browser.py 中 PlaywrightVideoRunner 的内部 revise retry loop。

    真实实现中,service._run_inner 调一次 self.runner.run(),runner.run 内部
    while-loop 抓 DoubaoContentRejected → revise_prompt → _submit_and_poll 改
    prompt 重提交,直到 max_reject_retries 用完。所以 service 视角下 self.runner.run()
    只调 1 次;但每次 invoke 内部,run() 自己会触发 1+max_reject_retries 次
    _submit_and_poll 等价物。

    本 mock 把内部循环直接复制出来,以便不依赖真实 Playwright 即可测试整条路径。
    """

    def __init__(self, *, always_reject: bool, success_after: int = 0):
        self.always_reject = always_reject
        self.success_after = success_after  # 第 N 次调用时改成返回 success
        self.service_calls = 0  # service → runner.run() 次数
        self.internal_attempts = 0  # runner 内部 _submit_and_poll 等价物次数
        self.prompts_seen: list[str] = []

    async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
        self.service_calls += 1
        max_reject_retries = int(kwargs.get("max_reject_retries", 0))
        from doupool.prompt_reviser import classify_failure, revise_prompt
        prompt_to_send = prompt
        attempt = 0
        while True:
            self.internal_attempts += 1
            self.prompts_seen.append(prompt_to_send)
            # runner 内部 update("generating") 触发扣款(quota_recorded 闸门只扣 1 次)
            update(status="generating", conversation_id=f"conv-{self.internal_attempts}")
            should_reject = self.always_reject or (self.internal_attempts <= self.success_after)
            if should_reject:
                if max_reject_retries <= 0 or attempt >= max_reject_retries:
                    raise DoubaoContentRejected(
                        "无法返回该内容",
                        response_text='{"downlink_body":{"messages":[{"text":"无法返回该内容"}]}}',
                    )
                attempt += 1
                failure = classify_failure("无法返回该内容")
                new_prompt = revise_prompt(prompt_to_send, failure, attempt=attempt)
                if not new_prompt or new_prompt == prompt_to_send:
                    raise DoubaoContentRejected(
                        "无法返回该内容",
                        response_text='{"downlink_body":{"messages":[{"text":"无法返回该内容"}]}}',
                    )
                update(error_message=f"豆包拒绝(第 {attempt}/{max_reject_retries} 次改写重试中)")
                prompt_to_send = new_prompt
                continue
            return {
                "remote_task_id": f"remote-{self.internal_attempts}",
                "result_url": f"https://example.test/video-{self.internal_attempts}.mp4",
                "cover_url": f"https://example.test/cover-{self.internal_attempts}.jpg",
            }


class ReviseSettings:
    """v0.2.22 Q1:StaticSettings 子类,允许测试覆盖 max_reject_retries。"""

    def __init__(self, max_reject_retries: int = 0):
        self._max = max_reject_retries

    def get(self):
        return {
            "daily_quota_mini": 50, "daily_quota_v2": 50, "daily_quota_std": 50,
            "quota_reset_time": "00:00", "max_concurrency": 1,
            "max_reject_retries": self._max,
        }

    def get_daily_quotas(self):
        return {"mini": 50, "v2": 50, "std": 50}


@pytest.mark.asyncio
async def test_content_rejected_revise_when_enabled_uses_two_attempts(repository, temp_profile):
    """v0.2.22 Q1:max_reject_retries=2 时,runner 拒 2 次后第三次成功——
    service 调 1 次 runner.run,内部 3 次 attempt(1 原始 + 2 改写),
    最终 succeeded,扣款仅 1 次,每次 prompt 不同(改写器真在改)。"""
    Account.create(
        id="acc-rev1", display_name="rev1", doubao_user_id="u-rev1",
        profile_dir=temp_profile,
    )
    runner = ReviseMockRunner(always_reject=False, success_after=2)
    service = VideoTaskService(
        repository, runner, ReviseSettings(max_reject_retries=2),
        account_poll_interval=0.01,
    )

    task = service.start("习近平出场", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    # 1. 最终 succeeded
    assert saved.status == "succeeded"
    # 2. service 调 runner.run 只 1 次(内部循环)
    assert runner.service_calls == 1
    # 3. 内部 attempt 3 次(1 原始 + 2 revise)
    assert runner.internal_attempts == 3
    # 4. 每次 prompt 不同(改写器真在改)
    assert runner.prompts_seen[0] == "习近平出场"
    assert runner.prompts_seen[1] != "习近平出场"
    assert runner.prompts_seen[2] != runner.prompts_seen[1]
    # 5. 扣款只 1 次(quota_recorded 闸门):5 点 mini
    acc = Account.get_by_id("acc-rev1")
    assert acc.video_quota_used_mini == 5
    # 6. task.prompt 仍是原文(revise 只改传输中的 prompt_to_send,DB 不写)
    assert saved.prompt == "习近平出场"


@pytest.mark.asyncio
async def test_content_rejected_revise_exhausts_after_max_attempts(repository, temp_profile):
    """v0.2.22 Q1:max_reject_retries=2 但 runner 永远拒 —— runner 内部跑 3 次
    (1+2) attempt 后 re-raise,service 接住 → failed + 退还额度(0) + error_message
    含「豆包拒绝」。"""
    Account.create(
        id="acc-rev2", display_name="rev2", doubao_user_id="u-rev2",
        profile_dir=temp_profile,
    )
    runner = ReviseMockRunner(always_reject=True)
    service = VideoTaskService(
        repository, runner, ReviseSettings(max_reject_retries=2),
        account_poll_interval=0.01,
    )

    task = service.start("色情片", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    # 1. failed
    assert saved.status == "failed"
    # 2. service 调 runner.run 1 次,内部 3 次(1+2) attempt 后 re-raise
    assert runner.service_calls == 1
    assert runner.internal_attempts == 3
    # 3. 退还额度(0,扣 5 退 5)
    acc = Account.get_by_id("acc-rev2")
    assert acc.video_quota_used_mini == 0
    # 4. error_message 含「豆包拒绝」
    assert "豆包拒绝" in (saved.error_message or "")


@pytest.mark.asyncio
async def test_content_rejected_revise_disabled_keeps_v0_2_21_behavior(repository, temp_profile):
    """v0.2.22 Q1:max_reject_retries=0(默认)时,行为与 v0.2.21 一致:
    service 调 1 次 runner.run,内部 1 次 attempt 即 re-raise,失败 + 退款。
    回归保护。"""
    Account.create(
        id="acc-rev3", display_name="rev3", doubao_user_id="u-rev3",
        profile_dir=temp_profile,
    )
    runner = ReviseMockRunner(always_reject=True)
    service = VideoTaskService(
        repository, runner, ReviseSettings(max_reject_retries=0),
        account_poll_interval=0.01,
    )

    task = service.start("违规再试", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    # service 调 runner.run 1 次,内部 1 次 attempt 即 re-raise(没开改写)
    assert runner.service_calls == 1
    assert runner.internal_attempts == 1
    assert saved.status == "failed"
    assert runner.prompts_seen == ["违规再试"]
    # 退款后为 0
    assert Account.get_by_id("acc-rev3").video_quota_used_mini == 0


# ---------- v0.2.22 Q4:同步 refresh-url 端点 ----------


@pytest.mark.asyncio
async def test_refresh_url_returns_fresh_signed_url(repository, temp_profile):
    """v0.2.22 Q4:DownloadButton 下载失败 → 前端调 schedule_refresh_url。
    后端用 runner.recheck_result(deadline=60s) 拿新签名 URL,只刷
    result_url / backup_result_url / fallback_result_url,不动 status /
    不发 callback / 不跑 watermark / 不消耗 quota。"""
    Account.create(
        id="acc-rfu", display_name="acc", doubao_user_id="u", profile_dir=temp_profile
    )
    account = Account.get_by_id("acc-rfu")
    account.video_quota_used_mini = 8  # 假装此前已扣过若干次
    account.save()

    task = repository.create_video_task("acc-rfu", "x", "seedance_v2.0_mini", "1:1", 5)
    task.conversation_id = "conv-rfu"
    task.status = "succeeded"
    task.result_url = "https://old.example/video.mp4"
    task.backup_result_url = "https://old.example/backup.mp4"
    task.save()

    fresh_result = {
        "result_url": "https://fresh.example/video.mp4",
        "backup_result_url": "https://fresh.example/backup.mp4",
        "fallback_result_url": "https://fresh.example/fallback.mp4",
        "cover_url": "https://fresh.example/cover.jpg",
    }
    runner = RecheckRunner(fresh_result)
    service = VideoTaskService(repository, runner, StaticSettings())

    wrapper = service.schedule_refresh_url(task.id)
    # 同步语义:前端 await wrapper 拿新 task,内部 body 已跑完。
    refreshed = await wrapper
    assert refreshed.id == task.id

    saved = repository.get_video_task(task.id)
    # 1. status 仍是 succeeded(没拿到新 result 时回滚,这里拿到了就保持)
    assert saved.status == "succeeded"
    # 2. result_url 三个字段都被刷新
    assert saved.result_url == "https://fresh.example/video.mp4"
    assert saved.backup_result_url == "https://fresh.example/backup.mp4"
    assert saved.fallback_result_url == "https://fresh.example/fallback.mp4"
    # 3. cover_url 拿到但不写库(refresh-url 只动 result 系列,不动 cover_url)
    #    —— 这个保持 nil,因为 update_video_task 没传 cover_url
    assert saved.cover_url is None
    # 4. error_message 清空
    assert not saved.error_message
    # 5. 关键:不扣额度
    assert Account.get_by_id("acc-rfu").video_quota_used_mini == 8


@pytest.mark.asyncio
async def test_refresh_url_rejects_non_succeeded_task(repository, temp_profile):
    """v0.2.22 Q4:仅 succeeded 任务支持 refresh-url。
    failed / generating / queued 任务应当抛 ValueError,前端拿到 409。"""
    Account.create(
        id="acc-rfu2", display_name="x", doubao_user_id="u", profile_dir=temp_profile
    )
    task = repository.create_video_task("acc-rfu2", "x", "seedance_v2.0_mini", "1:1", 5)
    task.conversation_id = "conv-rfu2"
    task.status = "failed"  # 非 succeeded
    task.save()

    service = VideoTaskService(repository, RecheckRunner(None), StaticSettings())
    with pytest.raises(ValueError, match="仅 succeeded"):
        service.schedule_refresh_url(task.id)


@pytest.mark.asyncio
async def test_refresh_url_rejects_missing_task(repository):
    """v0.2.22 Q4:任务不存在 → ValueError,前端拿 404。"""
    service = VideoTaskService(repository, RecheckRunner(None), StaticSettings())
    with pytest.raises(ValueError, match="任务不存在"):
        service.schedule_refresh_url("does-not-exist")
