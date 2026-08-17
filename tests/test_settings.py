import sqlite3
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from doupool.settings.service import (
    SettingsService,
    _pick_dir_linux,
    _pick_dir_macos,
    _pick_dir_windows,
    open_directory,
    pick_directory,
)


def test_pick_directory_windows_uses_constant_script_and_returns_path():
    result = SimpleNamespace(returncode=0, stdout="C:\\Videos\\Selected\r\n")
    with patch("doupool.settings.service.subprocess.run", return_value=result) as run:
        with patch("doupool.settings.service.os.environ", {"PATH": "test"}):
            selected = _pick_dir_windows("C:\\Videos")
    assert selected == "C:\\Videos\\Selected"
    command = run.call_args.args[0]
    assert command[:3] == ["powershell", "-NoProfile", "-STA"]
    # The start path is supplied through the environment, never interpolated
    # into the PowerShell source.
    assert "C:\\Videos" not in command[-1]
    assert run.call_args.kwargs["env"]["DOUPOOL_PICK_START_DIR"] == "C:\\Videos"


def test_pick_directory_macos_passes_start_as_argv_and_cancel_is_none():
    result = SimpleNamespace(returncode=0, stdout="")
    with patch("doupool.settings.service.subprocess.run", return_value=result) as run:
        assert _pick_dir_macos("/tmp/start") is None
    command = run.call_args.args[0]
    assert command[0:2] == ["osascript", "-e"]
    assert command[-1] == "/tmp/start"


def test_pick_directory_linux_falls_back_to_kdialog():
    missing = FileNotFoundError()
    selected = SimpleNamespace(returncode=0, stdout="/home/user/Videos\n")
    with patch(
        "doupool.settings.service.subprocess.run",
        side_effect=[missing, selected],
    ) as run:
        assert _pick_dir_linux("/home/user") == "/home/user/Videos"
    assert run.call_args_list[0].args[0][0] == "zenity"
    assert run.call_args_list[1].args[0][0] == "kdialog"


def test_pick_directory_dispatches_by_platform():
    with patch("doupool.settings.service.platform.system", return_value="Windows"):
        with patch("doupool.settings.service._pick_dir_windows", return_value="C:\\Videos") as pick:
            assert pick_directory("C:\\Start") == "C:\\Videos"
            pick.assert_called_once_with("C:\\Start")
    with patch("doupool.settings.service.platform.system", return_value="Unknown"):
        assert pick_directory("/tmp") is None


def test_open_directory_rejects_nonexistent_path(tmp_path):
    with patch("doupool.settings.service.subprocess.run") as run:
        assert open_directory(str(tmp_path / "missing")) is False
    run.assert_not_called()


def test_settings_defaults_update_and_persist(repository, database_manager, tmp_path):
    service = SettingsService(repository, tmp_path, database_manager.path)

    # v0.2.29:共享池只暴露 daily_quota_shared 一个 key,默认 50。
    defaults = service.get()
    assert defaults["daily_quota_shared"] == 50
    assert defaults["max_concurrency"] == 1
    assert defaults["default_model"] == "seedance_v2.0_mini"
    assert defaults["default_duration"] == 5

    updated = service.update({"daily_quota_shared": 8, "log_level": "DEBUG"})

    assert updated["daily_quota_shared"] == 8
    assert SettingsService(repository, tmp_path, database_manager.path).get()["log_level"] == "DEBUG"


def test_legacy_ttshitu_rows_are_preserved_but_ignored(
    repository, database_manager, tmp_path,
):
    repository.set_setting("ttshitu_username", "legacy-user")
    repository.set_setting("ttshitu_password", "legacy-password")
    repository.set_setting("ttshitu_enabled", True)

    service = SettingsService(repository, tmp_path, database_manager.path)
    values = service.get()

    assert "ttshitu_username" not in values
    assert "ttshitu_password" not in values
    assert "ttshitu_enabled" not in values
    with pytest.raises(ValueError, match="ttshitu_enabled"):
        service.update({"ttshitu_enabled": False})


def test_settings_reject_invalid_values(repository, database_manager, tmp_path):
    service = SettingsService(repository, tmp_path, database_manager.path)

    # v0.2.29:并发上限从 10 提到 50(用户实测多机并发需求)。
    with pytest.raises(ValueError, match="并发数"):
        service.update({"max_concurrency": 0})
    with pytest.raises(ValueError, match="并发数"):
        service.update({"max_concurrency": 51})
    with pytest.raises(ValueError, match="重置时间"):
        service.update({"quota_reset_time": "25:00"})


def test_settings_accept_concurrency_upper_bound_50(repository, database_manager, tmp_path):
    """v0.2.29:用户要求放宽到 50,边界值 50 必须能保存。"""
    service = SettingsService(repository, tmp_path, database_manager.path)
    updated = service.update({"max_concurrency": 50})
    assert updated["max_concurrency"] == 50


def test_get_daily_quotas_returns_shared(repository, database_manager, tmp_path):
    """v0.2.29:共享池 → get_daily_quotas 返 {'shared': int}。"""
    service = SettingsService(repository, tmp_path, database_manager.path)

    # 全 default → shared=50
    assert service.get_daily_quotas() == {"shared": 50}

    # 用户改 → 1 个 key 1 个桶
    service.update({"daily_quota_shared": 70})
    assert service.get_daily_quotas() == {"shared": 70}


def test_get_daily_quotas_falls_back_to_legacy_daily_quota(repository, database_manager, tmp_path):
    """v0.2.29:老 DB 还没写 daily_quota_shared,fallback 用单 daily_quota。

    用户第一次改 daily_quota_shared 时,新键写入,从此走新字段。
    """
    service = SettingsService(repository, tmp_path, database_manager.path)
    # 模拟"老 DB":只写 daily_quota=8,新 shared 桶没落库
    repository.set_setting("daily_quota", 8)
    # 验证 fallback
    assert service.get_daily_quotas() == {"shared": 8}

    # 用户改 shared → 应写入新键,从此走新字段
    service.update({"daily_quota_shared": 99})
    assert service.get_daily_quotas() == {"shared": 99}


def test_settings_reject_shared_quota_out_of_range(repository, database_manager, tmp_path):
    """v0.2.29:daily_quota_shared 1-100(豆包官方阈值)。"""
    service = SettingsService(repository, tmp_path, database_manager.path)

    with pytest.raises(ValueError, match="每日额度"):
        service.update({"daily_quota_shared": 0})
    with pytest.raises(ValueError, match="每日额度"):
        service.update({"daily_quota_shared": 101})


def test_settings_accept_duration_4_to_10(repository, database_manager, tmp_path):
    """v0.2.29:豆包接受任意整数 4..10 秒时长(原 {5,10} 太严)。"""
    service = SettingsService(repository, tmp_path, database_manager.path)

    for d in (4, 5, 6, 7, 8, 9, 10):
        updated = service.update({"default_duration": d})
        assert updated["default_duration"] == d


def test_settings_reject_duration_out_of_range(repository, database_manager, tmp_path):
    """v0.2.29:durations <4 或 >10 拒绝。"""
    service = SettingsService(repository, tmp_path, database_manager.path)

    with pytest.raises(ValueError, match="默认时长"):
        service.update({"default_duration": 3})
    with pytest.raises(ValueError, match="默认时长"):
        service.update({"default_duration": 11})


def test_task_interval_seconds_default_zero(repository, database_manager, tmp_path):
    """v0.2.34:新加的并发任务间隔,默认 0(沿用 v0.2.33 不间隔行为)。"""
    service = SettingsService(repository, tmp_path, database_manager.path)
    assert service.get()["task_interval_seconds"] == 0


def test_settings_accept_task_interval_in_range(repository, database_manager, tmp_path):
    """v0.2.34:0..60 秒间隔,边界值必须能保存。"""
    service = SettingsService(repository, tmp_path, database_manager.path)
    for v in (0, 1, 5, 30, 60):
        updated = service.update({"task_interval_seconds": v})
        assert updated["task_interval_seconds"] == v


def test_settings_reject_task_interval_out_of_range(repository, database_manager, tmp_path):
    """v0.2.34:任务间隔<0 或 >60 拒绝 —— 浏览器禁用 save 按钮,后端再兜底。"""
    service = SettingsService(repository, tmp_path, database_manager.path)
    with pytest.raises(ValueError, match="任务间隔"):
        service.update({"task_interval_seconds": -1})
    with pytest.raises(ValueError, match="任务间隔"):
        service.update({"task_interval_seconds": 61})


def test_settings_reject_round_robin_strategy(repository, database_manager, tmp_path):
    """v0.2.29:调度策略只剩 least_used —— round_robin 是 v0.2.9 死链。

    DB 里若还残留 round_robin 老值,启动后会被 settings.get() 兜底成 least_used,
    但 update() 仍然会拒绝(防止用户主动写回 round_robin)。
    """
    service = SettingsService(repository, tmp_path, database_manager.path)

    with pytest.raises(ValueError, match="调度策略"):
        service.update({"scheduler_strategy": "round_robin"})


def test_database_backup_is_consistent(repository, database_manager, tmp_path):
    service = SettingsService(repository, tmp_path, database_manager.path)

    backup = service.backup()

    assert backup.parent == tmp_path / "backups"
    with sqlite3.connect(backup) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
