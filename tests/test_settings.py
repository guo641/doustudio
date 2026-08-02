import sqlite3

import pytest

from doupool.settings.service import SettingsService


def test_settings_defaults_update_and_persist(repository, database_manager, tmp_path):
    service = SettingsService(repository, tmp_path, database_manager.path)

    assert service.get()["daily_quota"] == 5
    assert service.get()["default_model"] == "seedance_v2.0_mini"

    updated = service.update({"daily_quota": 8, "log_level": "DEBUG"})

    assert updated["daily_quota"] == 8
    assert SettingsService(repository, tmp_path, database_manager.path).get()["log_level"] == "DEBUG"


def test_settings_reject_invalid_values(repository, database_manager, tmp_path):
    service = SettingsService(repository, tmp_path, database_manager.path)

    with pytest.raises(ValueError, match="并发数"):
        service.update({"max_concurrency": 0})
    with pytest.raises(ValueError, match="重置时间"):
        service.update({"quota_reset_time": "25:00"})


def test_database_backup_is_consistent(repository, database_manager, tmp_path):
    service = SettingsService(repository, tmp_path, database_manager.path)

    backup = service.backup()

    assert backup.parent == tmp_path / "backups"
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
