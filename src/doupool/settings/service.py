from __future__ import annotations

import os
import platform
import re
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from doupool.video.protocol import FIXED_VIDEO_DURATION_SECONDS

_SHANGHAI = ZoneInfo("Asia/Shanghai")


class DownloadDirPickerUnavailable(RuntimeError):
    """桌面窗口未就绪,无法弹目录选择器。"""


def open_directory(path: str) -> bool:
    """Open an existing directory in the platform file manager."""
    directory = Path(str(path or "")).expanduser()
    if not directory.is_dir():
        return False
    system = platform.system()
    try:
        if system == "Windows":
            startfile = getattr(os, "startfile", None)
            if startfile is None:  # defensive for non-Windows test hosts
                return False
            startfile(str(directory))
            return True
        command = ["open", str(directory)] if system == "Darwin" else ["xdg-open", str(directory)]
        proc = subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
        return proc.returncode == 0
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired):
        return False


class SettingsService:
    DEFAULTS = {
        "max_concurrency": 1,
        # v0.2.29:豆包官方按账号每日总配额(共享池),不再按 model 拆桶。
        # 旧 daily_quota / daily_quota_mini/v2/std 保留只读,get_daily_quotas()
        # 仍会兜底,新装实例或重置后只用一个 daily_quota_shared。
        "daily_quota_shared": 50,
        "quota_reset_time": "00:00",
        "scheduler_strategy": "least_used",
        "default_model": "seedance_v2.0_mini",
        "default_duration": FIXED_VIDEO_DURATION_SECONDS,
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
        # v0.2.24 Q2:视频生成时 Chromium 窗口是否显示在桌面。v0.2.22 默认
        # False 是为了「隐身行为」(窗口放到屏幕外 -2000,-2000),但用户反馈
        # 看不到界面不知道是不是在工作。改为默认 True(窗口落在 80,80),
        # 仍可在设置里手动关。仅在 BrowserContext 首次创建时生效;cached
        # context 复用前次位置,改 setting 后只对下次新建 context 生效。
        "runner_window_visible": True,
        # v0.2.27:每个任务等待豆包生成的最长时长(分钟)。超时未成功自动退还
        # 额度(见 video/service.py 退款白名单 + FailureKind.TIMEOUT)。main.py
        # 启动时把 minutes × 60 写入 PlaywrightVideoRunner.timeout。修改设置
        # 后只对下一个 task 生效(live update 不做,见 plan 注释)。
        "default_timeout_minutes": 7,
        # v0.2.34:并发任务间隔(秒)—— 用户在 SettingsPage 调,组提交和单提交
        # 走同一路径(都过 _run_inner)。每个 task 在 assigning 到账号后、
        # 抢 _global_semaphore 之前 sleep 这个时长,避免同 IP 多账号同时操作
        # 触发豆包风控。默认 0 = 不间隔(沿用 v0.2.33 行为)。
        "task_interval_seconds": 0,
        # v0.2.17:浏览器 PC 端版本号,被 video/browser.py 读取塞到
        # payload.client_meta.pc_version。不暴露前端(17-b 时一起加 UI),
        # 升级时只需改这里 + 重启服务。
        "pc_version": "3.27.4",
    }

    # v0.2.29:共享池下不再按 model 拆桶,保留常量以兼容老调用点的 import。
    MODEL_QUOTA_BUCKETS = {
        "seedance_v2.0_mini": "shared",
        "seedance_v2.0": "shared",
        "seedance_v2.0_std": "shared",
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
        # 老库中的 default_duration 行原样保留，但 v0.3.6 不再读取其业务值。
        values["default_duration"] = FIXED_VIDEO_DURATION_SECONDS
        values["data_dir"] = str(self.data_dir)
        return values

    def get_daily_quotas(self) -> dict[str, int]:
        """v0.2.29:共享池 → 返回 {'shared': int}。

        优先级:explicit daily_quota_shared > legacy daily_quota > DEFAULT 50。
        """
        shared = self.repository.get_setting("daily_quota_shared", None)
        if shared is not None:
            return {"shared": int(shared)}
        legacy = self.repository.get_setting("daily_quota", None)
        if legacy is not None:
            return {"shared": int(legacy)}
        return {"shared": int(self.DEFAULTS["daily_quota_shared"])}

    def update(self, changes: dict) -> dict:
        changes = dict(changes)
        current = self.get()
        unknown = set(changes) - set(self.DEFAULTS)
        if unknown:
            raise ValueError(f"不支持的设置：{', '.join(sorted(unknown))}")
        if "default_duration" in changes:
            changes["default_duration"] = FIXED_VIDEO_DURATION_SECONDS
        candidate = {**current, **changes}
        candidate["default_duration"] = FIXED_VIDEO_DURATION_SECONDS
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
        # v0.2.29:用户实测多机并发量大,放宽到 50;默认 1 保留(显式压住老改动)。
        if not 1 <= int(values["max_concurrency"]) <= 50:
            raise ValueError("并发数必须在 1 到 50 之间")
        # v0.2.29:共享池只校验 daily_quota_shared。
        if not 1 <= int(values["daily_quota_shared"]) <= 100:
            raise ValueError("每日额度必须在 1 到 100 之间")
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", str(values["quota_reset_time"])):
            raise ValueError("额度重置时间格式无效")
        if values["scheduler_strategy"] not in {"least_used"}:
            raise ValueError("调度策略无效")
        if values["default_model"] not in {"seedance_v2.0_std", "seedance_v2.0", "seedance_v2.0_mini"}:
            raise ValueError("默认模型无效")
        # v0.2.29:豆包接受任意整数 4..10 秒,放宽白名单。
        if int(values["default_duration"]) != FIXED_VIDEO_DURATION_SECONDS:
            raise ValueError("视频时长固定为 10 秒")
        if values["default_ratio"] not in {"1:1", "3:4", "4:3", "9:16", "16:9", "21:9"}:
            raise ValueError("默认比例无效")
        if values["log_level"] not in {"DEBUG", "INFO", "WARNING", "ERROR"}:
            raise ValueError("日志级别无效")
        if not 1 <= int(values["log_retention_days"]) <= 365:
            raise ValueError("日志保留天数必须在 1 到 365 之间")
        # v0.2.27:超时上限 20 分钟由用户决定 —— 太长会让「真的卡死」的任务
        # 在队列里占名额太久,反而拖慢整批任务周转。1 分钟下限避免误设 0。
        if not 1 <= int(values["default_timeout_minutes"]) <= 20:
            raise ValueError("任务超时必须在 1 到 20 分钟之间")
        # v0.2.34:0 = 不间隔(默认行为),1..60 限上限成 —— 单次间隔 60s 已
        # 足够把 50 并发任务拉到 50 分钟,够「同 IP 触限」缓冲,再高用户自己
        # 拉低 max_concurrency 更合适。
        if not 0 <= int(values["task_interval_seconds"]) <= 60:
            raise ValueError("任务间隔必须在 0 到 60 秒之间")
        if not Path(values["download_dir"]).expanduser().is_absolute():
            raise ValueError("下载目录必须是绝对路径")
