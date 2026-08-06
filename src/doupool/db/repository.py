from __future__ import annotations

from collections.abc import Mapping
import json
from datetime import date, datetime, timedelta
from uuid import uuid4

from peewee import JOIN, SqliteDatabase

from .models import Account, AppLog, AppSetting, LoginAttempt, VideoTask, utcnow


# v0.2.29:豆包官方按账号每日总配额,不区分模型 → 共享池。
# 扣退/选择/限流全部走这一个字段,旧 mini/v2/std 三列保留只读(历史可查)。
SHARED_QUOTA_FIELD = "video_quota_used_shared"


def _shared_quota_field() -> str:
    """v0.2.29:共享池下所有 model 路由到同一列,签名里 `model` 参数仅作日志可观测。"""
    return SHARED_QUOTA_FIELD


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
                # v0.2.29:共享池下重置只清 shared 桶 + limited_until。
                account.video_quota_used_shared = 0
                account.video_limited_until = None
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
        self,
        daily_quotas: dict[str, int],
        model: str,
        now: datetime | None = None,
        strategy: str = "least_used",
    ) -> Account | None:
        """v0.2.29:共享额度池 —— 所有 model 走同一 quota 列,bucket 选 'shared'。

        签名保留 `model` / `strategy` 是为了上层日志/可观测性,不影响扣哪个桶。
        """
        field_name = _shared_quota_field()
        quota_limit = int(daily_quotas["shared"])
        field = getattr(Account, field_name)
        now = now or utcnow()
        query = (
            Account.select()
            .where(
                (Account.enabled == True)  # noqa: E712
                & (Account.status == "active")
                & (field < quota_limit)
                & ((Account.video_limited_until.is_null(True)) | (Account.video_limited_until <= now))
            )
        )
        if strategy == "round_robin":
            query = query.order_by(Account.updated_at.asc())
        else:
            query = query.order_by(field.asc(), Account.updated_at.asc())
        return query.first()

    def summarize_account_availability(
        self,
        daily_quotas: dict[str, int],
        model: str,
        now: datetime | None = None,
    ) -> dict[str, int]:
        """v0.2.29:共享池下 bucket='shared',统计 enabled_total / bucket_full。"""
        field_name = _shared_quota_field()
        quota_limit = int(daily_quotas["shared"])
        field = getattr(Account, field_name)
        now = now or utcnow()
        enabled_total = (
            Account.select()
            .where((Account.enabled == True) & (Account.status == "active"))  # noqa: E712
            .count()
        )
        bucket_full = (
            Account.select()
            .where(
                (Account.enabled == True)  # noqa: E712
                & (Account.status == "active")
                & (
                    (field >= quota_limit)
                    | ((Account.video_limited_until.is_null(False))
                       & (Account.video_limited_until > now))
                )
            )
            .count()
        )
        return {"enabled_total": enabled_total, "bucket_full": bucket_full}

    def reset_daily_quotas(self, business_date: date, now: datetime | None = None) -> None:
        """v0.2.29:共享池 reset —— 只清 shared 桶;旧 mini/v2/std 不再写入。

        日切条件保留(业务日变化时清零 + 写 date);同时清 limited_until
        已过期但桶被 cap 死的账号。
        """
        now = now or utcnow()
        (Account.update(
            video_quota_used_shared=0,
            video_quota_date=business_date,
            video_limited_until=None,
         )
         .where((Account.video_quota_date.is_null(True)) | (Account.video_quota_date != business_date))
         .execute())
        (Account.update(
            video_quota_used_shared=0,
            video_limited_until=None,
            updated_at=now,
         )
         .where(
             (Account.video_limited_until.is_null(False))
             & (Account.video_limited_until <= now)
         )
         .execute())

    def mark_account_limited(
        self,
        account_id: str,
        limited_until: datetime,
        daily_quotas: dict[str, int],
        business_date: date | None = None,
    ) -> None:
        # v0.2.29:共享池下只 cap shared 一桶;配合 choose_available_account 的
        # < 比较,该账号任何 model 都不可再选。
        update_kwargs: dict = dict(
            video_quota_used_shared=daily_quotas["shared"],
            video_limited_until=limited_until,
            updated_at=utcnow(),
        )
        if business_date is not None:
            update_kwargs["video_quota_date"] = business_date
        (Account.update(**update_kwargs)
         .where(Account.id == account_id).execute())

    def increment_account_quota(
        self, account_id: str, model: str, *, by: int = 1
    ) -> None:
        """v0.2.29:共享池扣 —— 所有 model 扣同一个 shared 桶。

        `model` 参数保留仅为日志可观测,不影响扣哪个桶;非法 by(<1)仍抛 ValueError。
        """
        if by < 1:
            raise ValueError(f"increment by must be >= 1, got {by}")
        field_name = _shared_quota_field()
        field = getattr(Account, field_name)
        (Account.update(**{field_name: field + by}, updated_at=utcnow())
         .where(Account.id == account_id).execute())

    def decrement_account_quota(
        self, account_id: str, model: str, *, by: int = 1
    ) -> None:
        """v0.2.29:共享池退 —— 同 increment,但走 max(0, used - by)。

        桶下界 0,避免跨天 reset 后退款把 used 打成负数。
        """
        if by < 1:
            raise ValueError(f"decrement by must be >= 1, got {by}")
        field_name = _shared_quota_field()
        field = getattr(Account, field_name)
        # 用 SQL 表达式 max(field - by, 0):peewee 没有直接的 GREATEST,
        # 但 SQLite/MySQL/Postgres 都支持 GREATEST。用 Case 实现兼容性更好。
        from peewee import Case, SQL
        new_value = Case(
            None,
            [(field - by < 0, SQL("0"))],
            field - by,
        )
        (Account.update(**{field_name: new_value}, updated_at=utcnow())
         .where(Account.id == account_id).execute())

    # ---------- v0.2.29:共享池迁移 + 手动重置 ----------

    def migrate_legacy_quota_buckets(self, now: datetime | None = None) -> int:
        """把历史 mini+v2+std 一次性累加进 shared 桶。

        触发条件:`video_quota_used_shared == 0 且 sum(mini+v2+std) > 0`。
        已迁过的账号(shared>0 或 sum==0)不动,多次执行安全。
        返回迁移过的账号数。
        """
        now = now or utcnow()
        migrated = 0
        with self.database.atomic():
            for account in Account.select():
                if account.video_quota_used_shared > 0:
                    continue
                legacy_total = (
                    (account.video_quota_used_mini or 0)
                    + (account.video_quota_used_v2 or 0)
                    + (account.video_quota_used_std or 0)
                )
                if legacy_total <= 0:
                    continue
                Account.update(
                    video_quota_used_shared=legacy_total,
                    updated_at=now,
                ).where(Account.id == account.id).execute()
                migrated += 1
        return migrated

    def reset_account_quota(self, account_id: str, business_date: date, now: datetime | None = None) -> bool:
        """v0.2.29:清单个账号的 shared 桶 + 清 limited_until。

        账号不存在返回 False,清成功返回 True。幂等。
        """
        now = now or utcnow()
        query = Account.update(
            video_quota_used_shared=0,
            video_quota_date=business_date,
            video_limited_until=None,
            updated_at=now,
        ).where(Account.id == account_id)
        updated = query.execute()
        return updated > 0

    def reset_all_quotas(self, business_date: date, now: datetime | None = None) -> int:
        """v0.2.29:一键清所有 enabled 账号的 shared 桶 + limited_until。

        返回被清的账号数。disabled 账号不动(用户显式关掉的不要自动清)。
        """
        now = now or utcnow()
        return Account.update(
            video_quota_used_shared=0,
            video_quota_date=business_date,
            video_limited_until=None,
            updated_at=now,
        ).where(Account.enabled == True).execute()  # noqa: E712

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

    def assign_video_task(self, task_id: str, account_id: str | None) -> VideoTask | None:
        # v0.2.15:DELETE 端点删了任务后,worker 还在 in-flight 时调用这里会
        # 抛 VideoTaskDoesNotExist(peewee 包成 IndexError: list index out of range),
        # 触发 _run_inner 顶层兜底的 ERROR「视频任务执行器出现未捕获异常」。
        # 改成 silent skip:任务没了就不更新,worker 下一轮 get_video_task 会
        # 自己退出。
        try:
            task = VideoTask.get_by_id(task_id)
        except VideoTask.DoesNotExist:
            return None
        task.account = account_id
        task.updated_at = utcnow()
        task.save()
        if account_id:
            Account.update(updated_at=utcnow()).where(Account.id == account_id).execute()
        return task

    def get_video_task(self, task_id: str) -> VideoTask | None:
        # v0.2.15:任务被 DELETE 端点删了 → 返回 None(原来是抛 DoesNotExist)。
        # service._run_inner 收到 None 直接 return,worker 静默退出。
        try:
            return VideoTask.get_by_id(task_id)
        except VideoTask.DoesNotExist:
            return None

    def list_video_tasks(self, limit: int = 200) -> list[VideoTask]:
        return list(
            VideoTask.select(VideoTask, Account)
            .join(Account, JOIN.LEFT_OUTER)
            .order_by(VideoTask.created_at.desc())
            .limit(limit)
        )

    def update_video_task(self, task_id: str, **values) -> VideoTask | None:
        # v0.2.15:同 assign_video_task —— 任务被删时 silent skip。
        try:
            task = VideoTask.get_by_id(task_id)
        except VideoTask.DoesNotExist:
            return None
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

    # ---------- v0.2.11:任务删除 ----------

    _RUNNING_STATUSES: tuple[str, ...] = ("starting", "generating", "resolving")

    def delete_video_task(self, task_id: str) -> None:
        """v0.2.11:物理删除一条 VideoTask。

        running 状态由调用方挡掉(API 层 → service.delete → ValueError / 409)。
        这里只做 delete_instance,不要预检 status,避免事务竞争。
        """
        VideoTask.delete().where(VideoTask.id == task_id).execute()

    def is_task_deletable(self, task_id: str) -> bool:
        """v0.2.11:running 状态不可删(防正在生成的任务被打断)。
        任务不存在也算不可删(False 让上层走 404 分支)。
        """
        row = VideoTask.select(VideoTask.status).where(
            VideoTask.id == task_id
        ).first()
        if row is None:
            return False
        return row.status not in self._RUNNING_STATUSES
