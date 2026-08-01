from doupool.db.models import Account, LoginAttempt


def test_complete_login_creates_account(repository, temp_profile):
    attempt = repository.create_login_attempt()
    account = repository.complete_login(
        attempt.id,
        {"user_id": "u-1", "nickname": "莲韵"},
        temp_profile,
    )

    assert account.doubao_user_id == "u-1"
    assert account.doubao_nickname == "莲韵"
    assert account.status == "active"
    assert LoginAttempt.get_by_id(attempt.id).state == "succeeded"
    assert Account.select().count() == 1


def test_complete_login_updates_existing_user(repository, temp_profile, tmp_path):
    first = repository.create_login_attempt()
    original = repository.complete_login(
        first.id, {"user_id": "u-1", "nickname": "旧昵称"}, temp_profile
    )
    second_profile = tmp_path / "second-profile"
    second_profile.mkdir()
    second = repository.create_login_attempt(original.id)

    updated = repository.complete_login(
        second.id,
        {"user_id": "u-1", "nickname": "新昵称"},
        str(second_profile),
    )

    assert updated.id == original.id
    assert updated.doubao_nickname == "新昵称"
    assert Account.select().count() == 1


def test_sqlite_pragmas_are_enabled(database_manager):
    database = database_manager.database
    assert database.execute_sql("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert database.execute_sql("PRAGMA foreign_keys").fetchone()[0] == 1
    assert database.execute_sql("PRAGMA busy_timeout").fetchone()[0] == 5000


def test_version_three_database_migrates_without_losing_related_rows(tmp_path):
    import sqlite3
    from doupool.db.database import DatabaseManager

    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL PRIMARY KEY);
        INSERT INTO schema_version VALUES (3);
        CREATE TABLE account (id TEXT PRIMARY KEY, display_name TEXT NOT NULL, doubao_user_id TEXT, doubao_nickname TEXT, profile_dir TEXT NOT NULL, status TEXT NOT NULL, enabled INTEGER NOT NULL, last_verified_at DATETIME, last_error TEXT, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL);
        CREATE TABLE loginattempt (id TEXT PRIMARY KEY, account_id TEXT REFERENCES account(id), state TEXT NOT NULL, error_code TEXT, error_message TEXT, started_at DATETIME NOT NULL, finished_at DATETIME);
        CREATE TABLE applog (id INTEGER PRIMARY KEY, level TEXT NOT NULL, module TEXT NOT NULL, event TEXT NOT NULL, message TEXT NOT NULL, account_id TEXT REFERENCES account(id), login_attempt_id TEXT REFERENCES loginattempt(id), created_at DATETIME NOT NULL);
        CREATE TABLE videotask (id TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES account(id), prompt TEXT NOT NULL, model TEXT NOT NULL, ratio TEXT NOT NULL, duration INTEGER NOT NULL, status TEXT NOT NULL, conversation_id TEXT, section_id TEXT, question_id TEXT, remote_task_id TEXT, vid TEXT, result_url TEXT, backup_result_url TEXT, fallback_result_url TEXT, cover_url TEXT, error_message TEXT, created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, completed_at DATETIME);
        INSERT INTO account VALUES ('a1','账号','u1',NULL,'/tmp/profile','active',1,NULL,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP);
        INSERT INTO loginattempt VALUES ('l1','a1','succeeded',NULL,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP);
        INSERT INTO videotask VALUES ('t1','a1','提示词','seedance_v2.0_mini','1:1',5,'succeeded',NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP);
        """)

    manager = DatabaseManager(path)
    manager.initialize()
    try:
        assert manager.database.execute_sql("SELECT MAX(version) FROM schema_version").fetchone()[0] == 5
        assert manager.database.execute_sql("SELECT COUNT(*) FROM account").fetchone()[0] == 1
        assert manager.database.execute_sql("SELECT COUNT(*) FROM loginattempt").fetchone()[0] == 1
        columns = {row[1] for row in manager.database.execute_sql("PRAGMA table_info(videotask)")}
        account_column = next(row for row in manager.database.execute_sql("PRAGMA table_info(videotask)") if row[1] == "account_id")
        assert account_column[3] == 0
        assert "mode" in columns
        assert "image_paths" in columns
        assert manager.database.execute_sql("SELECT mode FROM videotask WHERE id='t1'").fetchone()[0] == "t2v"
    finally:
        manager.close()
