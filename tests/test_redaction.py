import logging

from doupool.db.models import AppLog
from doupool.logging.redaction import redact
from doupool.logging.setup import configure_logging


def test_redacts_nested_sensitive_values():
    value = {"Authorization": "Bearer secret", "phone": "13800138000", "ok": 200}
    assert redact(value) == {
        "Authorization": "[REDACTED]",
        "phone": "138****8000",
        "ok": 200,
    }


def test_redacts_cookie_in_message():
    assert "abc123" not in redact("Cookie: session=abc123")


def test_configured_logger_writes_redacted_database_event(database_manager, tmp_path):
    logger = configure_logging(tmp_path, database_enabled=True)
    logger.info(
        "Cookie: session=abc123 phone=13800138000",
        extra={"event": "login_probe"},
    )
    for handler in logger.handlers:
        handler.flush()

    row = AppLog.get()
    assert row.event == "login_probe"
    assert "abc123" not in row.message
    assert "13800138000" not in row.message
    assert "138****8000" in row.message
