from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path


class SettingsService:
    DEFAULTS = {
        "max_concurrency": 1,
        "daily_quota": 5,
        "quota_reset_time": "00:00",
        "scheduler_strategy": "least_used",
        "default_model": "seedance_v2.0_mini",
        "default_duration": 5,
        "default_ratio": "1:1",
        "download_dir": "",
        "log_level": "INFO",
        "log_retention_days": 30,
    }

    def __init__(self, repository, data_dir: Path, database_path: Path):
        self.repository = repository
        self.data_dir = Path(data_dir)
        self.database_path = Path(database_path)

    def get(self) -> dict:
        values = dict(self.DEFAULTS)
        values["download_dir"] = str(self.data_dir / "downloads")
        for key in self.DEFAULTS:
            stored = self.repository.get_setting(key, None)
            if stored is not None:
                values[key] = stored
        values["data_dir"] = str(self.data_dir)
        return values

    def update(self, changes: dict) -> dict:
        current = self.get()
        unknown = set(changes) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(f"不支持的设置：{', '.join(sorted(unknown))}")
        candidate = {**current, **changes}
        self._validate(candidate)
        for key, value in changes.items():
            self.repository.set_setting(key, value)
        Path(candidate["download_dir"]).expanduser().mkdir(parents=True, exist_ok=True)
        return self.get()

    def backup(self) -> Path:
        backup_dir = self.data_dir / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        target = backup_dir / f"doupool-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite3"
        with sqlite3.connect(self.database_path) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        return target

    @staticmethod
    def _validate(values: dict) -> None:
        if not 1 <= int(values["max_concurrency"]) <= 10:
            raise ValueError("并发数必须在 1 到 10 之间")
        if not 1 <= int(values["daily_quota"]) <= 100:
            raise ValueError("每日额度必须在 1 到 100 之间")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(values["quota_reset_time"])):
            raise ValueError("额度重置时间格式无效")
        if values["scheduler_strategy"] not in {"least_used", "round_robin"}:
            raise ValueError("调度策略无效")
        if values["default_model"] not in {"seedance_v2.0_std", "seedance_v2.0", "seedance_v2.0_mini"}:
            raise ValueError("默认模型无效")
        if int(values["default_duration"]) not in {5, 10}:
            raise ValueError("默认时长无效")
        if values["default_ratio"] not in {"1:1", "3:4", "4:3", "9:16", "16:9", "21:9"}:
            raise ValueError("默认比例无效")
        if values["log_level"] not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("日志级别无效")
        if not 1 <= int(values["log_retention_days"]) <= 365:
            raise ValueError("日志保留天数必须在 1 到 365 之间")
        if not Path(values["download_dir"]).expanduser().is_absolute():
            raise ValueError("下载目录必须是绝对路径")
