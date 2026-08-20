"""Validated SQLite queries used by the license administration UI."""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Any, Iterable

from ..config import REVOKED_PREFIX_LEN
from .db import db_connection


_FILTERS = {None, "active_24h", "active_7d", "expiring_30d", "expired"}
_ORDER_FIELDS = {"last_seen_at", "first_seen_at", "expires_at", "heartbeat_count"}
_ORDERS = {"asc", "desc"}
_HMAC_RE = re.compile(r"^[0-9a-f]{64}$")


@dataclass(frozen=True)
class MutationResult:
    changed: int = 0
    skipped: int = 0
    not_found: int = 0


def _active_clause(alias: str = "a") -> str:
    return (
        "NOT EXISTS (SELECT 1 FROM revoked_license_hmac_prefixes r "
        f"WHERE r.prefix = substr({alias}.license_hmac, 1, {REVOKED_PREFIX_LEN}))"
    )


def _validate_listing_args(
    page: int,
    per_page: int,
    order_by: str,
    order: str,
    filter_status: str | None,
) -> None:
    if page < 1:
        raise ValueError("page must be at least 1")
    if not 1 <= per_page <= 100:
        raise ValueError("per_page must be between 1 and 100")
    if order_by not in _ORDER_FIELDS:
        raise ValueError("unsupported order_by")
    if order not in _ORDERS:
        raise ValueError("order must be asc or desc")
    if filter_status not in _FILTERS:
        raise ValueError("unsupported filter_status")


def get_all_licenses(
    *,
    page: int = 1,
    per_page: int = 50,
    order_by: str = "last_seen_at",
    order: str = "desc",
    filter_status: str | None = None,
    now: int | None = None,
) -> tuple[list[dict[str, Any]], int]:
    """Return non-revoked licenses without exposing machine fingerprints."""
    _validate_listing_args(page, per_page, order_by, order, filter_status)
    now = int(time.time()) if now is None else now
    conditions = [_active_clause("a")]
    params: list[Any] = []

    if filter_status == "active_24h":
        conditions.append("a.last_seen_at BETWEEN ? AND ?")
        params.extend((now - 86400, now))
    elif filter_status == "active_7d":
        conditions.append("a.last_seen_at BETWEEN ? AND ?")
        params.extend((now - 7 * 86400, now))
    elif filter_status == "expiring_30d":
        conditions.append("a.expires_at > ? AND a.expires_at <= ?")
        params.extend((now, now + 30 * 86400))
    elif filter_status == "expired":
        conditions.append("a.expires_at IS NOT NULL AND a.expires_at <= ?")
        params.append(now)

    where_sql = " AND ".join(conditions)
    order_sql = order.upper()
    offset = (page - 1) * per_page
    with db_connection() as conn:
        count = conn.execute(
            f"SELECT COUNT(*) FROM license_activations a WHERE {where_sql}",
            params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT a.license_hmac, a.expires_at, a.first_seen_at,
                   a.last_seen_at, a.heartbeat_count, a.note
            FROM license_activations a
            WHERE {where_sql}
            ORDER BY a.{order_by} {order_sql}, a.license_hmac ASC
            LIMIT ? OFFSET ?
            """,
            (*params, per_page, offset),
        ).fetchall()
    return [dict(row) for row in rows], int(count)


def get_statistics(*, now: int | None = None) -> dict[str, int]:
    now = int(time.time()) if now is None else now
    active = _active_clause("a")
    with db_connection() as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE WHEN {active}
                    AND a.last_seen_at BETWEEN ? AND ? THEN 1 ELSE 0 END), 0) AS active_24h,
                COALESCE(SUM(CASE WHEN {active}
                    AND a.last_seen_at BETWEEN ? AND ? THEN 1 ELSE 0 END), 0) AS active_7d,
                COALESCE(SUM(CASE WHEN {active}
                    AND a.expires_at > ? AND a.expires_at <= ? THEN 1 ELSE 0 END), 0) AS expiring_30d
            FROM license_activations a
            """,
            (
                now - 86400,
                now,
                now - 7 * 86400,
                now,
                now,
                now + 30 * 86400,
            ),
        ).fetchone()
        revoked = conn.execute(
            "SELECT COUNT(*) FROM revoked_license_hmac_prefixes"
        ).fetchone()[0]
    return {
        "total": int(row["total"]),
        "active_24h": int(row["active_24h"]),
        "active_7d": int(row["active_7d"]),
        "expiring_30d": int(row["expiring_30d"]),
        "revoked": int(revoked),
    }


def get_heartbeat_distribution(*, now: int | None = None) -> list[dict[str, int | str]]:
    now = int(time.time()) if now is None else now
    counts = [0, 0, 0, 0, 0]
    with db_connection() as conn:
        rows = conn.execute(
            f"SELECT last_seen_at FROM license_activations a WHERE {_active_clause('a')}"
        ).fetchall()
    for row in rows:
        age = now - int(row["last_seen_at"])
        if age < 0:
            counts[4] += 1
        elif age <= 3600:
            counts[0] += 1
        elif age <= 86400:
            counts[1] += 1
        elif age <= 7 * 86400:
            counts[2] += 1
        elif age <= 30 * 86400:
            counts[3] += 1
        else:
            counts[4] += 1
    labels = ["1 小时内", "1 天内", "7 天内", "30 天内", "超过 30 天"]
    total = sum(counts)
    return [
        {
            "label": label,
            "count": count,
            "percent": 0 if total == 0 else round(count * 100 / total),
        }
        for label, count in zip(labels, counts)
    ]


def _deduplicate(values: Iterable[str]) -> list[str]:
    normalized: list[str] = []
    for value in values:
        candidate = value.strip().lower()
        if not _HMAC_RE.fullmatch(candidate):
            raise ValueError("license_hmac must be 64 hexadecimal characters")
        normalized.append(candidate)
    return list(dict.fromkeys(normalized))


def set_license_note(license_hmac: str, note: str) -> MutationResult:
    """Set or clear one license note; blank text is stored as NULL."""
    candidate = license_hmac.strip().lower()
    if not _HMAC_RE.fullmatch(candidate):
        raise ValueError("license_hmac must be 64 hexadecimal characters")
    note = (note or "").strip()
    if len(note) > 200:
        raise ValueError("note must be at most 200 characters")
    value = note or None
    with db_connection() as conn:
        cursor = conn.execute(
            "UPDATE license_activations SET note = ? WHERE license_hmac = ?",
            (value, candidate),
        )
        if cursor.rowcount == 0:
            return MutationResult(not_found=1)
        return MutationResult(changed=1)


def set_license_expiry(
    license_hmacs: Iterable[str],
    expires_at: int | None,
    *,
    now: int | None = None,
) -> MutationResult:
    """Set an absolute server-side expiry; None restores an unlimited license."""
    targets = _deduplicate(license_hmacs)
    if not targets or len(targets) > 50:
        raise ValueError("between 1 and 50 targets are required")
    now = int(time.time()) if now is None else now
    if expires_at is not None:
        expires_at = int(expires_at)
        if not now <= expires_at <= now + 3650 * 86400:
            raise ValueError("expires_at must be within now..now+3650d")
    placeholders = ",".join("?" for _ in targets)

    with db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            found = conn.execute(
                f"SELECT license_hmac FROM license_activations "
                f"WHERE license_hmac IN ({placeholders})",
                targets,
            ).fetchall()
            not_found = len(targets) - len(found)
            if not_found:
                conn.rollback()
                return MutationResult(not_found=not_found)
            conn.execute(
                f"UPDATE license_activations SET expires_at = ? "
                f"WHERE license_hmac IN ({placeholders})",
                (expires_at, *targets),
            )
            conn.commit()
            return MutationResult(changed=len(found))
        except Exception:
            conn.rollback()
            raise


def extend_license_expiry(
    license_hmacs: Iterable[str], days: int, *, now: int | None = None
) -> MutationResult:
    """Extend finite overrides atomically; an unbounded license remains unbounded."""
    targets = _deduplicate(license_hmacs)
    if not targets or len(targets) > 50:
        raise ValueError("between 1 and 50 targets are required")
    if not 1 <= days <= 3650:
        raise ValueError("days must be between 1 and 3650")
    now = int(time.time()) if now is None else now
    placeholders = ",".join("?" for _ in targets)
    seconds = days * 86400

    with db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            rows = conn.execute(
                f"SELECT license_hmac, expires_at FROM license_activations "
                f"WHERE license_hmac IN ({placeholders})",
                targets,
            ).fetchall()
            not_found = len(targets) - len(rows)
            if not_found:
                conn.rollback()
                return MutationResult(not_found=not_found)
            finite = [row["license_hmac"] for row in rows if row["expires_at"] is not None]
            skipped = len(rows) - len(finite)
            if finite:
                finite_placeholders = ",".join("?" for _ in finite)
                cursor = conn.execute(
                    f"""
                    UPDATE license_activations
                    SET expires_at = CASE
                        WHEN expires_at < ? THEN ? + ?
                        ELSE expires_at + ?
                    END
                    WHERE license_hmac IN ({finite_placeholders})
                    """,
                    (now, now, seconds, seconds, *finite),
                )
                changed = cursor.rowcount
            else:
                changed = 0
            conn.commit()
            return MutationResult(changed=changed, skipped=skipped)
        except Exception:
            conn.rollback()
            raise


def revoke_licenses(
    license_hmacs: Iterable[str], reason: str, *, now: int | None = None
) -> MutationResult:
    """Add revocation prefixes in one transaction after all targets are found."""
    targets = _deduplicate(license_hmacs)
    reason = reason.strip()
    if not targets or len(targets) > 50:
        raise ValueError("between 1 and 50 targets are required")
    if not reason or len(reason) > 200:
        raise ValueError("reason must contain between 1 and 200 characters")
    now = int(time.time()) if now is None else now
    placeholders = ",".join("?" for _ in targets)

    with db_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            found = conn.execute(
                f"SELECT license_hmac FROM license_activations "
                f"WHERE license_hmac IN ({placeholders})",
                targets,
            ).fetchall()
            not_found = len(targets) - len(found)
            if not_found:
                conn.rollback()
                return MutationResult(not_found=not_found)

            prefixes = list(
                dict.fromkeys(
                    row["license_hmac"][:REVOKED_PREFIX_LEN] for row in found
                )
            )
            prefix_placeholders = ",".join("?" for _ in prefixes)
            existing = {
                row["prefix"]
                for row in conn.execute(
                    f"SELECT prefix FROM revoked_license_hmac_prefixes "
                    f"WHERE prefix IN ({prefix_placeholders})",
                    prefixes,
                ).fetchall()
            }
            new_prefixes = [prefix for prefix in prefixes if prefix not in existing]
            conn.executemany(
                "INSERT INTO revoked_license_hmac_prefixes (prefix, revoked_at, reason) "
                "VALUES (?, ?, ?)",
                [(prefix, now, reason) for prefix in new_prefixes],
            )
            conn.commit()
            return MutationResult(changed=len(new_prefixes), skipped=len(existing))
        except Exception:
            conn.rollback()
            raise


def get_revoked_licenses() -> list[dict[str, Any]]:
    with db_connection() as conn:
        rows = conn.execute(
            """
            SELECT prefix, revoked_at, reason
            FROM revoked_license_hmac_prefixes
            ORDER BY revoked_at DESC, prefix ASC
            """
        ).fetchall()
    return [dict(row) for row in rows]
