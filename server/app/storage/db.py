"""v0.3.1 license server: SQLite 数据库连接 + 表初始化。

只存 HMAC,绝不存 fingerprint 明文,绝不存 license_token 明文。
拖库 = 攻击者能造"任意 fingerprint 都过"的请求,但**不能回溯合法用户**。
"""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from ..config import DB_PATH

logger = logging.getLogger(__name__)


SCHEMA = """
CREATE TABLE IF NOT EXISTS license_activations (
    license_hmac    TEXT PRIMARY KEY,
    fingerprint_hex TEXT NOT NULL,
    expires_at      INTEGER,
    first_seen_at   INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    heartbeat_count INTEGER NOT NULL DEFAULT 0,
    note            TEXT
);
CREATE INDEX IF NOT EXISTS idx_license_first_seen ON license_activations(first_seen_at);

CREATE TABLE IF NOT EXISTS revoked_license_hmac_prefixes (
    prefix      TEXT PRIMARY KEY,
    revoked_at  INTEGER NOT NULL,
    reason      TEXT
);

CREATE TABLE IF NOT EXISTS server_signing_key_history (
    key_id      TEXT PRIMARY KEY,
    public_key  TEXT NOT NULL,
    activated_at INTEGER NOT NULL,
    rotated_at   INTEGER
);
"""


def init_db(path: Path | None = None) -> None:
    """建表(幂等)。"""
    db_path = path or DB_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(SCHEMA)
        # Multiple uvicorn workers can initialize an existing database at once.
        # Serialize the legacy-column check and ALTER so only one worker migrates.
        conn.execute("BEGIN IMMEDIATE")
        cols = {
            row[1]
            for row in conn.execute("PRAGMA table_info(license_activations)")
        }
        if "note" not in cols:
            conn.execute("ALTER TABLE license_activations ADD COLUMN note TEXT")
        conn.commit()
    logger.info("数据库初始化完成: %s", db_path)


@contextmanager
def db_connection() -> Iterator[sqlite3.Connection]:
    """SQLite 连接 context manager。

    用 sqlite3.Row factory 让查询返 dict-like 行。
    """
    conn = sqlite3.connect(DB_PATH, isolation_level=None)  # autocommit,自己管事务
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def upsert_license_activation(
    conn: sqlite3.Connection,
    *,
    license_hmac: str,
    fingerprint_hex: str,
    expires_at: int | None,
    server_time: int,
) -> None:
    """心跳成功时 upsert 该 license 的最后活跃时间。

    首次见 → INSERT;之后 → UPDATE last_seen_at + heartbeat_count。
    """
    conn.execute(
        """
        INSERT INTO license_activations (
            license_hmac, fingerprint_hex, expires_at, first_seen_at, last_seen_at, heartbeat_count
        ) VALUES (?, ?, ?, ?, ?, 1)
        ON CONFLICT(license_hmac) DO UPDATE SET
            last_seen_at = excluded.last_seen_at,
            heartbeat_count = heartbeat_count + 1
        """,
        (license_hmac, fingerprint_hex, expires_at, server_time, server_time),
    )
