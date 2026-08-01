from __future__ import annotations

from datetime import UTC, datetime

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


def utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


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
    prompt = TextField()
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
    cover_url = TextField(null=True)
    error_message = TextField(null=True)
    created_at = DateTimeField(default=utcnow)
    updated_at = DateTimeField(default=utcnow)
    completed_at = DateTimeField(null=True)


class AppSetting(BaseModel):
    key = CharField(primary_key=True)
    value = TextField()
    updated_at = DateTimeField(default=utcnow)


ALL_MODELS = (Account, LoginAttempt, AppLog, VideoTask, AppSetting)
