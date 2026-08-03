from __future__ import annotations

from collections.abc import Mapping
import json
from datetime import date, datetime, timedelta
from uuid import uuid4

from peewee import JOIN, SqliteDatabase

from .models import Account, AppLog, AppSetting, LoginAttempt, VideoTask, utcnow


class AccountRepository:
    def __init__(self, database: SqliteDatabase):
        self.database = database

    def create_login_attempt(self, account_id: str | None = None) -> LoginAttempt:
        return LoginAttempt.create(id=str(uuid4()), account=account_id, state="created")

    def set_attempt_state(
        self,
        attempt_id: str,
        state: str,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> LoginAttempt:
        attempt = LoginAttempt.get_by_id(attempt_id)
        attempt.state = state
        attempt.error_code = error_code
        attempt.error_message = error_message
        if state in {"succeeded", "failed", "cancelled", "timed_out"}:
            attempt.finished_at = utcnow()
        attempt.save()
        return attempt

    def complete_login(
        self,
        attempt_id: str,
        identity: Mapping[str, str | None],
        profile_dir: str,
    ) -> Account:
        user_id = identity.get("user_id")
        if not user_id:
            raise ValueError(
                "identity.user_id is required "
                "(v0.2.7: 无法从 cookies/localStorage 提取, 请重新扫码登录)"
            )
        nickname = identity.get("nickname")
        now = utcnow()
        with self.database.atomic():
            account = Account.get_or_none(Account.doubao_user_id == user_id)
            if account is None:
                account = Account.create(
                    id=str(uuid4()),
                    display_name=nickname or f"豆包账号 {user_id[-4:]}",
                    doubao_user_id=user_id,
                    doubao_nickname=nickname,
                    profile_dir=profile_dir,
                    status="active",
                    last_verified_at=now,
                )
            else:
                account.doubao_nickname = nickname
                account.display_name = nickname or account.display_name
                account.profile_dir = profile_dir
                account.status = "active"
                account.last_verified_at = now
                account.last_error = None
                account.updated_at = now
                account.save()
            attempt = LoginAttempt.get_by_id(attempt_id)
            attempt.account = account
            attempt.state = "succeeded"
            attempt.finished_at = now
            attempt.save()
        return account

    def list_accounts(self) -> list[Account]:
        return list(Account.select().order_by(Account.created_at.desc()))

    def list_logs(self, limit: int = 200):
        return list(AppLog.select().order_by(AppLog.created_at.desc()).limit(limit))

    def clear_logs(self) -> int:
        return AppLog.delete().execute()

    def prune_logs(self, retention_days: int) -> int:
        return AppLog.delete().where(AppLog.created_at < utcnow() - timedelta(days=retention_days)).execute()

    def get_setting(self, key: str, default=None):
        item = AppSetting.get_or_none(AppSetting.key == key)
        return json.loads(item.value) if item else default

    def set_setting(self, key: str, value) -> None:
        AppSetting.insert(key=key, value=json.dumps(value), updated_at=utcnow()).on_conflict(
            conflict_target=(AppSetting.key,),
            update={AppSetting.value: json.dumps(value), AppSetting.updated_at: utcnow()},
        ).execute()

    def choose_available_account(
        self, daily_quota: int = 5, now: datetime | None = None, strategy: str = "least_used"
    ) -> Account | None:
        now = now or utcnow()
        query = (
            Account.select()
            .where(
                (Account.enabled == True)  # noqa: E712
                & (Account.status == "active")
                & (Account.video_quota_used < daily_quota)
                & ((Account.video_limited_until.is_null(True)) | (Account.video_limited_until <= now))
            )
        )
        if strategy == "round_robin":
            query = query.order_by(Account.updated_at.asc())
        else:
            query = query.order_by(Account.video_quota_used.asc(), Account.updated_at.asc())
        return query.first()

    def reset_daily_quotas(self, business_date: date) -> None:
        (Account.update(video_quota_used=0, video_quota_date=business_date, video_limited_until=None)
         .where((Account.video_quota_date.is_null(True)) | (Account.video_quota_date != business_date))
         .execute())

    def mark_account_limited(self, account_id: str, limited_until: datetime, daily_quota: int) -> None:
        (Account.update(video_quota_used=daily_quota, video_limited_until=limited_until, updated_at=utcnow())
         .where(Account.id == account_id).execute())

    def increment_account_quota(self, account_id: str) -> None:
        (Account.update(video_quota_used=Account.video_quota_used + 1, updated_at=utcnow())
         .where(Account.id == account_id).execute())

    def create_video_task(
        self,
        account_id: str | None,
        prompt: str,
        model: str,
        ratio: str,
        duration: int,
        *,
        mode: str = "t2v",
        image_paths: list[str] | None = None,
        group_id: str | None = None,
        group_index: int = 0,
        callback_url: str | None = None,
    ) -> VideoTask:
        with self.database.atomic():
            task = VideoTask.create(
                id=str(uuid4()),
                account=account_id,
                prompt=prompt,
                original_prompt=prompt,
                prompt_retry_count=0,
                model=model,
                ratio=ratio,
                duration=duration,
                mode=mode or "t2v",
                image_paths=json.dumps(image_paths or [], ensure_ascii=False) if image_paths else None,
                group_id=group_id,
                group_index=group_index,
                callback_url=callback_url,
                callback_status="pending" if callback_url else None,
            )
            if account_id:
                Account.update(updated_at=utcnow()).where(Account.id == account_id).execute()
        return task

    def list_task_groups(self, limit: int = 50) -> list[dict]:
        """聚合返回最近的有 group_id 的任务组,按组内首个任务时间倒序"""
        # 找出最近 N 个 group_id,以及每个组的任务数
        from peewee import fn
        rows = list(
            VideoTask.select(
                VideoTask.group_id,
                fn.MIN(VideoTask.created_at).alias("first_at"),
                fn.COUNT(VideoTask.id).alias("task_count"),
            )
            .where(VideoTask.group_id.is_null(False))
            .group_by(VideoTask.group_id)
            .order_by(fn.MIN(VideoTask.created_at).desc())
            .limit(limit)
        )
        return [
            {
                "group_id": r.group_id,
                "task_count": r.task_count,
                "first_at": r.first_at.isoformat() if r.first_at else None,
            }
            for r in rows
        ]

    def list_tasks_by_group(self, group_id: str) -> list[VideoTask]:
        return list(
            VideoTask.select(VideoTask, Account)
            .join(Account, JOIN.LEFT_OUTER)
            .where(VideoTask.group_id == group_id)
            .order_by(VideoTask.group_index.asc(), VideoTask.created_at.asc())
        )

    def assign_video_task(self, task_id: str, account_id: str | None) -> VideoTask:
        task = VideoTask.get_by_id(task_id)
        task.account = account_id
        task.updated_at = utcnow()
        task.save()
        if account_id:
            Account.update(updated_at=utcnow()).where(Account.id == account_id).execute()
        return task

    def get_video_task(self, task_id: str) -> VideoTask:
        return VideoTask.get_by_id(task_id)

    def list_video_tasks(self, limit: int = 200) -> list[VideoTask]:
        return list(
            VideoTask.select(VideoTask, Account)
            .join(Account, JOIN.LEFT_OUTER)
            .order_by(VideoTask.created_at.desc())
            .limit(limit)
        )

    def update_video_task(self, task_id: str, **values) -> VideoTask:
        task = VideoTask.get_by_id(task_id)
        for key, value in values.items():
            setattr(task, key, value)
        task.updated_at = utcnow()
        if task.status in {"succeeded", "failed", "cancelled"}:
            task.completed_at = utcnow()
        task.save()
        return task

    def list_queued_video_tasks(self) -> list[VideoTask]:
        return list(VideoTask.select().where(VideoTask.status == "queued").order_by(VideoTask.created_at))

    def has_active_tasks(self, account_id: str) -> bool:
        return (VideoTask.select().where(
            (VideoTask.account == account_id) & VideoTask.status.in_(("starting", "generating", "resolving"))
        ).exists())
