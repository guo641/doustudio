from datetime import date, datetime, timedelta

from doupool.db.models import Account, VideoTask


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

    # v0.2.29:共享额度池,quotas 只用 shared 一桶。
    assert repository.choose_available_account({"shared": 50}, model="seedance_v2.0_mini").id == expected.id


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
        profile_dir=temp_profile, video_quota_used_shared=5, video_quota_date=date(2026, 7, 12),
        video_limited_until=datetime(2026, 7, 12, 16, 0),
    )
    task = repository.create_video_task(None, "测试", "seedance_v2.0_mini", "1:1", 5)

    repository.reset_daily_quotas(date(2026, 7, 13))
    repository.assign_video_task(task.id, account.id)

    account = Account.get_by_id(account.id)
    task = repository.get_video_task(task.id)
    # v0.2.29:reset 只清 shared 桶,旧三桶不再写入。
    assert account.video_quota_used_shared == 0
    assert account.video_limited_until is None
    assert task.account.id == account.id


# ---------- v0.2.29:共享额度池 ----------


def test_choose_available_account_filters_by_shared_bucket(repository, temp_profile):
    """v0.2.29:mini 桶满员的账号,任意模型任务都不该再被选中(共享池)。"""
    full_shared = Account.create(
        id="full-shared", display_name="shared 满", doubao_user_id="u1",
        profile_dir=temp_profile, video_quota_used_shared=50,
    )
    open_shared = Account.create(
        id="open-shared", display_name="open", doubao_user_id="u2",
        profile_dir=temp_profile, video_quota_used_shared=4,
    )
    quotas = {"shared": 50}

    # std 任务:full_shared shared=50 满 → 退到 open_shared
    assert repository.choose_available_account(quotas, model="seedance_v2.0_std").id == open_shared.id
    # mini 任务:同上(共享池不分模型)
    assert repository.choose_available_account(quotas, model="seedance_v2.0_mini").id == open_shared.id


def test_increment_account_quota_targets_shared_bucket(repository, temp_profile):
    """v0.2.29:不同 model 都扣同一个 shared 桶,参数 model 仅作日志。"""
    account = Account.create(
        id="acc", display_name="a", doubao_user_id="u", profile_dir=temp_profile,
    )
    repository.increment_account_quota(account.id, model="seedance_v2.0_mini")
    repository.increment_account_quota(account.id, model="seedance_v2.0_mini")
    repository.increment_account_quota(account.id, model="seedance_v2.0_std")

    refreshed = Account.get_by_id(account.id)
    # 共享池累计到 3(无论什么 model)
    assert refreshed.video_quota_used_shared == 3


def test_mark_account_limited_caps_shared_bucket(repository, temp_profile):
    """v0.2.29:豆包 423 限流封整号 → cap shared 桶,任意模型都不可再选。"""
    account = Account.create(
        id="acc-l", display_name="l", doubao_user_id="u", profile_dir=temp_profile,
        video_quota_used_shared=1,
    )
    quotas = {"shared": 50}
    until = datetime(2026, 7, 13, 16, 0)
    repository.mark_account_limited(
        account.id, until, quotas, business_date=date(2026, 7, 13)
    )

    refreshed = Account.get_by_id(account.id)
    assert refreshed.video_quota_used_shared == 50
    assert refreshed.video_limited_until == until
    assert refreshed.video_quota_date == date(2026, 7, 13)
    # 任意模型任务都不可再选(共享池)
    assert repository.choose_available_account(quotas, model="seedance_v2.0_mini") is None
    assert repository.choose_available_account(quotas, model="seedance_v2.0") is None
    assert repository.choose_available_account(quotas, model="seedance_v2.0_std") is None


def test_mark_account_limited_without_business_date_is_backward_compatible(repository, temp_profile):
    """v0.2.29:business_date=None 时不写 video_quota_date,跟老调用兼容。"""
    account = Account.create(
        id="acc-bc", display_name="bc", doubao_user_id="bc", profile_dir=temp_profile,
        video_quota_date=date(2026, 7, 10),  # 故意写一个旧值
    )
    quotas = {"shared": 50}
    repository.mark_account_limited(account.id, datetime(2026, 7, 13, 16, 0), quotas)
    refreshed = Account.get_by_id(account.id)
    # 没传 business_date → video_quota_date 保持原值不动
    assert refreshed.video_quota_date == date(2026, 7, 10)


def test_reset_daily_quotas_clears_expired_limited_until(repository, temp_profile):
    """v0.2.12:`mark_account_limited` 把 shared 桶 cap 到 quota_limit,
    如果 limited_until 在当天内到期,旧实现会让账号永久不可选。
    `reset_daily_quotas` 现在顺带清掉已过期的 limited_until + shared 桶归零。"""
    account = Account.create(
        id="acc-recover", display_name="r", doubao_user_id="u-recover",
        profile_dir=temp_profile,
        video_quota_date=date(2026, 7, 13),
    )
    quotas = {"shared": 50}
    # 假设豆包 423 封到 16:00
    repository.mark_account_limited(account.id, datetime(2026, 7, 13, 16, 0), quotas)
    assert repository.choose_available_account(quotas, model="seedance_v2.0_mini") is None

    # 当天 21:00 — limited_until 已过期 5 小时
    repository.reset_daily_quotas(date(2026, 7, 13), now=datetime(2026, 7, 13, 21, 0))

    refreshed = Account.get_by_id(account.id)
    assert refreshed.video_quota_used_shared == 0
    assert refreshed.video_limited_until is None
    # 账号重新可选
    picked = repository.choose_available_account(quotas, model="seedance_v2.0_mini")
    assert picked is not None
    assert picked.id == account.id


def test_reset_daily_quotas_does_not_touch_active_limited_until(repository, temp_profile):
    """v0.2.12:limited_until 还在未来时不要清桶,封号期别被中途放出来。"""
    account = Account.create(
        id="acc-active-lim", display_name="a", doubao_user_id="u-al",
        profile_dir=temp_profile,
        video_quota_date=date(2026, 7, 13),
    )
    quotas = {"shared": 50}
    future = datetime(2026, 7, 14, 0, 0)  # 次日凌晨才到期
    repository.mark_account_limited(account.id, future, quotas)

    repository.reset_daily_quotas(date(2026, 7, 13), now=datetime(2026, 7, 13, 21, 0))

    refreshed = Account.get_by_id(account.id)
    assert refreshed.video_limited_until == future
    assert refreshed.video_quota_used_shared == 50
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
    quotas = {"shared": 50}
    # 旧的 limited_until 已经过期(13 号 16:00 < 14 号中午)
    repository.mark_account_limited(
        account.id, datetime(2026, 7, 13, 16, 0), quotas,
        business_date=date(2026, 7, 12),
    )
    # 跨天 + limited_until 已过期 —— 第一段(date != business_date)和第二段
    # (limited_until <= now)都会命中;两段都做清桶,幂等无副作用
    repository.reset_daily_quotas(date(2026, 7, 14), now=datetime(2026, 7, 14, 12, 0))

    refreshed = Account.get_by_id(account.id)
    assert refreshed.video_quota_used_shared == 0
    assert refreshed.video_limited_until is None
    assert refreshed.video_quota_date == date(2026, 7, 14)
    # 账号重新可选
    picked = repository.choose_available_account(quotas, model="seedance_v2.0_mini")
    assert picked is not None
    assert picked.id == account.id


def test_summarize_account_availability_counts_shared_bucket(repository, temp_profile):
    """v0.2.29:UI 需要区分「没有账号」vs「全部用完」,summary 计数要准。
    共享池后 bucket_full = shared 桶满的账号数。"""
    quotas = {"shared": 50}
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


def test_increment_account_quota_by_accumulates(repository, temp_profile):
    """v0.2.29:by=2/3/1 累加到 shared 桶。"""
    account = Account.create(
        id="acc-by", display_name="by", doubao_user_id="u", profile_dir=temp_profile,
    )
    repository.increment_account_quota(account.id, model="seedance_v2.0_mini", by=2)
    repository.increment_account_quota(account.id, model="seedance_v2.0_mini", by=3)
    repository.increment_account_quota(account.id, model="seedance_v2.0_std", by=1)

    refreshed = Account.get_by_id(account.id)
    assert refreshed.video_quota_used_shared == 6


def test_increment_account_quota_by_default_is_one(repository, temp_profile):
    """v0.2.29:不传 by 默认 1,保持向后兼容。"""
    account = Account.create(
        id="acc-d", display_name="d", doubao_user_id="u", profile_dir=temp_profile,
    )
    repository.increment_account_quota(account.id, model="seedance_v2.0_mini")
    repository.increment_account_quota(account.id, model="seedance_v2.0_mini")

    assert Account.get_by_id(account.id).video_quota_used_shared == 2


def test_increment_account_quota_by_rejects_zero_or_negative(repository, temp_profile):
    """v0.2.29:by 必须 >= 1,避免被传 0 静默无操作。"""
    import pytest

    account = Account.create(
        id="acc-bad", display_name="bad", doubao_user_id="u", profile_dir=temp_profile,
    )
    with pytest.raises(ValueError, match="increment by must be >= 1"):
        repository.increment_account_quota(account.id, model="seedance_v2.0_mini", by=0)
    with pytest.raises(ValueError, match="increment by must be >= 1"):
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


def test_get_update_assign_video_task_returns_none_when_task_deleted(repository, temp_profile):
    """v0.2.15:DELETE 端点物理删除任务后,worker 还在 in-flight 时调用
    get_video_task / update_video_task / assign_video_task,不应抛
    VideoTaskDoesNotExist(peewee 包成 IndexError: list index out of range),
    而是静默返回 None / skip,让 service._run_inner 自己退出。
    """
    account = Account.create(
        id="account-1",
        display_name="测试账号",
        doubao_user_id="user-1",
        profile_dir=temp_profile,
    )
    task = repository.create_video_task(
        account.id, "P", "seedance_v2.0_mini", "1:1", 5,
    )
    repository.delete_video_task(task.id)
    # 三种调用都不抛,都返回 None
    assert repository.get_video_task(task.id) is None
    assert repository.update_video_task(task.id, status="queued") is None
    assert repository.assign_video_task(task.id, None) is None


def test_complete_login_resets_shared_bucket_for_existing_account(repository, temp_profile):
    """v0.2.29:已存在账号重新登录成功后,清 shared 桶 quota + 清 limited_until。

    v0.2.12 时代被 mark_account_limited cap 死的桶,v0.2.13 修了 reset_daily_quotas
    两段清桶,但装上 v0.2.13 之前已经死锁的桶只会等下次 423 / 跨天才解。
    「重新登录」是最稳的恢复点 —— 反正账号刚扫完码就能用,
    把桶清掉让账号立刻可调度,而不是让用户以为「登录失败」。
    """
    account = Account.create(
        id="acc-relogin", display_name="旧昵称", doubao_user_id="u-relogin",
        profile_dir=temp_profile,
        # 模拟被 cap 死的状态
        video_quota_used_shared=50,
        video_limited_until=datetime(2026, 8, 3, 16, 0),
        video_quota_date=date(2026, 8, 3),
        status="limited",
    )
    attempt = repository.create_login_attempt()
    quotas = {"shared": 50}

    # 重登录前:shared 满、limited_until 在未来 → 任何模型都选不到
    assert repository.choose_available_account(quotas, model="seedance_v2.0_mini") is None
    assert repository.choose_available_account(quotas, model="seedance_v2.0_std") is None

    # 重新登录成功(沿用原 doubao_user_id 命中现有账号,走 else 分支)
    refreshed = repository.complete_login(
        attempt.id,
        identity={"user_id": "u-relogin", "nickname": "新昵称"},
        profile_dir=temp_profile,
    )

    # shared 桶清零、limited_until 清空、昵称/状态都更新
    assert refreshed.video_quota_used_shared == 0
    assert refreshed.video_limited_until is None
    assert refreshed.status == "active"
    assert refreshed.doubao_nickname == "新昵称"

    # 登录后立刻可调度
    picked = repository.choose_available_account(quotas, model="seedance_v2.0_mini")
    assert picked is not None
    assert picked.id == account.id


def test_complete_login_creates_new_account_without_touching_others(repository, temp_profile):
    """v0.2.29:首次登录的新账号走 if 分支,不该动别的账号 quota。"""
    # 已存在的老账号,quota 满
    Account.create(
        id="acc-old", display_name="老", doubao_user_id="u-old",
        profile_dir=temp_profile,
        video_quota_used_shared=50,
        video_limited_until=datetime(2026, 8, 3, 16, 0),
    )
    attempt = repository.create_login_attempt()

    repository.complete_login(
        attempt.id,
        identity={"user_id": "u-new", "nickname": "新号"},
        profile_dir=temp_profile,
    )

    # 老账号 quota 不应该被新账号登录牵连清掉
    old = Account.get_by_id("acc-old")
    assert old.video_quota_used_shared == 50
    assert old.video_limited_until == datetime(2026, 8, 3, 16, 0)


# --- v0.2.29:decrement_account_quota (失败退还额度,共享池) ---


def test_decrement_account_quota_subtracts_from_shared_bucket(repository, temp_profile):
    """v0.2.29:退款都走 shared 桶(共享池下不分模型)。"""
    account = Account.create(
        id="acc-refund", display_name="refund", doubao_user_id="u",
        profile_dir=temp_profile,
        video_quota_used_shared=15,
    )
    repository.decrement_account_quota(account.id, model="seedance_v2.0_mini", by=10)

    refreshed = Account.get_by_id(account.id)
    assert refreshed.video_quota_used_shared == 5


def test_decrement_account_quota_clamps_at_zero(repository, temp_profile):
    """v0.2.29:跨天 reset 后再退款,used 已经是 0,不能被打成负数。"""
    account = Account.create(
        id="acc-clamp", display_name="clamp", doubao_user_id="u",
        profile_dir=temp_profile,
        video_quota_used_shared=0,
    )
    # shared 桶 0,退款 10 点不能变负
    repository.decrement_account_quota(account.id, model="seedance_v2.0_mini", by=10)

    refreshed = Account.get_by_id(account.id)
    assert refreshed.video_quota_used_shared == 0


def test_decrement_account_quota_rejects_invalid_by(repository, temp_profile):
    """v0.2.29:by 必须 >= 1,跟 increment 保持对称。"""
    import pytest
    account = Account.create(
        id="acc-bad-refund", display_name="br", doubao_user_id="u",
        profile_dir=temp_profile,
    )
    with pytest.raises(ValueError, match="decrement by must be >= 1"):
        repository.decrement_account_quota(account.id, model="seedance_v2.0_mini", by=0)
    with pytest.raises(ValueError, match="decrement by must be >= 1"):
        repository.decrement_account_quota(account.id, model="seedance_v2.0_mini", by=-5)


def test_increment_then_decrement_is_net_zero(repository, temp_profile):
    """v0.2.29:扣 10 点后退 10 点,shared 桶回到原值。完整闭环。"""
    account = Account.create(
        id="acc-roundtrip", display_name="rt", doubao_user_id="u",
        profile_dir=temp_profile,
        video_quota_used_shared=7,
    )
    repository.increment_account_quota(account.id, model="seedance_v2.0_mini", by=10)
    assert Account.get_by_id(account.id).video_quota_used_shared == 17
    repository.decrement_account_quota(account.id, model="seedance_v2.0_mini", by=10)
    assert Account.get_by_id(account.id).video_quota_used_shared == 7


# ---------- v0.2.29:共享池迁移 + 手动重置 ----------


def test_migrate_legacy_quota_buckets_sums_three_buckets(repository, temp_profile):
    """v0.2.29:启动迁移把老 mini+v2+std 一次性累加进 shared。

    A:shared=0 mini=10 v2=20 std=15 → 迁到 shared=45
    B:shared=5(已迁过) mini=0 v2=0 std=0 → 不动
    C:shared=0 全 0 → 不动
    """
    a = Account.create(
        id="acc-a", display_name="A", doubao_user_id="u-a",
        profile_dir=temp_profile,
        video_quota_used_mini=10, video_quota_used_v2=20, video_quota_used_std=15,
    )
    b = Account.create(
        id="acc-b", display_name="B", doubao_user_id="u-b",
        profile_dir=temp_profile,
        video_quota_used_shared=5,  # 已迁过
        video_quota_used_mini=0, video_quota_used_v2=0, video_quota_used_std=0,
    )
    c = Account.create(
        id="acc-c", display_name="C", doubao_user_id="u-c",
        profile_dir=temp_profile,
        video_quota_used_shared=0,
        video_quota_used_mini=0, video_quota_used_v2=0, video_quota_used_std=0,
    )

    migrated = repository.migrate_legacy_quota_buckets()
    assert migrated == 1

    a_after = Account.get_by_id(a.id)
    b_after = Account.get_by_id(b.id)
    c_after = Account.get_by_id(c.id)
    assert a_after.video_quota_used_shared == 45
    assert b_after.video_quota_used_shared == 5  # 不动
    assert c_after.video_quota_used_shared == 0  # 不动

    # 多次执行幂等
    migrated_again = repository.migrate_legacy_quota_buckets()
    assert migrated_again == 0


def test_reset_account_quota_clears_shared_bucket(repository, temp_profile):
    """v0.2.29:清单个账号 shared 桶 + limited_until。"""
    account = Account.create(
        id="acc-reset", display_name="r", doubao_user_id="u",
        profile_dir=temp_profile,
        video_quota_used_shared=30,
        video_limited_until=datetime(2026, 8, 6, 16, 0),
        video_quota_date=date(2026, 8, 5),
    )
    ok = repository.reset_account_quota(account.id, date(2026, 8, 6))
    assert ok is True

    refreshed = Account.get_by_id(account.id)
    assert refreshed.video_quota_used_shared == 0
    assert refreshed.video_limited_until is None
    assert refreshed.video_quota_date == date(2026, 8, 6)


def test_reset_account_quota_returns_false_for_missing_account(repository):
    """v0.2.29:账号不存在返 False(让 API 层返 404)。"""
    assert repository.reset_account_quota("nonexistent", date(2026, 8, 6)) is False


def test_reset_all_quotas_only_clears_enabled_accounts(repository, temp_profile):
    """v0.2.29:一键重置只清 enabled 账号 —— disabled 是用户显式关的,别自动清。"""
    enabled = Account.create(
        id="acc-enabled", display_name="enabled", doubao_user_id="u1",
        profile_dir=temp_profile, enabled=True,
        video_quota_used_shared=40, video_limited_until=datetime(2026, 8, 6, 16, 0),
    )
    disabled = Account.create(
        id="acc-disabled", display_name="disabled", doubao_user_id="u2",
        profile_dir=temp_profile, enabled=False,
        video_quota_used_shared=40, video_limited_until=datetime(2026, 8, 6, 16, 0),
    )

    count = repository.reset_all_quotas(date(2026, 8, 6))
    assert count == 1  # 只清了 enabled

    e = Account.get_by_id(enabled.id)
    d = Account.get_by_id(disabled.id)
    assert e.video_quota_used_shared == 0
    assert e.video_limited_until is None
    # disabled 不动
    assert d.video_quota_used_shared == 40
    assert d.video_limited_until == datetime(2026, 8, 6, 16, 0)


def test_reset_all_quotas_returns_zero_when_no_enabled_accounts(repository, temp_profile):
    """v0.2.29:没有 enabled 账号时返 0(前端 UI 显示「已重置 0 个」)。"""
    Account.create(
        id="acc-only-disabled", display_name="d", doubao_user_id="u",
        profile_dir=temp_profile, enabled=False,
        video_quota_used_shared=50,
    )
    assert repository.reset_all_quotas(date(2026, 8, 6)) == 0


def test_reset_account_quota_is_idempotent(repository, temp_profile):
    """v0.2.29:reset 多次执行幂等(用户狂点也不出事)。"""
    account = Account.create(
        id="acc-idem", display_name="i", doubao_user_id="u",
        profile_dir=temp_profile,
        video_quota_used_shared=20,
    )
    assert repository.reset_account_quota(account.id, date(2026, 8, 6)) is True
    assert repository.reset_account_quota(account.id, date(2026, 8, 6)) is True
    assert repository.reset_account_quota(account.id, date(2026, 8, 6)) is True
    assert Account.get_by_id(account.id).video_quota_used_shared == 0