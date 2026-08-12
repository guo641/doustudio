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


@pytest.fixture(autouse=True)
def _auto_activate_for_non_license_tests(monkeypatch, request):
    """v0.3.0:让所有非 license 测试自动通过授权闸门。

    离线激活闸门(`authorize_with_license`)会读 activated.bin → 返 status;
    没有 activated.bin 时返 'missing' → 403,前端拿不到 API。

    测试用的 tmp_path 没有 activated.bin,本来应该每个测试都 seed 一个,
    那样要改 50+ 个测试。改用 monkeypatch 注入:'valid' 表示授权通过。

    例外:
      - tests/test_license_*.py:本身就是 license 行为测试,不能 mock
      - 单个测试需要真实 status 时,加 `@pytest.mark.real_license` 标记
    """
    test_path = str(request.fspath)
    if "test_license_" in test_path:
        return
    if "test_license_verifier" in test_path:
        return
    if request.node.get_closest_marker("real_license"):
        return

    # 默认 mock 为 'valid'(已激活),让现有 API 测试无需感知 license。
    from doupool import license as _lic
    monkeypatch.setattr(_lic, "get_activation_status", lambda: "valid")
    monkeypatch.setattr(_lic, "current_fingerprint", lambda: "0" * 64)

