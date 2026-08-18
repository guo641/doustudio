from __future__ import annotations

import json
import sqlite3
import time

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from server.app import config
from server.app.api import heartbeat
from server.app.crypto.verify import verify_heartbeat_request


def _request(
    developer_key: Ed25519PrivateKey,
    client_key: Ed25519PrivateKey,
    *,
    fingerprint: str = "ab" * 32,
    timestamp: int | None = None,
) -> dict[str, str | int]:
    timestamp = int(time.time()) if timestamp is None else timestamp
    payload = {
        "v": 2,
        "fingerprint_hex": fingerprint,
        "customer": "security-test",
        "issued_at": timestamp - 1,
        "expires_at": timestamp + 3600,
    }
    payload_bytes = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    client_seed = client_key.private_bytes_raw()
    client_pub = client_key.public_key().public_bytes_raw()
    token = client_seed + client_pub + payload_bytes + developer_key.sign(payload_bytes)
    nonce = b"n" * 16
    client_sig = client_key.sign(
        fingerprint.encode("ascii") + str(timestamp).encode("ascii") + nonce
    )
    return {
        "license_token": token.hex(),
        "fingerprint": fingerprint,
        "client_pubkey": client_pub.hex(),
        "timestamp": timestamp,
        "client_sig": client_sig.hex(),
        "nonce": nonce.hex(),
    }


def _empty_license_db() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE license_activations "
        "(license_hmac TEXT PRIMARY KEY, expires_at INTEGER)"
    )
    conn.execute(
        "CREATE TABLE revoked_license_hmac_prefixes "
        "(prefix TEXT PRIMARY KEY)"
    )
    return conn


@pytest.mark.parametrize("configured", ["", "too-short", "g" * 64])
def test_missing_or_invalid_developer_key_fails_closed(monkeypatch, configured):
    developer = Ed25519PrivateKey.generate()
    client = Ed25519PrivateKey.generate()
    monkeypatch.setattr(config, "CLIENT_PUBKEY_HEX", configured)
    monkeypatch.delenv("DOUSTUDIO_ALLOW_UNSIGNED", raising=False)

    result = verify_heartbeat_request(
        _request(developer, client), _empty_license_db(), int(time.time())
    )

    assert not result.ok
    assert result.error_code == "bad_signature"


def test_unsigned_override_requires_exact_one(monkeypatch):
    developer = Ed25519PrivateKey.generate()
    client = Ed25519PrivateKey.generate()
    monkeypatch.setattr(config, "CLIENT_PUBKEY_HEX", "")

    for value in ("0", "true", "01"):
        monkeypatch.setenv("DOUSTUDIO_ALLOW_UNSIGNED", value)
        result = verify_heartbeat_request(
            _request(developer, client), _empty_license_db(), int(time.time())
        )
        assert not result.ok
        assert result.error_code == "bad_signature"

    monkeypatch.setenv("DOUSTUDIO_ALLOW_UNSIGNED", "1")
    result = verify_heartbeat_request(
        _request(developer, client), _empty_license_db(), int(time.time())
    )
    assert result.ok


def test_wrong_developer_signature_is_rejected(monkeypatch):
    expected_developer = Ed25519PrivateKey.generate()
    signing_developer = Ed25519PrivateKey.generate()
    client = Ed25519PrivateKey.generate()
    monkeypatch.setattr(
        config, "CLIENT_PUBKEY_HEX", expected_developer.public_key().public_bytes_raw().hex()
    )

    result = verify_heartbeat_request(
        _request(signing_developer, client), _empty_license_db(), int(time.time())
    )

    assert not result.ok
    assert result.error_code == "bad_signature"


def test_admin_http_route_is_absent():
    paths = {route.path for route in heartbeat.router.routes}
    assert "/api/admin/revoke" not in paths
