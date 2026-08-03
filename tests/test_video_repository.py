from datetime import date, datetime

from doupool.db.models import Account


def test_create_and_complete_video_task(repository, temp_profile):
    account = Account.create(
        id="account-1",
        display_name="测试账号",
        doubao_user_id="user-1",
        profile_dir=temp_profile,
    )

    task = repository.create_video_task(
        account_id=account.id,
        prompt="一只猫在草地上行走",
        model="seedance_v2.0_mini",
        ratio="1:1",
        duration=5,
    )
    repository.update_video_task(
        task.id,
        status="succeeded",
        conversation_id="conversation-1",
        remote_task_id="remote-1",
        vid="video-1",
        result_url="https://example.test/video.mp4",
        backup_result_url="https://backup.example.test/video.mp4",
        fallback_result_url="https://watermark.example.test/video.mp4",
        cover_url="https://example.test/cover.jpg",
    )

    saved = repository.get_video_task(task.id)
    assert saved.status == "succeeded"
    assert saved.account.id == "account-1"
    assert saved.result_url.endswith("video.mp4")
    assert saved.vid == "video-1"
    assert repository.list_video_tasks()[0].id == task.id


def test_choose_available_account_ignores_disabled(repository, temp_profile):
    Account.create(
        id="disabled",
        display_name="停用账号",
        doubao_user_id="user-disabled",
        profile_dir=temp_profile,
        enabled=False,
        status="disabled",
    )
    expected = Account.create(
        id="active",
        display_name="可用账号",
        doubao_user_id="user-active",
        profile_dir=temp_profile,
    )

    assert repository.choose_available_account({"mini": 5, "v2": 5, "std": 5}, model="seedance_v2.0_mini").id == expected.id


def test_queued_task_can_wait_without_an_account(repository):
    task = repository.create_video_task(
        account_id=None,
        prompt="等待账号",
        model="seedance_v2.0_mini",
        ratio="1:1",
        duration=5,
    )

    assert task.account_id is None
    assert repository.list_video_tasks()[0].account_id is None


def test_account_quota_reset_and_assignment(repository, temp_profile):
    account = Account.create(
        id="quota-account", display_name="额度账号", doubao_user_id="quota-user",
        profile_dir=temp_profile, video_quota_used_mini=5, video_quota_date=date(2026, 7, 12),
        video_limited_until=datetime(2026, 7, 12, 16, 0),
    )
    task = repository.create_video_task(None, "测试", "seedance_v2.0_mini", "1:1", 5)

    repository.reset_daily_quotas(date(2026, 7, 13))
    repository.assign_video_task(task.id, account.id)

    account = Account.get_by_id(account.id)
    task = repository.get_video_task(task.id)
    assert account.video_quota_used_mini == 0
    assert account.video_quota_used_v2 == 0
    assert account.video_quota_used_std == 0
    assert account.video_limited_until is None
    assert task.account.id == account.id


# ---------- v0.2.9:per-model quota bucket ----------

def test_choose_available_account_filters_by_model_bucket(repository, temp_profile):
    """v0.2.9:mini 桶满员的账号,不该被 std 任务选中(每个模型独立 quota)。"""
    full_mini = Account.create(
        id="full-mini", display_name="mini 满", doubao_user_id="u1",
        profile_dir=temp_profile, video_quota_used_mini=5,
    )
    open_std = Account.create(
        id="open-std", display_name="std 满", doubao_user_id="u2",
        profile_dir=temp_profile, video_quota_used_std=4,
    )
    quotas = {"mini": 5, "v2": 5, "std": 5}

    # std 任务:full_mini 还在 mini 桶满,std 桶 0 → 应当被选中
    assert repository.choose_available_account(quotas, model="seedance_v2.0_std").id == full_mini.id
    # mini 任务:open_std 还没用 mini 桶,但 mini 桶已被选走(full_mini 是 5)→ 退到 open_std
    assert repository.choose_available_account(quotas, model="seedance_v2.0_mini").id == open_std.id


def test_increment_account_quota_targets_model_bucket(repository, temp_profile):
    """v0.2.9:不同 model 互不影响桶。"""
    account = Account.create(
        id="acc", display_name="a", doubao_user_id="u", profile_dir=temp_profile,
    )
    repository.increment_account_quota(account.id, model="seedance_v2.0_mini")
    repository.increment_account_quota(account.id, model="seedance_v2.0_mini")
    repository.increment_account_quota(account.id, model="seedance_v2.0_std")

    refreshed = Account.get_by_id(account.id)
    # 增量按桶分别计
    assert refreshed.video_quota_used_mini == 2
    # v2 桶不受 mini/std 任务影响
    assert refreshed.video_quota_used_v2 == 0
    assert refreshed.video_quota_used_std == 1


def test_mark_account_limited_zeroes_all_buckets(repository, temp_profile):
    """v0.2.9:豆包 423 限流封整号 — 三桶一并 cap,任意模型都不可再选。"""
    account = Account.create(
        id="acc-l", display_name="l", doubao_user_id="u", profile_dir=temp_profile,
        video_quota_used_mini=1, video_quota_used_v2=2, video_quota_used_std=3,
    )
    quotas = {"mini": 5, "v2": 5, "std": 5}
    until = datetime(2026, 7, 13, 16, 0)
    repository.mark_account_limited(account.id, until, quotas)

    refreshed = Account.get_by_id(account.id)
    assert refreshed.video_quota_used_mini == 5
    assert refreshed.video_quota_used_v2 == 5
    assert refreshed.video_quota_used_std == 5
    assert refreshed.video_limited_until == until
    # 任意桶都不可再选
    assert repository.choose_available_account(quotas, model="seedance_v2.0_mini") is None
    assert repository.choose_available_account(quotas, model="seedance_v2.0") is None
    assert repository.choose_available_account(quotas, model="seedance_v2.0_std") is None


def test_increment_account_quota_rejects_unknown_model(repository, temp_profile):
    """v0.2.9:非法 model 不能悄悄扣错桶。"""
    account = Account.create(
        id="acc-nk", display_name="nk", doubao_user_id="u", profile_dir=temp_profile,
    )
    import pytest

    with pytest.raises(ValueError, match="unsupported model"):
        repository.increment_account_quota(account.id, model="foobar_baz")


# ---------- v0.2.11:per-model quota 按 by=N 增量 + 任务删除 ----------

def test_increment_account_quota_by_accumulates(repository, temp_profile):
    """v0.2.11:by=2/3/1 累加到对应桶,不是简单的 +1 循环。"""
    account = Account.create(
        id="acc-by", display_name="by", doubao_user_id="u", profile_dir=temp_profile,
    )
    repository.increment_account_quota(account.id, model="seedance_v2.0_mini", by=2)
    repository.increment_account_quota(account.id, model="seedance_v2.0_mini", by=3)
    repository.increment_account_quota(account.id, model="seedance_v2.0_std", by=1)

    refreshed = Account.get_by_id(account.id)
    assert refreshed.video_quota_used_mini == 5
    assert refreshed.video_quota_used_std == 1
    # v2 桶没碰
    assert refreshed.video_quota_used_v2 == 0


def test_increment_account_quota_by_default_is_one(repository, temp_profile):
    """v0.2.11:不传 by 默认 1,保持向后兼容。"""
    account = Account.create(
        id="acc-d", display_name="d", doubao_user_id="u", profile_dir=temp_profile,
    )
    repository.increment_account_quota(account.id, model="seedance_v2.0_mini")
    repository.increment_account_quota(account.id, model="seedance_v2.0_mini")

    assert Account.get_by_id(account.id).video_quota_used_mini == 2


def test_increment_account_quota_by_rejects_zero_or_negative(repository, temp_profile):
    """v0.2.11:by 必须 >= 1,避免被传 0 静默无操作。"""
    import pytest

    account = Account.create(
        id="acc-bad", display_name="bad", doubao_user_id="u", profile_dir=temp_profile,
    )
    with pytest.raises(ValueError, match="by must be >= 1"):
        repository.increment_account_quota(account.id, model="seedance_v2.0_mini", by=0)
    with pytest.raises(ValueError, match="by must be >= 1"):
        repository.increment_account_quota(account.id, model="seedance_v2.0_mini", by=-3)


def test_delete_video_task_removes_row(repository):
    """v0.2.11:delete_video_task 物理删除。"""
    from doupool.db.models import VideoTask

    task = repository.create_video_task(
        None, "删我", "seedance_v2.0_mini", "1:1", 5,
    )
    repository.delete_video_task(task.id)
    assert VideoTask.select().count() == 0


def test_is_task_deletable_true_for_terminal_and_queued(repository):
    """v0.2.11:queued/succeeded/failed/cancelled 都能删。"""
    for status in ("queued", "succeeded", "failed", "limited", "cancelled", "rechecking"):
        task = repository.create_video_task(None, status, "seedance_v2.0_mini", "1:1", 5)
        repository.update_video_task(task.id, status=status)
        assert repository.is_task_deletable(task.id) is True, status


def test_is_task_deletable_false_for_running(repository):
    """v0.2.11:starting/generating/resolving 不能删。"""
    for status in ("starting", "generating", "resolving"):
        task = repository.create_video_task(None, status, "seedance_v2.0_mini", "1:1", 5)
        repository.update_video_task(task.id, status=status)
        assert repository.is_task_deletable(task.id) is False, status


def test_is_task_deletable_false_for_missing(repository):
    """v0.2.11:不存在的 task_id 返回 False(让上层走 404 分支)。"""
    assert repository.is_task_deletable("nonexistent") is False
