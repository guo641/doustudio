from __future__ import annotations

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from doupool.db.models import AppLog

from .redaction import redact

# v0.2.16:日志时间戳统一按北京时间,跟 OS 时区解耦。
# 默认 logging.Formatter 用 time.localtime(),如果用户机器时区配 UTC
# (Windows server / docker)日志就晚 8 小时,UI 上看着别扭。
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class RedactingFormatter(logging.Formatter):
    """v0.2.16:asctime 强制 Asia/Shanghai,不让 OS 时区影响日志观感。

    用户本地就是北京时间,日志里的 2026-08-04 17:23:48 跟用户手机一致。
    业务时间(text 字段里的时间戳)由 `db.models.utcnow()` 写入,也是北京时间,口径一致。
    """

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=_SHANGHAI)
        if datefmt:
            return dt.strftime(datefmt)
        # 默认格式: 2026-08-04 17:23:48,123
        return dt.strftime("%Y-%m-%d %H:%M:%S") + f",{int(record.msecs):03d}"

    def format(self, record: logging.LogRecord) -> str:
        return str(redact(super().format(record)))


class DatabaseLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            AppLog.create(
                level=record.levelname,
                module=record.name,
                event=getattr(record, "event", "log"),
                message=redact(record.getMessage()),
                account=getattr(record, "account_id", None),
                login_attempt=getattr(record, "login_attempt_id", None),
            )
        except Exception:
            self.handleError(record)


def configure_logging(log_dir: Path, database_enabled: bool = True) -> logging.Logger:
    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("doupool")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    for handler in list(logger.handlers):
        handler.close()
        logger.removeHandler(handler)

    formatter = RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    file_handler = RotatingFileHandler(
        log_dir / "doupool.log",
        maxBytes=5_000_000,
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(file_handler)
    if database_enabled:
        logger.addHandler(DatabaseLogHandler())
    return logger


def set_log_level(level: str) -> None:
    logging.getLogger("doupool").setLevel(getattr(logging, level, logging.INFO))
