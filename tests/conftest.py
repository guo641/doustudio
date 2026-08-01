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

