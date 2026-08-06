from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from peewee import (
    AutoField,
    BooleanField,
    CharField,
    DateField,
    DateTimeField,
    ForeignKeyField,
    IntegerField,
    Model,
    TextField,
)
from playhouse.db_url import DatabaseProxy


database_proxy = DatabaseProxy()

# v0.2.16:所有时间戳统一按"北京时间"存 — DB 里写啥用户就该看到啥,
# 跟 OS 时区解耦(用户机器时区如果配 UTC,这里也会得到 +8 的 Beijing 时间)。
SHANGHAI = ZoneInfo("Asia/Shanghai")


def utcnow() -> datetime:
    """v0.2.16:墙钟时间用 Asia/Shanghai 返回,名字保留 utcnow 兼容老调用点。

    历史原因:这个函数原本叫 utcnow 是真 UTC,但业务上一直当 "now" 用 —— quota
    桶 reset、登录 finished_at、账号 last_verified_at 这些对用户来说都该是
    "本地时间"。统一到 Asia/Shanghai 之后,DB 里的 DateTime 字段就是用户视角的
    北京时间,quota_window 内部本来也用 Asia/Shanghai 算 business_date,口径一致。
    """
    return datetime.now(SHANGHAI).replace(tzinfo=None)


class BaseModel(Model):
    class Meta:
        database = database_proxy


class Account(BaseModel):
    id = CharField(primary_key=True)
    display_name = CharField()
    doubao_user_id = CharField(unique=True, null=True)
    doubao_nickname = CharField(null=True)
    profile_dir = CharField()
    status = CharField(default="active")
    enabled = BooleanField(default=True)
    last_verified_at = DateTimeField(null=True)
    last_error = TextField(null=True)
    video_quota_used = IntegerField(default=0)
    video_quota_date = DateField(null=True)
    video_limited_until = DateTimeField(null=True)
    # v0.2.9:按 seedance 模型分桶计费(mini / v2 / std 各算各的当日额度)。
    # 老 video_quota_used 列保留(只读,不再写入),留给老 DB 导出兼容。
    video_quota_used_mini = IntegerField(default=0)
    video_quota_used_v2 = IntegerField(default=0)
    video_quota_used_std = IntegerField(default=0)
    # v0.2.29:豆包官方按账号每日总配额,不区分模型 → 共享池。
    # 旧 mini/v2/std 三列保留只读(历史可查),扣退/选择/限流全部走 shared。
    video_quota_used_shared = IntegerField(default=0)
    created_at = DateTimeField(default=utcnow)
    updated_at = DateTimeField(default=utcnow)


class LoginAttempt(BaseModel):
    id = CharField(primary_key=True)
    account = ForeignKeyField(Account, null=True, backref="login_attempts")
    state = CharField(default="created")
    error_code = CharField(null=True)
    error_message = TextField(null=True)
    started_at = DateTimeField(default=utcnow)
    finished_at = DateTimeField(null=True)


class AppLog(BaseModel):
    id = AutoField()
    level = CharField()
    module = CharField()
    event = CharField()
    message = TextField()
    account = ForeignKeyField(Account, null=True, backref="logs")
    login_attempt = ForeignKeyField(LoginAttempt, null=True, backref="logs")
    created_at = DateTimeField(default=utcnow)


class VideoTask(BaseModel):
    id = CharField(primary_key=True)
    account = ForeignKeyField(Account, null=True, backref="video_tasks")
    group_id = CharField(null=True, index=True)
    group_index = IntegerField(default=0)  # 组内顺序(从 1 开始)
    prompt = TextField()
    original_prompt = TextField(null=True)
    prompt_retry_count = IntegerField(default=0)
    model = CharField()
    ratio = CharField()
    duration = IntegerField()
    mode = CharField(default="t2v")
    image_paths = TextField(null=True)
    status = CharField(default="queued")
    conversation_id = CharField(null=True)
    section_id = CharField(null=True)
    question_id = CharField(null=True)
    remote_task_id = CharField(null=True)
    vid = CharField(null=True)
    result_url = TextField(null=True)
    backup_result_url = TextField(null=True)
    fallback_result_url = TextField(null=True)
    clean_video_url = TextField(null=True)
    clean_error = TextField(null=True)
    cover_url = TextField(null=True)
    error_message = TextField(null=True)
    created_at = DateTimeField(default=utcnow)
    updated_at = DateTimeField(default=utcnow)
    completed_at = DateTimeField(null=True)
    # v0.2.9:callbackUrl 异步回执状态。callback_url 在提交时一次性写入,
    # callback_status / callback_attempts / callback_last_error 由 service
    # 在任务 terminal 后异步维护,前端只在排查时查询(暂未暴露给前端)。
    callback_url = TextField(null=True)
    callback_status = CharField(null=True)  # pending / sending / succeeded / failed / disabled
    callback_attempts = IntegerField(default=0)
    callback_last_error = TextField(null=True)


class AppSetting(BaseModel):
    key = CharField(primary_key=True)
    value = TextField()
    updated_at = DateTimeField(default=utcnow)


ALL_MODELS = (Account, LoginAttempt, AppLog, VideoTask, AppSetting)
