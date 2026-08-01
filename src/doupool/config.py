from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from platformdirs import user_data_path, user_log_path

from doupool.paths import frontend_dir as resolve_frontend_dir


@dataclass(frozen=True, slots=True)
class Settings:
    data_dir: Path
    log_dir: Path
    frontend_dir: Path
    debug: bool = False
    login_timeout_seconds: int = 300
    version: str = "0.1.0"  # DouStudio 当前版本(updater 用来对比 latest)

    @classmethod
    def from_environment(cls) -> "Settings":
        data_dir = Path(os.environ.get("DOUPOOL_DATA_DIR", user_data_path("DoubaoManager")))
        log_dir = Path(os.environ.get("DOUPOOL_LOG_DIR", user_log_path("DoubaoManager")))
        return cls(
            data_dir=data_dir,
            log_dir=log_dir,
            frontend_dir=resolve_frontend_dir(),
            debug=os.environ.get("DOUPOOL_DEBUG", "").lower() in {"1", "true", "yes"},
            version=os.environ.get("DOUSTUDIO_VERSION", "0.1.0"),
        )

