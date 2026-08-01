import datetime as _datetime
# Python 3.10 缺 `datetime.UTC`,给 3.11+ 兼容 shim(项目 pyproject requires>=3.12,
# 但本机开发可能是 3.10 — 让测试能跑)。
if not hasattr(_datetime, "UTC"):
    from datetime import timezone

    _datetime.UTC = timezone.utc

# 同理,Python 3.10 缺 `enum.StrEnum`,给 3.11+ 兼容 shim
import enum as _enum  # noqa: E402

if not hasattr(_enum, "StrEnum"):
    class _StrEnum(str, _enum.Enum):
        """3.11+ StrEnum 的最小兼容实现,供旧 Python 跑测试用"""

        def __new__(cls, value):
            obj = str.__new__(cls, value)
            obj._value_ = value
            return obj

    _enum.StrEnum = _StrEnum
    import sys

    sys.modules["enum"].StrEnum = _StrEnum

from pathlib import Path

import pytest


@pytest.fixture
def database_manager(tmp_path: Path):
    from doupool.db.database import DatabaseManager

    manager = DatabaseManager(tmp_path / "test.sqlite3")
    manager.initialize()
    yield manager
    manager.close()


@pytest.fixture
def repository(database_manager):
    from doupool.db.repository import AccountRepository

    return AccountRepository(database_manager.database)


@pytest.fixture
def temp_profile(tmp_path: Path) -> str:
    path = tmp_path / "profile"
    path.mkdir()
    return str(path)

