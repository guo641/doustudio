from __future__ import annotations

import sys
import sqlite3

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.app.crypto.kms_adapter import hmac_fingerprint_hex
from server.scripts import revoke


def _token() -> tuple[str, bytes]:
    client = Ed25519PrivateKey.generate()
    public_key = client.public_key().public_bytes_raw()
    # revoke.py only needs the v0.3.1 layout to extract the public-key segment.
    token = client.private_bytes_raw() + public_key + b"{}" + (b"s" * 64)
    return token.hex(), public_key


def test_revoke_canonicalizes_uppercase_fingerprint(monkeypatch, tmp_path):
    token_hex, public_key = _token()
    fingerprint = "ab" * 32
    db_path = tmp_path / "license.sqlite3"
    # revoke.py intentionally runs as a script with ``app`` rooted at
    # server/. Patch that module namespace rather than the ``server.app``
    # namespace used by this test.
    revoke_db = __import__("app.storage.db", fromlist=["DB_PATH"])
    monkeypatch.setattr(revoke_db, "DB_PATH", db_path)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "revoke.py",
            "--license-token",
            token_hex,
            "--fingerprint",
            fingerprint.upper(),
            "--reason",
            "test",
        ],
    )

    assert revoke.main() == 0

    expected_prefix = hmac_fingerprint_hex(public_key, fingerprint)[:16]
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            "SELECT prefix, reason FROM revoked_license_hmac_prefixes"
        ).fetchone()
    assert row == (expected_prefix, "test")


def test_revoke_rejects_non_hex_fingerprint(monkeypatch):
    token_hex, _ = _token()
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "revoke.py",
            "--license-token",
            token_hex,
            "--fingerprint",
            "z" * 64,
        ],
    )

    assert revoke.main() == 1
