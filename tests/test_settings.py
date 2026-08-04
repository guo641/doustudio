import sqlite3

import pytest

from doupool.settings.service import SettingsService


def test_settings_defaults_update_and_persist(repository, database_manager, tmp_path):
    service = SettingsService(repository, tmp_path, database_manager.path)

    # v0.2.19:三桶默认从 5 改成 50 — 豆包每天每账号 50 点
    defaults = service.get()
    assert defaults["daily_quota"] == 50
    assert defaults["daily_quota_mini"] == 50
    assert defaults["daily_quota_v2"] == 50
    assert defaults["daily_quota_std"] == 50
    assert defaults["default_model"] == "seedance_v2.0_mini"

    updated = service.update({"daily_quota_mini": 8, "log_level": "DEBUG"})

    assert updated["daily_quota_mini"] == 8
    assert SettingsService(repository, tmp_path, database_manager.path).get()["log_level"] == "DEBUG"


def test_settings_reject_invalid_values(repository, database_manager, tmp_path):
    service = SettingsService(repository, tmp_path, database_manager.path)

    with pytest.raises(ValueError, match="并发数"):
        service.update({"max_concurrency": 0})
    with pytest.raises(ValueError, match="重置时间"):
        service.update({"quota_reset_time": "25:00"})


def test_get_daily_quotas_returns_model_buckets(repository, database_manager, tmp_path):
    """v0.2.9:SettingsService 暴露三桶给上层(API / 调度)用。v0.2.19:默认 50。"""
    service = SettingsService(repository, tmp_path, database_manager.path)

    # 全 default → 三个 50
    assert service.get_daily_quotas() == {"mini": 50, "v2": 50, "std": 50}

    # 单独改 mini,v2/std 不动
    service.update({"daily_quota_mini": 70})
    assert service.get_daily_quotas() == {"mini": 70, "v2": 50, "std": 50}

    # 三个独立
    service.update({"daily_quota_v2": 40, "daily_quota_std": 60})
    assert service.get_daily_quotas() == {"mini": 70, "v2": 40, "std": 60}


def test_get_daily_quotas_falls_back_to_legacy_daily_quota(repository, database_manager, tmp_path):
    """v0.2.9:老 DB 还没升过三桶,fallback 用单 daily_quota 让三桶一致。
    一旦用户在前端任意改一个桶,自动写入新键,从此摆脱 fallback。"""
    service = SettingsService(repository, tmp_path, database_manager.path)
    # 模拟"老 DB":只写 daily_quota=8,新三桶都没落库
    repository.set_setting("daily_quota", 8)
    # 验证 fallback
    assert service.get_daily_quotas() == {"mini": 8, "v2": 8, "std": 8}

    # 用户改任一桶 → 应写入新键,从此走新三桶
    service.update({"daily_quota_std": 99})
    assert service.get_daily_quotas() == {"mini": 8, "v2": 8, "std": 99}


def test_settings_reject_per_model_quota_out_of_range(repository, database_manager, tmp_path):
    """v0.2.9:daily_quota_mini/v2/std 都 1-100。"""
    service = SettingsService(repository, tmp_path, database_manager.path)

    for key in ("daily_quota_mini", "daily_quota_v2", "daily_quota_std"):
        with pytest.raises(ValueError, match=key):
            service.update({key: 0})
        with pytest.raises(ValueError, match=key):
            service.update({key: 101})


def test_database_backup_is_consistent(repository, database_manager, tmp_path):
    service = SettingsService(repository, tmp_path, database_manager.path)

    backup = service.backup()

    assert backup.parent == tmp_path / "backups"
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
