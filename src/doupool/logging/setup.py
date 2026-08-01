from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from doupool.db.models import AppLog

from .redaction import redact


class RedactingFormatter(logging.Formatter):
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
