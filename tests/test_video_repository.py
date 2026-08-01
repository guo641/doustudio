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

    assert repository.choose_available_account().id == expected.id


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
        profile_dir=temp_profile, video_quota_used=5, video_quota_date=date(2026, 7, 12),
        video_limited_until=datetime(2026, 7, 12, 16, 0),
    )
    task = repository.create_video_task(None, "测试", "seedance_v2.0_mini", "1:1", 5)

    repository.reset_daily_quotas(date(2026, 7, 13))
    repository.assign_video_task(task.id, account.id)

    account = Account.get_by_id(account.id)
    task = repository.get_video_task(task.id)
    assert account.video_quota_used == 0
    assert account.video_limited_until is None
    assert task.account.id == account.id
