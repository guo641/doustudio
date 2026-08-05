from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path, user_log_path

from doupool.paths import frontend_dir as resolve_frontend_dir


# 上游版本用过的应用名(用于自动迁移老用户的数据目录到新名字)
LEGACY_APP_NAMES: tuple[str, ...] = ("DoubaoManager",)


def _resolve_app_dirs() -> tuple[Path, Path]:
    """
    解析 DouStudio 数据/日志目录,自动把上游遗留的 DoubaoManager 目录迁移过来。

    优先级:
      1. 环境变量 DOUPOOL_DATA_DIR / DOUPOOL_LOG_DIR(用户显式指定)
      2. platformdirs 默认路径,app name = "DouStudio"
      3. 兼容:若 DouStudio 目录不存在,但旧名(DoubaoManager)目录存在,迁移过来
    """
    explicit_data = os.environ.get("DOUPOOL_DATA_DIR")
    explicit_log = os.environ.get("DOUPOOL_LOG_DIR")
    new_data = Path(explicit_data) if explicit_data else Path(user_data_path("DouStudio"))
    new_log = Path(explicit_log) if explicit_log else Path(user_log_path("DouStudio"))

    # 用户显式指定了就别动他的目录,跳过迁移
    if explicit_data or explicit_log:
        return new_data, new_log

    # 不在迁移路径上(已在 DouStudio 下)就直接返回
    if new_data.exists():
        return new_data, new_log

    # 尝试从旧名迁移
    for legacy in LEGACY_APP_NAMES:
        legacy_data = Path(user_data_path(legacy))
        if legacy_data == new_data or not legacy_data.exists():
            continue
        try:
            print(f"[config] 迁移数据目录 {legacy_data} -> {new_data}")
            new_data.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy_data), str(new_data))
        except OSError as exc:
            # 跨盘/权限问题:降级到 copy,旧目录留在那里给用户手动清理
            try:
                print(f"[config] move 失败({exc}),尝试 copy")
                shutil.copytree(legacy_data, new_data)
            except OSError as exc2:
                print(f"[config] 迁移失败,继续使用旧目录: {exc2}")
                return legacy_data, new_log
        # 日志目录同理(放在不同 platformdir 下面,单独迁)
        legacy_log = Path(user_log_path(legacy))
        if legacy_log.exists() and not new_log.exists():
            try:
                new_log.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(legacy_log), str(new_log))
            except OSError:
                pass
        break

    return new_data, new_log


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    log_dir: Path
    frontend_dir: Path
    debug: bool = False
    login_timeout_seconds: int = 300
    version: str = "0.2.25"  # DouStudio 当前版本(updater 用来对比 latest)

    @classmethod
    def from_environment(cls) -> "Settings":
        data_dir, log_dir = _resolve_app_dirs()
        return cls(
            data_dir=data_dir,
            log_dir=log_dir,
            frontend_dir=resolve_frontend_dir(),
            debug=os.environ.get("DOUPOOL_DEBUG", "").lower() in {"1", "true", "yes"},
            version=os.environ.get("DOUSTUDIO_VERSION", "0.2.25"),
        )