from __future__ import annotations

from pathlib import Path

from peewee import SqliteDatabase

from .models import ALL_MODELS, AppSetting, VideoTask, database_proxy


class DatabaseManager:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.database = SqliteDatabase(
            self.path,
            pragmas={"foreign_keys": 1, "journal_mode": "wal", "busy_timeout": 5000},
        )

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        database_proxy.initialize(self.database)
        self.database.connect(reuse_if_open=True)
        self.database.execute_sql(
            "CREATE TABLE IF NOT EXISTS schema_version "
            "(version INTEGER NOT NULL PRIMARY KEY)"
        )
        row = self.database.execute_sql("SELECT MAX(version) FROM schema_version").fetchone()
        version = row[0]
        if version is None:
            with self.database.atomic():
                self.database.create_tables(ALL_MODELS)
                self.database.execute_sql("INSERT INTO schema_version(version) VALUES (10)")
            return
        if version < 2:
            with self.database.atomic():
                self.database.create_tables((VideoTask,), safe=True)
                self.database.execute_sql("INSERT INTO schema_version(version) VALUES (2)")
            version = 2
        if version < 3:
            columns = {
                row[1] for row in self.database.execute_sql("PRAGMA table_info(videotask)").fetchall()
            }
            with self.database.atomic():
                for name in ("vid", "backup_result_url", "fallback_result_url"):
                    if name not in columns:
                        self.database.execute_sql(f"ALTER TABLE videotask ADD COLUMN {name} TEXT")
                self.database.execute_sql("INSERT INTO schema_version(version) VALUES (3)")
            version = 3
        if version < 4:
            account_columns = {
                row[1] for row in self.database.execute_sql("PRAGMA table_info(account)").fetchall()
            }
            task_columns = {
                row[1]: row for row in self.database.execute_sql("PRAGMA table_info(videotask)").fetchall()
            }
            self.database.execute_sql("PRAGMA foreign_keys=OFF")
            try:
                with self.database.atomic():
                    if "video_quota_used" not in account_columns:
                        self.database.execute_sql(
                            "ALTER TABLE account ADD COLUMN video_quota_used INTEGER NOT NULL DEFAULT 0"
                        )
                    if "video_quota_date" not in account_columns:
                        self.database.execute_sql("ALTER TABLE account ADD COLUMN video_quota_date DATE")
                    if "video_limited_until" not in account_columns:
                        self.database.execute_sql("ALTER TABLE account ADD COLUMN video_limited_until DATETIME")
                    if task_columns.get("account_id", (None, None, None, 0))[3]:
                        self._make_video_account_nullable()
                    self.database.create_tables((AppSetting,), safe=True)
                    self.database.execute_sql("INSERT INTO schema_version(version) VALUES (4)")
            finally:
                self.database.execute_sql("PRAGMA foreign_keys=ON")
            version = 4
        if version < 5:
            columns = {
                row[1] for row in self.database.execute_sql("PRAGMA table_info(videotask)").fetchall()
            }
            with self.database.atomic():
                if "mode" not in columns:
                    self.database.execute_sql(
                        "ALTER TABLE videotask ADD COLUMN mode VARCHAR(255) NOT NULL DEFAULT 't2v'"
                    )
                if "image_paths" not in columns:
                    self.database.execute_sql("ALTER TABLE videotask ADD COLUMN image_paths TEXT")
                self.database.execute_sql("INSERT INTO schema_version(version) VALUES (5)")
            version = 5
        if version < 6:
            columns = {
                row[1] for row in self.database.execute_sql("PRAGMA table_info(videotask)").fetchall()
            }
            with self.database.atomic():
                if "clean_video_url" not in columns:
                    self.database.execute_sql("ALTER TABLE videotask ADD COLUMN clean_video_url TEXT")
                if "clean_error" not in columns:
                    self.database.execute_sql("ALTER TABLE videotask ADD COLUMN clean_error TEXT")
                self.database.execute_sql("INSERT INTO schema_version(version) VALUES (6)")
            version = 6
        if version < 7:
            columns = {
                row[1] for row in self.database.execute_sql("PRAGMA table_info(videotask)").fetchall()
            }
            with self.database.atomic():
                if "original_prompt" not in columns:
                    self.database.execute_sql("ALTER TABLE videotask ADD COLUMN original_prompt TEXT")
                if "prompt_retry_count" not in columns:
                    self.database.execute_sql(
                        "ALTER TABLE videotask ADD COLUMN prompt_retry_count INTEGER NOT NULL DEFAULT 0"
                    )
                self.database.execute_sql("INSERT INTO schema_version(version) VALUES (7)")
            version = 7
        if version < 8:
            columns = {
                row[1] for row in self.database.execute_sql("PRAGMA table_info(videotask)").fetchall()
            }
            with self.database.atomic():
                if "group_id" not in columns:
                    self.database.execute_sql("ALTER TABLE videotask ADD COLUMN group_id VARCHAR(255)")
                    self.database.execute_sql(
                        "CREATE INDEX IF NOT EXISTS videotask_group_id ON videotask(group_id)"
                    )
                if "group_index" not in columns:
                    self.database.execute_sql(
                        "ALTER TABLE videotask ADD COLUMN group_index INTEGER NOT NULL DEFAULT 0"
                    )
                self.database.execute_sql("INSERT INTO schema_version(version) VALUES (8)")
            version = 8
        if version < 9:
            # v0.2.9:按 seedance 模型拆 daily_quota。Account 加 3 个 used 列
            # (mini / v2 / std),老 video_quota_used 单列保留做兼容字段。
            account_columns = {
                row[1] for row in self.database.execute_sql("PRAGMA table_info(account)").fetchall()
            }
            with self.database.atomic():
                for name in ("video_quota_used_mini", "video_quota_used_v2", "video_quota_used_std"):
                    if name not in account_columns:
                        self.database.execute_sql(
                            f"ALTER TABLE account ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0"
                        )
                # 老数据归到 mini 桶(无 model 信息,mini 是默认模型)。
                # 只迁未置的(避免覆盖新装分支已写入的值)。
                if "video_quota_used" in account_columns:
                    self.database.execute_sql(
                        "UPDATE account SET video_quota_used_mini = video_quota_used "
                        "WHERE video_quota_used_mini = 0 AND video_quota_used > 0"
                    )
                self.database.execute_sql("INSERT INTO schema_version(version) VALUES (9)")
        if version < 10:
            # v0.2.10:补 e4ced5a 漏掉的 callbackUrl 迁移。VideoTask 加 4 个 callback
            # 列(callback_url / status / attempts / last_error),幂等 ALTER 兜住
            # v9 DB(已升 v9 但还没建 callback 列的用户)。v0.2.9 双击 exe 就崩的根因
            # 就在这里 — peewee 在 lifespan 里 SELECT 全部 VideoTask 字段,旧 DB 没
            # callback_url 直接 OperationalError。
            task_columns = {
                row[1] for row in self.database.execute_sql("PRAGMA table_info(videotask)").fetchall()
            }
            with self.database.atomic():
                if "callback_url" not in task_columns:
                    self.database.execute_sql(
                        "ALTER TABLE videotask ADD COLUMN callback_url TEXT"
                    )
                if "callback_status" not in task_columns:
                    self.database.execute_sql(
                        "ALTER TABLE videotask ADD COLUMN callback_status VARCHAR(255)"
                    )
                if "callback_attempts" not in task_columns:
                    self.database.execute_sql(
                        "ALTER TABLE videotask ADD COLUMN callback_attempts INTEGER NOT NULL DEFAULT 0"
                    )
                if "callback_last_error" not in task_columns:
                    self.database.execute_sql(
                        "ALTER TABLE videotask ADD COLUMN callback_last_error TEXT"
                    )
                self.database.execute_sql("INSERT INTO schema_version(version) VALUES (10)")

    def _make_video_account_nullable(self) -> None:
        self.database.execute_sql("""
            CREATE TABLE videotask_v4 (
                id VARCHAR(255) NOT NULL PRIMARY KEY, account_id VARCHAR(255), prompt TEXT NOT NULL,
                model VARCHAR(255) NOT NULL, ratio VARCHAR(255) NOT NULL, duration INTEGER NOT NULL,
                status VARCHAR(255) NOT NULL, conversation_id VARCHAR(255), section_id VARCHAR(255),
                question_id VARCHAR(255), remote_task_id VARCHAR(255), vid VARCHAR(255), result_url TEXT,
                backup_result_url TEXT, fallback_result_url TEXT, cover_url TEXT, error_message TEXT,
                created_at DATETIME NOT NULL, updated_at DATETIME NOT NULL, completed_at DATETIME,
                FOREIGN KEY (account_id) REFERENCES account(id)
            )
        """)
        columns = (
            "id, account_id, prompt, model, ratio, duration, status, conversation_id, section_id, "
            "question_id, remote_task_id, vid, result_url, backup_result_url, fallback_result_url, "
            "cover_url, error_message, created_at, updated_at, completed_at"
        )
        self.database.execute_sql(f"INSERT INTO videotask_v4 ({columns}) SELECT {columns} FROM videotask")
        self.database.execute_sql("DROP TABLE videotask")
        self.database.execute_sql("ALTER TABLE videotask_v4 RENAME TO videotask")
        self.database.execute_sql("CREATE INDEX videotask_account_id ON videotask(account_id)")

    def close(self) -> None:
        if not self.database.is_closed():
            self.database.close()
