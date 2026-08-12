"""tests/test_captcha_config.py

凭证合并优先级:env > SQLite;enabled 默认 False,除非 env 有完整凭证。
"""
from __future__ import annotations

import pytest

from doupool.captcha.config import CaptchaCredentials, load_credentials, credentials_present


class FakeSettings:
    """最小化 SettingsService 替身,避免拉 DB。"""

    def __init__(self, mapping: dict | None) -> None:
        self._m = mapping or {}

    def get(self) -> dict:
        return dict(self._m)


def test_env_overrides_sqlite(monkeypatch):
    monkeypatch.setenv("DOUSTUDIO_TTSHITU_USERNAME", "env_user")
    monkeypatch.setenv("DOUSTUDIO_TTSHITU_PASSWORD", "env_pass")
    s = FakeSettings({"ttshitu_username": "db_user", "ttshitu_password": "db_pass", "ttshitu_enabled": "0"})
    creds = load_credentials(settings=s)
    assert creds.username == "env_user"
    assert creds.password == "env_pass"
    assert creds.enabled is True  # env 配了凭证 → 默认开


def test_sqlite_used_when_no_env(monkeypatch):
    monkeypatch.delenv("DOUSTUDIO_TTSHITU_USERNAME", raising=False)
    monkeypatch.delenv("DOUSTUDIO_TTSHITU_PASSWORD", raising=False)
    s = FakeSettings({"ttshitu_username": "db_user", "ttshitu_password": "db_pass", "ttshitu_enabled": "1"})
    creds = load_credentials(settings=s)
    assert creds.username == "db_user"
    assert creds.enabled is True


def test_disabled_default_when_no_env_no_sqlite_enabled(monkeypatch):
    monkeypatch.delenv("DOUSTUDIO_TTSHITU_USERNAME", raising=False)
    monkeypatch.delenv("DOUSTUDIO_TTSHITU_PASSWORD", raising=False)
    s = FakeSettings({"ttshitu_username": "u", "ttshitu_password": "p"})  # 没设 enabled
    creds = load_credentials(settings=s)
    # 默认 enabled=False,防止「忘了开开关 → 白花钱」
    assert creds.enabled is False


def test_no_credentials_at_all(monkeypatch):
    monkeypatch.delenv("DOUSTUDIO_TTSHITU_USERNAME", raising=False)
    monkeypatch.delenv("DOUSTUDIO_TTSHITU_PASSWORD", raising=False)
    creds = load_credentials(settings=None)
    assert creds.username == ""
    assert creds.password == ""
    assert creds.enabled is False
    assert not creds.usable


def test_usable_property():
    assert CaptchaCredentials("u", "p", enabled=True).usable is True
    assert CaptchaCredentials("u", "p", enabled=False).usable is False
    assert CaptchaCredentials("", "p", enabled=True).usable is False
    assert CaptchaCredentials("u", "", enabled=True).usable is False


def test_credentials_present_helper():
    assert credentials_present(CaptchaCredentials("u", "p", enabled=False)) is True
    assert credentials_present(CaptchaCredentials("", "p")) is False


def test_enabled_coercion_variants(monkeypatch):
    monkeypatch.delenv("DOUSTUDIO_TTSHITU_USERNAME", raising=False)
    monkeypatch.delenv("DOUSTUDIO_TTSHITU_PASSWORD", raising=False)
    for raw, expected in [
        ("1", True),
        ("true", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("", False),
        (None, False),
    ]:
        s = FakeSettings({"ttshitu_username": "u", "ttshitu_password": "p", "ttshitu_enabled": raw})
        creds = load_credentials(settings=s)
        assert creds.enabled is expected, f"raw={raw!r} expected={expected} got={creds.enabled}"


def test_settings_get_raises_is_swallowed(monkeypatch):
    """SettingsService 抛错时不应让 captcha 也挂 —— 退化为空凭证。"""
    from doupool.captcha import config as cfg

    class BrokenSettings:
        def get(self):
            raise RuntimeError("db locked")

    monkeypatch.setattr(cfg, "SettingsService", BrokenSettings)
    # 直接构造一个会触发的场景:env 没设 + settings 抛错 → 应该拿到空 creds
    creds = cfg.load_credentials(settings=BrokenSettings())
    assert creds.username == ""
    assert creds.password == ""
    assert creds.enabled is False