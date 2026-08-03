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
        assert manager.database.execute_sql("SELECT MAX(version) FROM schema_version").fetchone()[0] == 9
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


# ---------- v0.2.9 schema v9:per-model quota 三桶迁移 ----------

def test_version_eight_database_migrates_to_v9_adding_three_quota_columns(tmp_path):
    """v0.2.9:从 v8 升 v9 给 account 表加 3 个 quota 列,并把老 video_quota_used
    落到 mini 桶(老数据无 model 信息,mini 是默认模型)。"""
    import sqlite3
    from doupool.db.database import DatabaseManager

    path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(path) as db:
        db.executescript("""
        CREATE TABLE schema_version (version INTEGER NOT NULL PRIMARY KEY);
        INSERT INTO schema_version VALUES (8);
        CREATE TABLE account (
          id TEXT PRIMARY KEY,
          display_name TEXT NOT NULL,
          doubao_user_id TEXT,
          doubao_nickname TEXT,
          profile_dir TEXT NOT NULL,
          status TEXT NOT NULL,
          enabled INTEGER NOT NULL,
          last_verified_at DATETIME,
          last_error TEXT,
          video_quota_used INTEGER NOT NULL DEFAULT 0,
          video_quota_date DATE,
          video_limited_until DATETIME,
          callback_url TEXT,
          callback_status TEXT,
          callback_attempts INTEGER NOT NULL DEFAULT 0,
          callback_last_error TEXT,
          callback_last_at DATETIME,
          created_at DATETIME NOT NULL,
          updated_at DATETIME NOT NULL
        );
        INSERT INTO account VALUES (
          'a1','老账号','u1',NULL,'/tmp/profile','active',1,NULL,NULL,
          4,NULL,NULL,NULL,NULL,0,NULL,NULL,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP
        );
        """)

    manager = DatabaseManager(path)
    manager.initialize()
    try:
        # 升级到 v9
        assert manager.database.execute_sql(
            "SELECT MAX(version) FROM schema_version"
        ).fetchone()[0] == 9
        # 三列都已加
        columns = {row[1] for row in manager.database.execute_sql("PRAGMA table_info(account)")}
        assert "video_quota_used_mini" in columns
        assert "video_quota_used_v2" in columns
        assert "video_quota_used_std" in columns
        # 老 used 自动落到 mini 桶
        assert manager.database.execute_sql(
            "SELECT video_quota_used_mini FROM account WHERE id='a1'"
        ).fetchone()[0] == 4
        # v2/std 默认 0
        assert manager.database.execute_sql(
            "SELECT video_quota_used_v2 FROM account WHERE id='a1'"
        ).fetchone()[0] == 0
        assert manager.database.execute_sql(
            "SELECT video_quota_used_std FROM account WHERE id='a1'"
        ).fetchone()[0] == 0
    finally:
        manager.close()


def test_v9_migration_is_idempotent(tmp_path):
    """v0.2.9:重跑 initialize() 不能报错 / 重复加列(幂等迁移,老 DB 双开安全)。"""
    from doupool.db.database import DatabaseManager

    path = tmp_path / "double-init.sqlite3"
    manager = DatabaseManager(path)
    try:
        # 第二次 initialize 不应炸
        manager.initialize()
        manager.initialize()
        # schema_version 只升到 v9,没有重复行
        rows = list(manager.database.execute_sql(
            "SELECT version FROM schema_version ORDER BY version"
        ).fetchall())
        assert [r[0] for r in rows] == [9]
    finally:
        manager.close()
