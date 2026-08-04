from __future__ import annotations

from collections.abc import Mapping
import json
from datetime import date, datetime, timedelta
from uuid import uuid4

from peewee import JOIN, SqliteDatabase

from .models import Account, AppLog, AppSetting, LoginAttempt, VideoTask, utcnow


# v0.2.9:seedance 模型 → quota 列名后缀。Account 列名 video_quota_used_<suffix>
# 必须和这里一致。校验非法 model 时 raise ValueError。
MODEL_QUOTA_FIELD: dict[str, str] = {
    "seedance_v2.0_mini": "video_quota_used_mini",
    "seedance_v2.0": "video_quota_used_v2",
    "seedance_v2.0_std": "video_quota_used_std",
}


def _quota_field(model: str) -> str:
    """模型名 → Account quota 列名。非法 model 抛 ValueError。"""
    try:
        return MODEL_QUOTA_FIELD[model]
    except KeyError as exc:
        raise ValueError(f"unsupported model for quota: {model}") from exc


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
                # v0.2.14:重新登录成功时重置 quota 桶。
                # v0.2.12 时代的账号被 mark_account_limited cap 到 quota_limit,
                # 即便 v0.2.13 的 reset_daily_quotas 修了跨天+同天到期两段清桶,
                # 用户已经死锁的桶也只会等下次 423 / 跨天才解 —— 而登录是最稳的
                # 恢复点,反正账号刚扫完码就能用。把 3 个桶清零、清 limited_until,
                # 让重新登录过的账号立刻可调度。
                account.video_quota_used_mini = 0
                account.video_quota_used_v2 = 0
                account.video_quota_used_std = 0
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
        """v0.2.9:按 model 桶过滤 quota。返回该桶还有额度的最早账号。

        daily_quotas: SettingsService.get_daily_quotas() 返回的 {'mini': int, ...}
        model: 任务模型(seedance_v2.0_mini / seedance_v2.0 / seedance_v2.0_std)
        """
        field_name = _quota_field(model)
        bucket = field_name.removeprefix("video_quota_used_")
        quota_limit = int(daily_quotas[bucket])
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
        """v0.2.12:给前端清晰的等待原因 —— 「没有账号」vs 「全部用完」。

        不直接返回 Account,只统计数字。choose_available_account 返 None
        时,前端拿这两个数字决定提示文案。
        """
        field_name = _quota_field(model)
        bucket = field_name.removeprefix("video_quota_used_")
        quota_limit = int(daily_quotas[bucket])
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
        """三桶一起 reset(日切不区分模型,豆包对所有模型额度统一重置)。

        v0.2.12 顺带清 limited_until 已过期但桶被 cap 死的账号:
        `mark_account_limited` 会把三桶 cap 到 quota_limit,直到 `reset_daily_quotas`
        在跨天时才清。如果 limited_until 落在当天内(豆包 423 短时封号),
        旧实现会让账号永久不可选 —— 因为 `< quota_limit` 永远是 False。
        现在即使还没跨天,只要 limited_until <= now,就把桶清回 0 + 撤掉
        limited_until,让账号恢复可用。
        """
        now = now or utcnow()
        (Account.update(
            video_quota_used_mini=0,
            video_quota_used_v2=0,
            video_quota_used_std=0,
            video_quota_date=business_date,
            video_limited_until=None,
         )
         .where((Account.video_quota_date.is_null(True)) | (Account.video_quota_date != business_date))
         .execute())
        # v0.2.12:同一天内 limited_until 已到期 → 桶已 cap 死,清桶让账号恢复。
        # v0.2.13:去掉 date == business_date 限制 —— 任何 limited_until <= now
        # 都清桶,跟第一段(日切)互不依赖更安全。mark_account_limited 现在会
        # 同步写 video_quota_date,跨天时第一段也会命中,两段协同不会重复写。
        (Account.update(
            video_quota_used_mini=0,
            video_quota_used_v2=0,
            video_quota_used_std=0,
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
        # v0.2.9:豆包 423 限流封整号(不区分模型),三桶一并 cap 到各自 quota,
        # 配合 choose_available_account 的 < 比较,该账号任何模型都不可再选。
        # v0.2.13:同步把 video_quota_date 写成业务日,确保 reset_daily_quotas
        # 跨天时第一段(日期不匹配)一定能命中,把桶清回 0。否则如果调用方传
        # 的 limited_until 是「当天未来某点」(quota_window 在 reset_time > now
        # 时的产物),且用户在设置面板改了 quota_reset_time,新 next_reset 已经
        # 跳到次日,但旧的 limited_until 还指向当天未来时间,跨天/同日两段都
        # 不会清桶,账号就 cap 死选不到了。
        update_kwargs: dict = dict(
            video_quota_used_mini=daily_quotas["mini"],
            video_quota_used_v2=daily_quotas["v2"],
            video_quota_used_std=daily_quotas["std"],
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
        """v0.2.9:按 model 桶扣。
        v0.2.11:加 by 参数,默认 1 保持向后兼容;非法 by(<1)抛 ValueError。
        非法 model 也抛 ValueError(避免悄悄扣错桶)。"""
        if by < 1:
            raise ValueError(f"increment by must be >= 1, got {by}")
        field_name = _quota_field(model)
        field = getattr(Account, field_name)
        (Account.update(**{field_name: field + by}, updated_at=utcnow())
         .where(Account.id == account_id).execute())

    def decrement_account_quota(
        self, account_id: str, model: str, *, by: int = 1
    ) -> None:
        """v0.2.19:失败退还额度 —— 配合 service 里的网络/prompt 违规退款路径。

        与 increment_account_quota 对称:非法 by(<1)抛 ValueError;非法 model
        也抛 ValueError;桶下界 0(`max(0, used - by)`),避免跨天 reset 后
        退款把 used 打成负数。

        注:这里的「退款」只覆盖 service 主动判定为失败的情况 —— 豆包真实
        是否计费只能等用户第二天看官方账户为准。我们这边只是把桶里
        多记的 quota 减回来,让用户感觉「提了被拒的任务不会扣我额度」。
        """
        if by < 1:
            raise ValueError(f"decrement by must be >= 1, got {by}")
        field_name = _quota_field(model)
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
