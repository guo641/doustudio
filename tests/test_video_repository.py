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
    """v0.2.9:豆包 423 限流封整号 — 三桶一并 cap,任意模型都不可再选。
    v0.2.13:同时写 video_quota_date=business_date,确保 reset_daily_quotas 跨天能清。"""
    account = Account.create(
        id="acc-l", display_name="l", doubao_user_id="u", profile_dir=temp_profile,
        video_quota_used_mini=1, video_quota_used_v2=2, video_quota_used_std=3,
    )
    quotas = {"mini": 5, "v2": 5, "std": 5}
    until = datetime(2026, 7, 13, 16, 0)
    repository.mark_account_limited(
        account.id, until, quotas, business_date=date(2026, 7, 13)
    )

    refreshed = Account.get_by_id(account.id)
    assert refreshed.video_quota_used_mini == 5
    assert refreshed.video_quota_used_v2 == 5
    assert refreshed.video_quota_used_std == 5
    assert refreshed.video_limited_until == until
    assert refreshed.video_quota_date == date(2026, 7, 13)  # v0.2.13 锚定
    # 任意桶都不可再选
    assert repository.choose_available_account(quotas, model="seedance_v2.0_mini") is None
    assert repository.choose_available_account(quotas, model="seedance_v2.0") is None
    assert repository.choose_available_account(quotas, model="seedance_v2.0_std") is None


def test_mark_account_limited_without_business_date_is_backward_compatible(repository, temp_profile):
    """v0.2.13:business_date=None 时不写 video_quota_date,跟老调用兼容。"""
    account = Account.create(
        id="acc-bc", display_name="bc", doubao_user_id="bc", profile_dir=temp_profile,
        video_quota_date=date(2026, 7, 10),  # 故意写一个旧值
    )
    quotas = {"mini": 5, "v2": 5, "std": 5}
    repository.mark_account_limited(account.id, datetime(2026, 7, 13, 16, 0), quotas)
    refreshed = Account.get_by_id(account.id)
    # 没传 business_date → video_quota_date 保持原值不动
    assert refreshed.video_quota_date == date(2026, 7, 10)


def test_reset_daily_quotas_clears_expired_limited_until(repository, temp_profile):
    """v0.2.12:`mark_account_limited` 把桶 cap 到 quota_limit,
    如果 limited_until 在当天内到期,旧实现会让账号永久不可选。
    `reset_daily_quotas` 现在顺带清掉已过期的 limited_until + 三桶归零。"""
    account = Account.create(
        id="acc-recover", display_name="r", doubao_user_id="u-recover",
        profile_dir=temp_profile, video_quota_date=date(2026, 7, 13),
    )
    quotas = {"mini": 5, "v2": 5, "std": 5}
    # 假设豆包 423 封到 16:00
    repository.mark_account_limited(account.id, datetime(2026, 7, 13, 16, 0), quotas)
    assert repository.choose_available_account(quotas, model="seedance_v2.0_mini") is None

    # 当天 21:00 — limited_until 已过期 5 小时
    repository.reset_daily_quotas(date(2026, 7, 13), now=datetime(2026, 7, 13, 21, 0))

    refreshed = Account.get_by_id(account.id)
    assert refreshed.video_quota_used_mini == 0
    assert refreshed.video_quota_used_v2 == 0
    assert refreshed.video_quota_used_std == 0
    assert refreshed.video_limited_until is None
    # 账号重新可选
    picked = repository.choose_available_account(quotas, model="seedance_v2.0_mini")
    assert picked is not None
    assert picked.id == account.id


def test_reset_daily_quotas_does_not_touch_active_limited_until(repository, temp_profile):
    """v0.2.12:limited_until 还在未来时不要清桶,封号期别被中途放出来。"""
    account = Account.create(
        id="acc-active-lim", display_name="a", doubao_user_id="u-al",
        profile_dir=temp_profile, video_quota_date=date(2026, 7, 13),
    )
    quotas = {"mini": 5, "v2": 5, "std": 5}
    future = datetime(2026, 7, 14, 0, 0)  # 次日凌晨才到期
    repository.mark_account_limited(account.id, future, quotas)

    repository.reset_daily_quotas(date(2026, 7, 13), now=datetime(2026, 7, 13, 21, 0))

    refreshed = Account.get_by_id(account.id)
    assert refreshed.video_limited_until == future
    assert refreshed.video_quota_used_mini == 5
    assert repository.choose_available_account(quotas, model="seedance_v2.0_mini") is None


def test_reset_daily_quotas_clears_expired_regardless_of_date(repository, temp_profile):
    """v0.2.13:用户改 quota_reset_time 后,旧 limited_until 落在当天未来,跨天第一段
    命中靠 video_quota_date=business_date 锚定;但若 date 不匹配 + limited_until 已
    过期的极端组合,第二段(纯看 limited_until <= now)也要清桶,不能漏。"""
    account = Account.create(
        id="acc-mismatch", display_name="m", doubao_user_id="u-mm",
        profile_dir=temp_profile,
        video_quota_date=date(2026, 7, 12),  # 故意旧 date,跟 business_date 不匹配
    )
    quotas = {"mini": 5, "v2": 5, "std": 5}
    # 旧的 limited_until 已经过期(13 号 16:00 < 14 号中午)
    repository.mark_account_limited(
        account.id, datetime(2026, 7, 13, 16, 0), quotas,
        business_date=date(2026, 7, 12),
    )
    # 跨天 + limited_until 已过期 —— 第一段(date != business_date)和第二段
    # (limited_until <= now)都会命中;两段都做清桶,幂等无副作用
    repository.reset_daily_quotas(date(2026, 7, 14), now=datetime(2026, 7, 14, 12, 0))

    refreshed = Account.get_by_id(account.id)
    assert refreshed.video_quota_used_mini == 0
    assert refreshed.video_quota_used_v2 == 0
    assert refreshed.video_quota_used_std == 0
    assert refreshed.video_limited_until is None
    assert refreshed.video_quota_date == date(2026, 7, 14)
    # 账号重新可选
    picked = repository.choose_available_account(quotas, model="seedance_v2.0_mini")
    assert picked is not None
    assert picked.id == account.id


def test_summarize_account_availability_counts_buckets(repository, temp_profile):
    """v0.2.12:UI 需要区分「没有账号」vs「全部用完」,summary 计数要准。"""
    quotas = {"mini": 5, "v2": 5, "std": 5}
    now = datetime(2026, 7, 13, 12, 0)
    # 0 个账号
    assert repository.summarize_account_availability(quotas, "seedance_v2.0_mini", now=now) == {
        "enabled_total": 0, "bucket_full": 0
    }
    # 1 个活跃账号,桶空
    Account.create(id="acc-ok", display_name="ok", doubao_user_id="ok", profile_dir=temp_profile)
    assert repository.summarize_account_availability(quotas, "seedance_v2.0_mini", now=now) == {
        "enabled_total": 1, "bucket_full": 0
    }
    # 1 个 disabled + 1 个 active 桶满
    Account.create(
        id="acc-dis", display_name="dis", doubao_user_id="dis",
        profile_dir=temp_profile, enabled=False,
    )
    full = Account.create(
        id="acc-full", display_name="full", doubao_user_id="full", profile_dir=temp_profile,
    )
    repository.mark_account_limited(full.id, datetime(2026, 7, 13, 16, 0), quotas)
    stats = repository.summarize_account_availability(quotas, "seedance_v2.0_mini", now=now)
    assert stats["enabled_total"] == 2  # ok + full(dis 不算)
    assert stats["bucket_full"] == 1


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
