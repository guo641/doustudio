from __future__ import annotations

import re
import sqlite3
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class SettingsService:
    DEFAULTS = {
        "max_concurrency": 1,
        # v0.2.9:按 seedance 模型拆 daily_quota。旧 daily_quota 保留做 legacy
        # fallback(老 DB 导出 / 还没升级设置的实例,get_daily_quotas() 会兜底)。
        # v0.2.19:默认值从 5 改成 50 — 豆包每天每账号 50 点,mini 1 点/秒 / fast 1.5 点/秒。
        # 老 DB 已经存了 5 的话 get_daily_quotas 仍会读老值,需要用户手动改一次。
        "daily_quota": 50,
        "daily_quota_mini": 50,
        "daily_quota_v2": 50,
        "daily_quota_std": 50,
        "quota_reset_time": "00:00",
        "scheduler_strategy": "least_used",
        "default_model": "seedance_v2.0_mini",
        "default_duration": 5,
        "default_ratio": "1:1",
        "download_dir": "",
        "log_level": "INFO",
        "log_retention_days": 30,
        # zhuceka 去水印
        "watermark_enabled": False,
        "watermark_uid": "",
        "watermark_key": "",
        # 失败自动改 prompt 重试
        "max_prompt_retries": 2,
        # v0.2.23 Q1:豆包内容审核拒绝后,自动改写 prompt 在同一 page 上重提交。
        # 用户真实反馈:「这种报错也是提示词的问题,让豆包自己修改后重新生成
        # 就可以,除非是额度不够」 —— 拒绝类基本都是 prompt 改一改能过的场景,
        # 之前 v0.2.22 默认 0 等于让用户每次手动开,体感差。改默认 2:1 次原 prompt
        # + 2 次改写 = 3 次总尝试,基本够「剥离违规关键词 + 安全模板兜底」。
        # quota 限流走 RATE_LIMITED 分支(见 prompt_reviser),revise_prompt=False,
        # 不浪费豆包次数。runner 内部仍 clamp 到 0..3,防止 setting 被改成 100 攻击。
        # 想完全关掉此行为的用户(老 v0.2.21 习惯)显式设 0 即可。
        "max_reject_retries": 2,
        # v0.2.22 Q2:视频生成时 Chromium 窗口是否显示在桌面。默认 False
        # 保持 v0.2.21 隐身行为(窗口放到屏幕外 -2000,-2000)。仅在
        # BrowserContext 首次创建时生效;cached context 复用前次位置,
        # 改 setting 后只对下次新建 context 生效。
        "runner_window_visible": False,
        # v0.2.17:浏览器 PC 端版本号,被 video/browser.py 读取塞到
        # payload.client_meta.pc_version。不暴露前端(17-b 时一起加 UI),
        # 升级时只需改这里 + 重启服务。
        "pc_version": "3.27.4",
    }

    # seedance 模型 → quota 桶名。供 repository / video_service 单一真值源使用。
    MODEL_QUOTA_BUCKETS = {
        "seedance_v2.0_mini": "mini",
        "seedance_v2.0": "v2",
        "seedance_v2.0_std": "std",
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

    def get_daily_quotas(self) -> dict[str, int]:
        """v0.2.9:返回 {'mini': int, 'v2': int, 'std': int}。

        三桶全空(老 DB 刚升上来) → 统一用 legacy daily_quota。
        任意一桶已写过 → 走新三桶,未写的桶用 legacy 兜底(用户碰哪个改哪个,
        不强求一次性设完三个)。
        """
        legacy = int(self.get().get("daily_quota", 5))
        mini = self.repository.get_setting("daily_quota_mini", None)
        v2 = self.repository.get_setting("daily_quota_v2", None)
        std = self.repository.get_setting("daily_quota_std", None)
        if mini is None and v2 is None and std is None:
            return {"mini": legacy, "v2": legacy, "std": legacy}
        return {
            "mini": int(mini) if mini is not None else legacy,
            "v2": int(v2) if v2 is not None else legacy,
            "std": int(std) if std is not None else legacy,
        }

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
        target = backup_dir / f"doupool-{datetime.now(_SHANGHAI):%Y%m%d-%H%M%S-%f}.sqlite3"
        with sqlite3.connect(self.database_path) as source, sqlite3.connect(target) as destination:
            source.backup(destination)
        return target

    @staticmethod
    def _validate(values: dict) -> None:
        if not 1 <= int(values["max_concurrency"]) <= 10:
            raise ValueError("并发数必须在 1 到 10 之间")
        # v0.2.9:按模型分别校验 daily_quota。旧 daily_quota 键留作 legacy
        # fallback 不再校验范围(老 DB 可能有越界值,迁完就让用户重新设)。
        for key in ("daily_quota_mini", "daily_quota_v2", "daily_quota_std"):
            if not 1 <= int(values[key]) <= 100:
                raise ValueError(f"{key} 必须在 1 到 100 之间")
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
