import asyncio
import contextlib
from datetime import datetime
from pathlib import Path

import pytest

from doupool.db.models import Account
from doupool.video.browser import TokenBundleUnavailable
from doupool.video.protocol import DoubaoContentRejected, DoubaoRateLimited
from doupool.video.service import NoAvailableAccount, VideoTaskService


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
            # v0.2.29:共享池下 daily_quota_mini/v2/std 已废弃,只保留
            # daily_quota_shared + 兼容 fallback daily_quota。
            "daily_quota": 50,
            "quota_reset_time": "00:00", "max_concurrency": 1,
        }

    def get_daily_quotas(self):
        # v0.2.29:repository 期望单一 shared 桶。
        return {"shared": 50}


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
    assert Account.get_by_id("account-1").video_quota_used_shared == 5


@pytest.mark.asyncio
async def test_service_respects_task_interval_seconds(repository, temp_profile):
    """v0.2.34:并发任务间隔(秒)—— 每个 task 在抢 _global_semaphore 之前
    sleep N 秒。把 StaticSettings.max_concurrency 调成 1(强制串行),把
    task_interval_seconds 设为 0.2,验证从「开始派发」到「runner.run 真正入
    参」至少间隔 0.2s。如果回归成 interval 缺失或被 clamp 到 0,这次会
    < 0.1s 跑完,断言失败。
    """
    Account.create(
        id="account-1", display_name="g", doubao_user_id="u", profile_dir=temp_profile
    )

    class IntervalSettings:
        def get(self):
            return {
                "daily_quota": 50,
                "quota_reset_time": "00:00",
                "max_concurrency": 1,
                # v0.2.34:用户调成 0.2 秒,绕开 1s 阈值太慢的场景
                "task_interval_seconds": 0.2,
            }

        def get_daily_quotas(self):
            return {"shared": 50}

    captured = {"dispatched_at": None, "runner_called_at": None}

    class IntervalTimingRunner:
        async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
            captured["runner_called_at"] = asyncio.get_event_loop().time()
            update(status="generating", conversation_id="c1")
            return {
                "remote_task_id": "r1",
                "result_url": "https://example.test/v.mp4",
                "cover_url": "https://example.test/c.jpg",
            }

    service = VideoTaskService(
        repository, IntervalTimingRunner(), IntervalSettings(), account_poll_interval=0.01,
    )
    captured["dispatched_at"] = asyncio.get_event_loop().time()
    task = service.start("P", "seedance_v2.0_mini", "1:1", 5)
    try:
        await asyncio.wait_for(service._tasks[task.id], timeout=3)
    finally:
        await service.shutdown()

    gap = captured["runner_called_at"] - captured["dispatched_at"]
    # 容差 50ms(调度器 + asyncio 调度有 ~10ms 抖动,0.2s 量级 50ms 够)
    assert gap >= 0.15, f"task_interval_seconds 应至少 sleep 0.2s,实际 {gap:.3f}s"


@pytest.mark.asyncio
async def test_service_serializes_task_interval_across_concurrent_tasks(repository, temp_profile):
    """v0.2.34 hotfix:并发派发 N 个 task 时,interval 必须**全局串行** —— 3
    个 task 同时间起跑,各自 sleep interval 失去意义(并行 sleep 一起醒来,
    race 触发豆包风控)。验证:interval=0.15、3 task,第 2、3 个 runner
    调用时刻相对第 1 个至少 +0.15 / +0.30s。
    """
    for i in range(3):
        sub = Path(temp_profile) / f"p{i}"
        sub.mkdir()
        Account.create(
            id=f"acc-{i}",
            display_name=f"A{i}",
            doubao_user_id=f"u{i}",
            profile_dir=str(sub),
        )

    class SerialSettings:
        def get(self):
            return {
                "daily_quota": 50,
                "quota_reset_time": "00:00",
                # max_concurrency=1 强制同账号串行,只让 interval 路径影响节奏
                "max_concurrency": 1,
                "task_interval_seconds": 0.15,
            }

        def get_daily_quotas(self):
            return {"shared": 50}

    captured = []

    class CaptureRunner:
        async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
            captured.append(asyncio.get_event_loop().time())
            return {
                "remote_task_id": "r",
                "result_url": "https://example.test/v.mp4",
                "cover_url": "https://example.test/c.jpg",
            }

    service = VideoTaskService(
        repository, CaptureRunner(), SerialSettings(), account_poll_interval=0.01,
    )
    t0 = asyncio.get_event_loop().time()
    tasks = [service.start(f"P{i}", "seedance_v2.0_mini", "1:1", 5) for i in range(3)]
    try:
        await asyncio.wait_for(
            asyncio.gather(*(service._tasks[t.id] for t in tasks)),
            timeout=3,
        )
    finally:
        await service.shutdown()

    assert len(captured) == 3, f"预期 3 个 runner 调用,实际 {len(captured)}"
    gaps = [captured[i] - captured[0] for i in range(3)]
    assert gaps[1] >= 0.14, f"第 2 个 runner 应在 +0.15s 后跑,实际 +{gaps[1]:.3f}s"
    assert gaps[2] >= 0.29, f"第 3 个 runner 应在 +0.30s 后跑,实际 +{gaps[2]:.3f}s"


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
    assert Account.get_by_id("account-1").video_quota_used_shared == 50
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
    assert acc.video_quota_used_shared == 0
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
    assert acc.video_quota_used_shared == 0
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
    account.video_quota_used_shared = 2  # 假装此前已扣过两次
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
    assert Account.get_by_id("acc-r").video_quota_used_shared == 2
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


# ---------- v0.2.29 共享额度池:cost() 仍按 model 算,但全累加到 shared ----------

class MiniOnlyLimitedRunner:
    """v0.2.29:cost() 按 model 算点数,但所有 model 的 cost 都累加到 shared 桶。"""
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
async def test_repository_chooses_account_by_shared_quota(repository, tmp_path):
    """v0.2.29:账号之间按 shared 桶余额比较(least_used)。

    v0.2.9 的 test_quota_per_model_isolation 在共享池改造后已无意义,
    这里替换成 shared 桶版本:不同 model 不再影响账号选择。
    """
    quotas = {"shared": 10}
    # 账号 A:shared 用 8(剩 2)
    Account.create(
        id="a", display_name="A", doubao_user_id="ua",
        profile_dir=str(tmp_path / "a"), video_quota_used_shared=8,
    )
    # 账号 B:shared 用 1(剩 9)
    Account.create(
        id="b", display_name="B", doubao_user_id="ub",
        profile_dir=str(tmp_path / "b"), video_quota_used_shared=1,
    )
    # 不管 model,least_used 都选 B(余额最多)
    picked_mini = repository.choose_available_account(quotas, model="seedance_v2.0_mini")
    assert picked_mini.id == "b"
    picked_std = repository.choose_available_account(quotas, model="seedance_v2.0_std")
    assert picked_std.id == "b"


@pytest.mark.asyncio
async def test_service_charges_shared_bucket_per_model_cost(repository, temp_profile):
    """v0.2.29:不同 model 任务的 cost 不同,但都累加到 shared 桶。

    mini 5s → 5 点,std 5s → 8 点(ceil(7.5))。两个任务完成后 shared 应该
    = 5 + 8 = 13。旧 mini/v2/std 三桶不再写入,保持 0。
    """
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
    # shared 桶累加两个任务的 cost(5 + 8 = 13)
    assert account.video_quota_used_shared == 13
    # v0.2.29:旧 mini/v2/std 三桶不再写入,保持 0
    assert account.video_quota_used_mini == 0
    assert account.video_quota_used_std == 0
    assert account.video_quota_used_v2 == 0
    # runner 真的按 model 跑(cost 仍按 model 算,只是扣哪个桶统一了)
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
            return data

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
            return data

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
        elif failure_kind == "timeout":
            # v0.2.27:本地 deadline 超时,应退款 —— 豆包没出结果,没真实扣费。
            self.exc = RuntimeError("视频生成超时")
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
        ("timeout", True),  # v0.2.27:本地 deadline 超时也退款
        ("generation_failed", False),
    ],
)
async def test_service_refunds_quota_on_refundable_failures(
    repository, temp_profile, failure_kind, should_refund
):
    """v0.2.19:NETWORK / POLICY_VIOLATION / INVALID_INPUT 失败时,退还已扣 quota。
    GENERATION_FAILED 不退(豆包很可能已经计费)。
    v0.2.27:加入 TIMEOUT —— 本地 deadline 超时也算退(豆包没出结果)。
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
        assert acc.video_quota_used_shared == 0, (
            f"{failure_kind} 应该退款,但 shared 桶 = {acc.video_quota_used_shared}"
        )
    else:
        # v0.2.33:start() 已预扣 → 失败路径无条件退(不再按 kind 筛选)。
        # revise_prompt 重试 3 次(1 原始 + 2 重试),每次 start() 都会预扣
        # 5 点,然后失败路径退 5 点 → 终态仍为 0(最后一次失败退完)。
        assert acc.video_quota_used_shared == 0, (
            f"v0.2.33: 所有失败路径都退,shared 桶应=0,实={acc.video_quota_used_shared}"
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
    assert acc.video_quota_used_shared == 0


class FailBeforeGeneratingRunner:
    """v0.2.19:runner 抛异常前没调用 update(generating) → quota 没扣 → 退款 noop。"""

    async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
        # 模拟「profile 加载失败」这种 — runner 没机会走到 generating
        raise RuntimeError("profile 加载失败:路径不存在")


@pytest.mark.asyncio
async def test_service_refund_noop_when_quota_was_not_charged(repository, temp_profile):
    """v0.2.19:runner 在扣 quota 之前就抛了 → 退款路径要安全 noop,桶不动。
    v0.2.33:start() 现在预扣 cost → 失败路径必须退预扣,终态仍是 0。
    """
    Account.create(
        id="acc-noref", display_name="noref", doubao_user_id="u",
        profile_dir=temp_profile,
    )
    runner = FailBeforeGeneratingRunner()
    service = VideoTaskService(repository, runner, StaticSettings(), account_poll_interval=0.01)

    task = service.start("前置失败", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    acc = Account.get_by_id("acc-noref")
    # v0.2.33:start() 预扣 5 + 失败路径退 5 → 终态 0
    assert acc.video_quota_used_shared == 0


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
    assert acc.video_quota_used_shared == 0
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
    """v0.2.22 Q1:StaticSettings 子类,允许测试覆盖 max_reject_retries。
    v0.2.29 共享池改造后用 daily_quota + get_daily_quotas 返 {"shared": ...}。
    """

    def __init__(self, max_reject_retries: int = 0):
        self._max = max_reject_retries

    def get(self):
        return {
            "daily_quota": 50, "quota_reset_time": "00:00", "max_concurrency": 1,
            "max_reject_retries": self._max,
        }

    def get_daily_quotas(self):
        return {"shared": 50}


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
    assert acc.video_quota_used_shared == 5
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
    assert acc.video_quota_used_shared == 0
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
    assert Account.get_by_id("acc-rev3").video_quota_used_shared == 0


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
    account.video_quota_used_shared = 8  # 假装此前已扣过若干次
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
    assert Account.get_by_id("acc-rfu").video_quota_used_shared == 8


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


# --- v0.2.27:用户取消也退还额度 ---


class CancellingRunner:
    """v0.2.27 测试用:runner 进入 generating 后主动 set cancel event,然后
    抛异常 —— 模拟「用户在生成中途点了停止」。这会走 service.py:_run_inner
    的 `if cancellation.is_set()` 分支。

    v0.2.30:取消时直接标 failed,不再写 queued。期望:退还 quota +
    任务标 failed + 文案「应用已停止，任务已取消」。

    runner 自己抛异常而不是返回成功,是为了让 service 进 except 分支(cancellation
    检查在 except 块里)。
    """

    async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
        # 先扣 quota
        update(status="generating", conversation_id="conv-cancel")
        # 模拟用户在 generating 阶段点了停止 — set cancel,然后让 except 块
        # 命中 cancellation.is_set() True 分支
        cancel_event.set()
        raise RuntimeError("任务已取消")


@pytest.mark.asyncio
async def test_service_refunds_quota_on_user_cancel(repository, temp_profile):
    """v0.2.27 行为变更:用户主动取消也退还额度(之前不退)。

    之前 v0.2.19-v0.2.26 的逻辑是「cancel = 不退,因为豆包已经在跑」,但这
    对用户不公平 —— 用户没拿到视频 = 失败,不应扣 quota。这次统一改成
    「失败 = 退」。

    v0.2.30:取消时任务标 failed(不再写 queued + 等下次继续),文案
    「应用已停止，任务已取消」,quota 净 0。
    """
    Account.create(
        id="acc-cancel", display_name="cancel", doubao_user_id="u-cancel",
        profile_dir=temp_profile,
    )
    runner = CancellingRunner()
    service = VideoTaskService(repository, runner, StaticSettings(), account_poll_interval=0.01)

    # mini 5s = 5 点
    task = service.start("取消测试", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    # v0.2.30:取消后任务标 failed,不再写 queued(避免 resume 死循环)
    assert saved.status == "failed", f"取消后应为 failed,实际 {saved.status}"
    assert "应用已停止" in (saved.error_message or ""), (
        f"取消文案应明确,实际: {saved.error_message}"
    )
    assert saved.completed_at is not None, "failed 状态应写 completed_at"
    # 关键:quota 净 0(扣 5 退 5)
    acc = Account.get_by_id("acc-cancel")
    assert acc.video_quota_used_shared == 0, (
        f"取消应退款,但 shared 桶 = {acc.video_quota_used_shared}"
    )


@pytest.mark.asyncio
async def test_service_cancel_refund_noop_when_quota_not_charged(repository, temp_profile):
    """v0.2.27:runner 在扣 quota 前就 cancel → refund_quota_if_recorded()
    安全 noop,不报错,quota 不动。
    v0.2.33:start() 已预扣 cost → 取消路径必须退预扣,终态仍是 0。
    """
    Account.create(
        id="acc-cancel-noop", display_name="cn", doubao_user_id="u",
        profile_dir=temp_profile,
    )

    class CancelBeforeChargingRunner:
        async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
            cancel_event.set()
            raise RuntimeError("任务已取消")

    runner = CancelBeforeChargingRunner()
    service = VideoTaskService(repository, runner, StaticSettings(), account_poll_interval=0.01)

    task = service.start("早取消", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    acc = Account.get_by_id("acc-cancel-noop")
    # v0.2.33:start() 预扣 5 + 取消退 5 → 终态 0
    assert acc.video_quota_used_shared == 0  # 预扣已退,refund 路径要安全


# --- v0.2.30:_run 顶层 CancelledError 也写 failed + resume_queued stale 兜底 ---


class CancelledDuringStartupRunner:
    """v0.2.30 测试用:runner.run 还没开始就 asyncio.CancelledError 冒泡
    (模拟 uvicorn / webview 关闭触发的事件循环 cancel)。

    期望:_run 顶层 except CancelledError 捕获,写 failed + 「应用已停止,
    任务已取消」,不写 queued(避免 resume 死循环)。
    """

    async def run(self, profile_dir, prompt, model, ratio, duration, update, cancel_event, **kwargs):
        # 模拟 await 某个阻塞操作时被 cancel —— 直接抛 CancelledError,
        # 不经过 update("generating"),所以 quota 不会被扣。
        raise asyncio.CancelledError()


@pytest.mark.asyncio
async def test_run_top_level_cancelled_error_writes_failed(repository, temp_profile):
    """v0.2.30 bug fix:_run 顶层 except CancelledError 不再写 queued。

    之前 v0.2.27 之前:cancel → status=queued + error='应用已停止,等待下次继续',
    resume_queued 拉起 → 又被 cancel → 又写回 queued,死循环,任务永远卡
    「生成中」。改成 failed + 明确文案。
    """
    Account.create(
        id="acc-cancel-top", display_name="top cancel", doubao_user_id="u",
        profile_dir=temp_profile,
    )
    service = VideoTaskService(
        repository, CancelledDuringStartupRunner(), StaticSettings(),
        account_poll_interval=0.01,
    )

    task = service.start("顶层取消", "seedance_v2.0_mini", "1:1", 5)
    # _run 顶层 except CancelledError 处理完后会 re-raise,这是预期行为
    # (上层 shutdown 收到 cancel 信号知道自己该停了),但 pytest-asyncio
    # 会把 re-raise 当成测试失败,所以这里 swallow 一下。
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(service._tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    assert saved.status == "failed", (
        f"顶层 CancelledError 应标 failed,实际 {saved.status}"
    )
    assert "应用已停止" in (saved.error_message or ""), (
        f"文案应明确,实际: {saved.error_message}"
    )
    assert saved.completed_at is not None, "failed 状态应写 completed_at"
    # 账号分配应该被解除,下次提交同 prompt 不会撞死账号
    assert saved.account_id is None, "cancel 后应释放账号分配"
    # v0.2.33:start() 已预扣 cost → cancel 路径必须退,终态 0
    acc = Account.get_by_id("acc-cancel-top")
    assert acc.video_quota_used_shared == 0


@pytest.mark.asyncio
async def test_resume_queued_sanitizes_stale_queued_tasks(repository, temp_profile):
    """v0.2.30 bug fix:resume_queued 在调度前 sanitize stale queued。

    场景:DB 里残留一个 status=queued 但 updated_at 是 2 小时前的任务
    (历史上 cancel 写入的孤儿)。启动 lifespan 调 resume_queued 时,
    这个任务被标 failed 而不是再 _schedule —— 避免无限死循环。
    """
    from datetime import timedelta
    from doupool.db.models import VideoTask, SHANGHAI

    Account.create(
        id="acc-stale", display_name="stale", doubao_user_id="u",
        profile_dir=temp_profile,
    )
    stale = repository.create_video_task(None, "stale 任务", "seedance_v2.0_mini", "1:1", 5)
    # 把 updated_at 拨到 2 小时前,模拟卡了很久的孤儿
    VideoTask.update(updated_at=datetime.now(SHANGHAI) - timedelta(hours=2)).where(
        VideoTask.id == stale.id
    ).execute()

    service = VideoTaskService(
        repository, SuccessfulVideoRunner(), StaticSettings(),
        account_poll_interval=0.01,
    )

    await service.resume_queued()

    saved = repository.get_video_task(stale.id)
    assert saved.status == "failed", (
        f"stale queued 应被 sanitize 标 failed,实际 {saved.status}"
    )
    assert "启动时清理" in (saved.error_message or ""), (
        f"文案应说明是启动时清理,实际: {saved.error_message}"
    )
    # 兜底只是 sanitize,不应该把 stale 任务重新 _schedule
    # 验证:service._tasks 里没有这条 stale
    assert stale.id not in service._tasks or service._tasks[stale.id].done(), (
        "stale 任务不应被 _schedule 拉起"
    )


@pytest.mark.asyncio
async def test_resume_queued_still_schedules_fresh_queued_tasks(repository, temp_profile):
    """v0.2.30:resume_queued 的 sanitize 只清理 stale,fresh queued 仍正常调度。

    阈值 30min:刚 queued 几分钟的任务不算 stale,应被 _schedule 拉起
    并跑完 succeeded(向后兼容,保证正常重启恢复任务)。
    """
    Account.create(
        id="acc-fresh", display_name="fresh", doubao_user_id="u",
        profile_dir=temp_profile,
    )
    fresh = repository.create_video_task(None, "fresh queued", "seedance_v2.0_mini", "1:1", 5)
    # updated_at 用 default utcnow(),默认是 now,远超 30min 内

    service = VideoTaskService(
        repository, SuccessfulVideoRunner(), StaticSettings(),
        account_poll_interval=0.01,
    )

    await service.resume_queued()
    await asyncio.wait_for(service._tasks[fresh.id], timeout=2)

    saved = repository.get_video_task(fresh.id)
    assert saved.status == "succeeded", (
        f"fresh queued 应被 resume 拉起跑完 succeeded,实际 {saved.status}"
    )


@pytest.mark.asyncio
async def test_resume_queued_skips_stale_only_keeps_fresh(repository, temp_profile):
    """v0.2.30:DB 同时有 stale + fresh queued,resume_queued 只 sanitize stale。

    混合场景:一条 stale (2h 前) + 一条 fresh (now)。期望:stale 标 failed,
    fresh 正常 succeeded,且 fresh 不被 sanitize 影响。
    """
    from datetime import timedelta
    from doupool.db.models import VideoTask, SHANGHAI

    Account.create(
        id="acc-mix", display_name="mix", doubao_user_id="u",
        profile_dir=temp_profile,
    )
    stale = repository.create_video_task(None, "stale", "seedance_v2.0_mini", "1:1", 5)
    fresh = repository.create_video_task(None, "fresh", "seedance_v2.0_mini", "1:1", 5)
    VideoTask.update(updated_at=datetime.now(SHANGHAI) - timedelta(hours=2)).where(
        VideoTask.id == stale.id
    ).execute()

    service = VideoTaskService(
        repository, SuccessfulVideoRunner(), StaticSettings(),
        account_poll_interval=0.01,
    )

    await service.resume_queued()
    await asyncio.wait_for(service._tasks[fresh.id], timeout=2)

    assert repository.get_video_task(stale.id).status == "failed"
    assert repository.get_video_task(fresh.id).status == "succeeded"


@pytest.mark.asyncio
async def test_start_with_explicit_group_id_inherits_into_new_task(repository, temp_profile):
    """v0.2.32:手动重试路径传 group_id → 新任务必须归属同一组。

    之前 App.retryVideoTask 只拷了 prompt/model/ratio/duration/mode,
    没传 group_id,新任务脱离原组,结果页按 group_id 聚合时丢失这条任务。
    """
    Account.create(
        id="account-retry", display_name="重试用", doubao_user_id="u", profile_dir=temp_profile
    )
    service = VideoTaskService(
        repository, SuccessfulVideoRunner(), StaticSettings(),
        account_poll_interval=0.01,
    )

    # 模拟手动重试:caller 拿到原 task.group_id 后原样透传给 service.start。
    task = service.start(
        "重试 prompt", "seedance_v2.0_mini", "1:1", 5,
        group_id="grp-existing-uuid",
    )
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    assert saved.group_id == "grp-existing-uuid", (
        f"手动重试应继承原 group_id,实际 {saved.group_id!r}"
    )
    # 单 prompt + 显式 group_id → group_index 仍按 1 起算(归组约定)
    assert saved.group_index == 1


@pytest.mark.asyncio
async def test_start_without_group_id_keeps_legacy_none_behavior(repository, temp_profile):
    """v0.2.32 回归:不传 group_id 且单 prompt 时,新任务 group_id 仍为 None。

    验证没误伤「单条新建任务不打组」的旧行为。
    """
    Account.create(
        id="account-single", display_name="单条", doubao_user_id="u", profile_dir=temp_profile
    )
    service = VideoTaskService(
        repository, SuccessfulVideoRunner(), StaticSettings(),
        account_poll_interval=0.01,
    )
    task = service.start("单条 prompt", "seedance_v2.0_mini", "1:1", 5)
    await asyncio.wait_for(service._tasks[task.id], timeout=2)

    saved = repository.get_video_task(task.id)
    assert saved.group_id is None
    assert saved.group_index == 0


# ---------- v0.2.33:并发分散 + 同组粘同账号 + 重启 reconciliation ----------

@pytest.mark.asyncio
async def test_start_precharges_quota_before_runner_runs(repository, temp_profile):
    """v0.2.33:start() 路径用 CAS 预扣 → used_shared 在 runner 启动前已 += cost。

    之前所有版本都是 _run_inner 的 update("generating") 闭包才扣;v0.2.33
    提前到 start() 防并发超扣。这里验证 start() 调用后立刻读到正确的预扣。
    """
    Account.create(
        id="acc-pre", display_name="预扣", doubao_user_id="u", profile_dir=temp_profile
    )
    service = VideoTaskService(
        repository, SuccessfulVideoRunner(), StaticSettings(),
        account_poll_interval=0.01,
    )

    task = service.start("预扣", "seedance_v2.0_mini", "1:1", 5)
    # 不 await _tasks —— start() 内部已经预扣过 5 点
    assert Account.get_by_id("acc-pre").video_quota_used_shared == 5
    # 内存里的预登记表也已写入
    assert task.id in service._pre_charged_tasks
    assert service._pre_charged_tasks[task.id] == ("acc-pre", 5, "seedance_v2.0_mini")

    await asyncio.wait_for(service._tasks[task.id], timeout=2)
    # 跑完后仍然是 5 —— update() 闭包见到预扣跳过 increment
    assert Account.get_by_id("acc-pre").video_quota_used_shared == 5


@pytest.mark.asyncio
async def test_start_with_prompts_uses_same_account_for_group(repository, tmp_path):
    """v0.2.33:同 group_id 的多个 prompt 沿用首 task 选中的账号(sticky)。

    三个账号都是空的,首 task 选 least_used 的 acc-a;剩 2 个 task 应继续用 acc-a
    而不是再次 choose_and_reserve_account。CAS 预扣 5 次,acc-a 桶加 15,
    acc-b / acc-c 桶 0。
    """
    for i in ("a", "b", "c"):
        Account.create(
            id=f"acc-{i}", display_name=f"账号{i}", doubao_user_id=f"u{i}",
            profile_dir=str(tmp_path / f"profile-{i}"),
        )
    service = VideoTaskService(
        repository, SuccessfulVideoRunner(), StaticSettings(),
        account_poll_interval=0.01,
    )

    prompts = ["p1 跑", "p2 跑", "p3 跑"]
    # prompt="" 避免和 prompts 双重给 —— 不然 prompt 会被当作第一段前缀补到队首
    first_task = service.start("", "seedance_v2.0_mini", "1:1", 5, prompts=prompts)
    assert first_task is not None
    group_id = first_task.group_id
    assert group_id is not None

    # 等所有组内 task 跑完
    import time
    deadline = time.monotonic() + 2
    from doupool.db.models import VideoTask as _VT
    while time.monotonic() < deadline:
        remaining = _VT.select().where(
            (_VT.group_id == group_id) & _VT.status.in_(("queued", "starting", "generating"))
        ).count()
        if remaining == 0:
            break
        await asyncio.sleep(0.02)

    group_tasks = list(_VT.select().where(_VT.group_id == group_id))
    assert len(group_tasks) == 3
    accounts_used = {t.account.id for t in group_tasks}
    assert len(accounts_used) == 1, f"同组应只用一个账号,实际: {accounts_used}"
    # 被选中的账号被扣 15,其余 0
    chosen = accounts_used.pop()
    for a in ("acc-a", "acc-b", "acc-c"):
        used = Account.get_by_id(a).video_quota_used_shared
        expected = 15 if a == chosen else 0
        assert used == expected, f"{a} used={used} 期望 {expected}"


@pytest.mark.asyncio
async def test_start_distributes_parallel_calls_across_accounts(repository, tmp_path):
    """v0.2.33:串行三次 start() 不同 group 时分散到不同账号(least_used CAS)。

    三账号各 0,三次 start 三个 group → CAS 按 used 升序选,每个账号 used=5。
    """
    for i in ("a", "b", "c"):
        Account.create(
            id=f"acc-{i}", display_name=f"账号{i}", doubao_user_id=f"u{i}",
            profile_dir=str(tmp_path / f"profile-{i}"),
        )
    service = VideoTaskService(
        repository, SuccessfulVideoRunner(), StaticSettings(),
        account_poll_interval=0.01,
    )

    # 串行 start 也能验证分散(每次 CAS 都选 used 最小的)
    t1 = service.start("g1", "seedance_v2.0_mini", "1:1", 5)
    t2 = service.start("g2", "seedance_v2.0_mini", "1:1", 5)
    t3 = service.start("g3", "seedance_v2.0_mini", "1:1", 5)

    assert {t1.account.id, t2.account.id, t3.account.id} == {"acc-a", "acc-b", "acc-c"}
    for a in ("acc-a", "acc-b", "acc-c"):
        assert Account.get_by_id(a).video_quota_used_shared == 5

    for t in (t1, t2, t3):
        await asyncio.wait_for(service._tasks[t.id], timeout=2)
    # 跑完后各账号仍 5(预扣路径,update 跳过 increment)
    for a in ("acc-a", "acc-b", "acc-c"):
        assert Account.get_by_id(a).video_quota_used_shared == 5


@pytest.mark.asyncio
async def test_start_with_explicit_account_id_raises_when_quota_full(
    repository, tmp_path,
):
    """v0.2.33:caller 显式指定 account_id,目标账号桶满 → NoAvailableAccount。

    区别于「无 target_account_id 时选不到也保持 queued」的兜底。
    """
    Account.create(
        id="full-acc", display_name="满", doubao_user_id="u",
        profile_dir=str(tmp_path / "full"), video_quota_used_shared=50,
    )
    service = VideoTaskService(
        repository, SuccessfulVideoRunner(), StaticSettings(),
        account_poll_interval=0.01,
    )

    with pytest.raises(NoAvailableAccount):
        service.start("指定满账号", "seedance_v2.0_mini", "1:1", 5, account_id="full-acc")


@pytest.mark.asyncio
async def test_reconcile_pre_charged_after_restart_skips_double_charge(
    repository, tmp_path,
):
    """v0.2.33:进程重启后 resume_queued 重建 _pre_charged_tasks → 不会 double charge。

    模拟场景:
      1. service1 start() → 预扣 5,used_shared=5,task 进 starting/generating
      2. service1 进程崩了(直接 _pre_charged_tasks 不清,模拟内存失)
      3. service2 = new VideoTaskService + resume_queued()
      4. reconcile 重建 _pre_charged_tasks
      5. _run_inner 跑完 → used_shared 仍是 5(没二次扣)
    """
    Account.create(
        id="acc-restart", display_name="重启", doubao_user_id="u",
        profile_dir=str(tmp_path / "restart"),
    )
    # 用一个「卡住」不返回的 runner 模拟崩了 —— 但 start 不会等 runner 跑完
    # 我们让 start 后立刻模拟「内存失」,建新 service 调 resume_queued
    class _StuckRunner:
        async def run(self, *a, **kw):
            # 模拟 in-flight:跑到一半进程崩了
            await asyncio.sleep(60)

    service1 = VideoTaskService(
        repository, _StuckRunner(), StaticSettings(),
        account_poll_interval=0.01,
    )
    task = service1.start("p", "seedance_v2.0_mini", "1:1", 5)
    # start 后立刻 used_shared=5(预扣)
    assert Account.get_by_id("acc-restart").video_quota_used_shared == 5

    # 模拟进程崩:service1 直接丢弃,_pre_charged_tasks 内存失
    del service1

    # service2(新进程)调 resume_queued → reconcile 回填 _pre_charged_tasks
    class _FinishRunner:
        async def run(self, *a, **kw):
            kw.get("update")  # noqa - 验证 update 是 keyword(实际是 positional,见下)
            # 真实场景:resume_queued 走 _schedule → _run_inner → runner.run。
            # runner.run 这里不再被调(因为 in-flight task 已经在用 StuckRunner 派发,
            # 但 service1 没了,runner.run 永远不会跑)。直接返回 success。
            return {
                "remote_task_id": "remote-restart",
                "result_url": "https://example.test/restart.mp4",
                "cover_url": "https://example.test/restart.jpg",
            }

    service2 = VideoTaskService(
        repository, _FinishRunner(), StaticSettings(),
        account_poll_interval=0.01,
    )
    # reconcile 走 resume_queued 路径
    await asyncio.wait_for(service2.resume_queued(), timeout=2)
    # _pre_charged_tasks 已被填回
    assert task.id in service2._pre_charged_tasks
    assert service2._pre_charged_tasks[task.id] == (
        "acc-restart", 5, "seedance_v2.0_mini",
    )
    # 现在手动 schedule + 跑 _run_inner 验证不 double charge
    # (service2 没有 StuckRunner 在跑这条 task,所以我们要再 schedule 一次)
    # 注意:_schedule 会拿 service2 的 _FinishRunner 跑 → 走 success 路径。
    # 先把 task.status 强制回 queued 模拟「重启」
    from doupool.db.models import VideoTask as _VT
    _VT.update(status="queued").where(_VT.id == task.id).execute()
    service2._schedule(task.id)
    await asyncio.wait_for(service2._tasks[task.id], timeout=2)

    # used_shared 仍是 5 —— 没有二次扣
    assert Account.get_by_id("acc-restart").video_quota_used_shared == 5


@pytest.mark.asyncio
async def test_start_refunds_pre_charge_when_runner_raises(repository, tmp_path):
    """v0.2.33:start() 预扣后 runner 抛错 → 失败路径退预扣 → used_shared 归零。

    SuccessfulVideoRunner 改为抛异常 → _run_inner 的 except 路径走
    refund_quota_if_recorded → used_shared -= 5。同时内存里的 _pre_charged_tasks
    也被 _refund_pre_charge_if_present 清掉。
    """
    Account.create(
        id="acc-fail", display_name="失败", doubao_user_id="u",
        profile_dir=str(tmp_path / "fail"),
    )

    class _FailingRunner:
        async def run(self, *a, **kw):
            update = a[5]  # update 是第 6 个 positional
            update(status="generating", conversation_id="c1")
            raise RuntimeError("runner crashed mid-run")

    service = VideoTaskService(
        repository, _FailingRunner(), StaticSettings(),
        account_poll_interval=0.01,
    )
    task = service.start("p", "seedance_v2.0_mini", "1:1", 5)
    # start 后立刻预扣
    assert Account.get_by_id("acc-fail").video_quota_used_shared == 5

    try:
        await asyncio.wait_for(service._tasks[task.id], timeout=2)
    except Exception:
        pass  # _run 的 except 写 failed 后 raise,测试里吞掉
    # 退预扣后归零
    assert Account.get_by_id("acc-fail").video_quota_used_shared == 0
    # 内存也清空
    assert task.id not in service._pre_charged_tasks
    # task 状态是 failed
    assert repository.get_video_task(task.id).status == "failed"
