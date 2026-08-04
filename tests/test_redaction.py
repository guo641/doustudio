import logging

from doupool.db.models import AppLog
from doupool.logging.redaction import redact
from doupool.logging.setup import RedactingFormatter, configure_logging


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


def test_formatter_uses_shanghai_timezone_regardless_of_os():
    """v0.2.16:RedactingFormatter.formatTime 强制 Asia/Shanghai,跟 OS 时区解耦。

    测试:塞一个 created=某 UTC 时刻 的 LogRecord,formatTime 出来的时间
    应该是 created 时刻 + 8h(北京时间),而不是 OS 时区算出的值。
    """
    from datetime import UTC, datetime
    from zoneinfo import ZoneInfo

    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(message)s")
    # 2026-08-04 09:00:00 UTC = 2026-08-04 17:00:00 Asia/Shanghai
    record = logging.LogRecord(
        name="doupool.test", level=logging.INFO, pathname="x", lineno=1,
        msg="hello", args=(), exc_info=None,
    )
    utc_dt = datetime(2026, 8, 4, 9, 0, 0, tzinfo=UTC)
    record.created = utc_dt.timestamp()
    record.msecs = 0

    formatted = fmt.format(record)
    # 期望时间戳是北京时间 17:00:00,000
    assert formatted.startswith("2026-08-04 17:00:00,000"), formatted

    # 直接用 tz 验证:Shanghai 都 17:00:00
    shanghai = ZoneInfo("Asia/Shanghai")
    expected = datetime.fromtimestamp(record.created, tz=shanghai).strftime("%Y-%m-%d %H:%M:%S")
    assert expected in formatted
