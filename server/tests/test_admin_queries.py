from __future__ import annotations

import sqlite3

import pytest

from server.app.storage import admin_queries, db


NOW = 2_000_000_000


@pytest.fixture
def admin_db(tmp_path, monkeypatch):
    db_path = tmp_path / "license.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", db_path)
    db.init_db(db_path)
    return db_path


def _insert_license(
    db_path,
    license_hmac: str,
    *,
    expires_at: int | None,
    first_seen_at: int = NOW - 90 * 86400,
    last_seen_at: int = NOW - 60,
    heartbeat_count: int = 1,
    fingerprint_hex: str = "fa" * 32,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            INSERT INTO license_activations (
                license_hmac, fingerprint_hex, expires_at, first_seen_at,
                last_seen_at, heartbeat_count
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                license_hmac,
                fingerprint_hex,
                expires_at,
                first_seen_at,
                last_seen_at,
                heartbeat_count,
            ),
        )


def test_statistics_and_distribution_exclude_revoked_activity(admin_db):
    active = "1" * 64
    recent = "2" * 64
    stale = "3" * 64
    revoked = "4" * 64
    _insert_license(admin_db, active, expires_at=NOW + 10 * 86400)
    _insert_license(
        admin_db,
        recent,
        expires_at=NOW - 1,
        last_seen_at=NOW - 2 * 86400,
    )
    _insert_license(
        admin_db,
        stale,
        expires_at=None,
        last_seen_at=NOW - 40 * 86400,
    )
    _insert_license(admin_db, revoked, expires_at=NOW + 10 * 86400)
    with sqlite3.connect(admin_db) as conn:
        conn.execute(
            "INSERT INTO revoked_license_hmac_prefixes VALUES (?, ?, ?)",
            (revoked[:16], NOW, "test"),
        )

    assert admin_queries.get_statistics(now=NOW) == {
        "total": 4,
        "active_24h": 1,
        "active_7d": 2,
        "expiring_30d": 1,
        "revoked": 1,
    }
    distribution = admin_queries.get_heartbeat_distribution(now=NOW)
    assert [bucket["count"] for bucket in distribution] == [1, 0, 1, 0, 1]
    assert sum(int(bucket["count"]) for bucket in distribution) == 3


def test_listing_filters_sorts_pages_and_never_returns_fingerprint(admin_db):
    active = "a" * 64
    expired = "b" * 64
    permanent = "c" * 64
    revoked = "d" * 64
    _insert_license(
        admin_db,
        active,
        expires_at=NOW + 5 * 86400,
        last_seen_at=NOW - 10,
        heartbeat_count=4,
        fingerprint_hex="secret-fingerprint-active",
    )
    _insert_license(
        admin_db,
        expired,
        expires_at=NOW - 1,
        last_seen_at=NOW - 2 * 86400,
        heartbeat_count=8,
        fingerprint_hex="secret-fingerprint-expired",
    )
    _insert_license(
        admin_db,
        permanent,
        expires_at=None,
        last_seen_at=NOW - 20 * 86400,
        heartbeat_count=2,
        fingerprint_hex="secret-fingerprint-permanent",
    )
    _insert_license(admin_db, revoked, expires_at=NOW + 100, heartbeat_count=99)
    with sqlite3.connect(admin_db) as conn:
        conn.execute(
            "INSERT INTO revoked_license_hmac_prefixes VALUES (?, ?, ?)",
            (revoked[:16], NOW, "hidden"),
        )

    rows, total = admin_queries.get_all_licenses(
        page=1, per_page=2, order_by="heartbeat_count", order="desc", now=NOW
    )
    assert total == 3
    assert [row["license_hmac"] for row in rows] == [expired, active]
    assert all("fingerprint_hex" not in row for row in rows)
    assert "secret-fingerprint" not in repr(rows)

    second_page, second_total = admin_queries.get_all_licenses(
        page=2, per_page=2, order_by="heartbeat_count", order="desc", now=NOW
    )
    assert second_total == 3
    assert [row["license_hmac"] for row in second_page] == [permanent]

    active_rows, _ = admin_queries.get_all_licenses(
        filter_status="active_24h", now=NOW
    )
    expiring_rows, _ = admin_queries.get_all_licenses(
        filter_status="expiring_30d", now=NOW
    )
    expired_rows, _ = admin_queries.get_all_licenses(
        filter_status="expired", now=NOW
    )
    assert [row["license_hmac"] for row in active_rows] == [active]
    assert [row["license_hmac"] for row in expiring_rows] == [active]
    assert [row["license_hmac"] for row in expired_rows] == [expired]


@pytest.mark.parametrize(
    "kwargs",
    [
        {"page": 0},
        {"per_page": 0},
        {"per_page": 101},
        {"order_by": "license_hmac"},
        {"order": "sideways"},
        {"filter_status": "unknown"},
    ],
)
def test_listing_rejects_unallowlisted_arguments(admin_db, kwargs):
    with pytest.raises(ValueError):
        admin_queries.get_all_licenses(**kwargs)


def test_extend_handles_finite_expired_and_unbounded_licenses(admin_db):
    future = "5" * 64
    expired = "6" * 64
    unbounded = "7" * 64
    _insert_license(admin_db, future, expires_at=NOW + 100)
    _insert_license(admin_db, expired, expires_at=NOW - 100)
    _insert_license(admin_db, unbounded, expires_at=None)

    result = admin_queries.extend_license_expiry(
        [future.upper(), expired, unbounded], 2, now=NOW
    )

    assert result == admin_queries.MutationResult(changed=2, skipped=1)
    with sqlite3.connect(admin_db) as conn:
        values = dict(
            conn.execute(
                "SELECT license_hmac, expires_at FROM license_activations"
            ).fetchall()
        )
    assert values[future] == NOW + 100 + 2 * 86400
    assert values[expired] == NOW + 2 * 86400
    assert values[unbounded] is None


def test_extend_with_any_missing_target_is_atomic(admin_db):
    existing = "8" * 64
    missing = "9" * 64
    original_expiry = NOW + 123
    _insert_license(admin_db, existing, expires_at=original_expiry)

    result = admin_queries.extend_license_expiry(
        [existing, missing], 365, now=NOW
    )

    assert result == admin_queries.MutationResult(not_found=1)
    with sqlite3.connect(admin_db) as conn:
        actual = conn.execute(
            "SELECT expires_at FROM license_activations WHERE license_hmac = ?",
            (existing,),
        ).fetchone()[0]
    assert actual == original_expiry


def test_revoke_is_idempotent_and_returns_ordered_records(admin_db):
    target = "e" * 64
    _insert_license(admin_db, target, expires_at=NOW + 86400)

    first = admin_queries.revoke_licenses(
        [target.upper(), target], " policy test ", now=NOW
    )
    second = admin_queries.revoke_licenses([target], "again", now=NOW + 1)

    assert first == admin_queries.MutationResult(changed=1)
    assert second == admin_queries.MutationResult(skipped=1)
    assert admin_queries.get_revoked_licenses() == [
        {"prefix": target[:16], "revoked_at": NOW, "reason": "policy test"}
    ]


def test_revoke_with_any_missing_target_is_atomic(admin_db):
    existing = "f" * 64
    missing = "0" * 64
    _insert_license(admin_db, existing, expires_at=NOW + 86400)

    result = admin_queries.revoke_licenses([existing, missing], "test", now=NOW)

    assert result == admin_queries.MutationResult(not_found=1)
    with sqlite3.connect(admin_db) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM revoked_license_hmac_prefixes"
        ).fetchone()[0]
    assert count == 0
