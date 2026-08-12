"""
v0.3.0:打码平台凭证来源解析。

优先级(前者覆盖后者):
  1. 环境变量 DOUSTUDIO_TTSHITU_USERNAME / _PASSWORD —— 给 CI / 临时调试用
  2. SettingsService 里的 ttshitu_username / ttshitu_password(用户在前端配置)

为什么不直接读文件而不是 settings:
  - 跟其他敏感设置(以后可能有更多)走同一通道,UI 加个表单就完事
  - 备份 = 备份 SQLite,不要单独管文件
  - 升级 / 迁移简单

为什么有 env var 覆盖:
  - 测试用例不想污染 SQLite
  - CI 跑同一份代码,不让打码 API 误打到真账号
  - 用户不想点 UI 也可直接 set env 验证
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from doupool.settings.service import SettingsService


@dataclass(frozen=True, slots=True)
class CaptchaCredentials:
    """图鉴打码平台登录凭证 + 启用开关。

    enabled = False 时 captcha solver 直接抛 AegisCaptchaDisabled,不调用 API,
    让视频任务继续走(失败的 CAPTCHA 弹窗会让任务 fail,但不会让 runner 卡住)。
    """

    username: str
    password: str
    enabled: bool = True

    @property
    def usable(self) -> bool:
        return self.enabled and bool(self.username) and bool(self.password)


def load_credentials(settings: SettingsService | None = None) -> CaptchaCredentials:
    """合并 env + SQLite settings,返回最终凭证。

    env 覆盖 SQLite;SQLite 缺失时返回空凭证。enabled 默认 False(SQLite 没设的话),
    防止「用户装了图鉴账号但忘了开 enabled 开关 → 每次都白花钱」。

    测试场景:env 设 DOUSTUDIO_TTSHITU_USERNAME + _PASSWORD 后,usable = True 即生效,
    SQLite 是否配不影响。
    """
    env_user = os.environ.get("DOUSTUDIO_TTSHITU_USERNAME", "").strip()
    env_pass = os.environ.get("DOUSTUDIO_TTSHITU_PASSWORD", "").strip()

    db_user = ""
    db_pass = ""
    db_enabled_raw: str | None = None
    if settings is not None:
        try:
            all_settings = settings.get()
        except Exception:
            all_settings = {}
        db_user = str(all_settings.get("ttshitu_username", "") or "").strip()
        db_pass = str(all_settings.get("ttshitu_password", "") or "").strip()
        db_enabled_raw = all_settings.get("ttshitu_enabled")

    username = env_user or db_user
    password = env_pass or db_pass
    # env 配置了凭证 → 默认 enabled=True(测试友好)
    # 否则读 SQLite 的 enabled 开关(用户配 SQLite 时显式开)
    if env_user and env_pass:
        enabled = True
    else:
        enabled = _coerce_bool(db_enabled_raw, default=False)

    return CaptchaCredentials(username=username, password=password, enabled=enabled)


def credentials_present(creds: CaptchaCredentials) -> bool:
    """是否有完整凭证(不管 enabled)。用于「提示用户配置」文案。"""
    return bool(creds.username) and bool(creds.password)


def _coerce_bool(value: object, default: bool = False) -> bool:
    """SQLite 取出来的可能是 '1' / 'true' / 1 / True / None。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return default