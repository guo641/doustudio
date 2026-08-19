from __future__ import annotations

import struct
import time

import pytest


CLIENT_PRIV_SEED = b"p" * 32
CLIENT_PUBKEY = b"k" * 32
TOKEN_BLOB = CLIENT_PRIV_SEED + CLIENT_PUBKEY + b"t" * (129 - 64)
FINGERPRINT_HEX = "ab" * 32
PREFIX_A = "0123456789abcdef"
PREFIX_B = "fedcba9876543210"
TOKEN_EXTRA = {
    "wire_version": 2,
    "client_priv_seed": CLIENT_PRIV_SEED,
    "client_pubkey": CLIENT_PUBKEY,
}


@pytest.fixture
def isolated_license_dir(monkeypatch, tmp_path):
    """Keep every activated.bin read and write inside pytest's temp tree."""
    data_dir = tmp_path / "data"
    log_dir = tmp_path / "log"
    data_dir.mkdir()
    log_dir.mkdir()

    import doupool.config as config

    monkeypatch.setattr(config, "_resolve_app_dirs", lambda: (data_dir, log_dir))
    return data_dir / "license"


@pytest.fixture(autouse=True)
def reset_verifier_cache_between_tests():
    """A DSA2 status must not leak across isolated filesystem fixtures."""
    import doupool.license as license_api

    verifier = license_api._verifier
    if verifier is None:
        yield
        return
    original = dict(verifier._cached_status)
    verifier._cached_status.clear()
    verifier._cached_status.update({"status": "missing", "loaded": False})
    try:
        yield
    finally:
        verifier._cached_status.clear()
        verifier._cached_status.update(original)


def _write_v031(storage, *, fresh_until: int = 100, last_server_sync: int = 90):
    storage.write_token_v031(
        license_token_blob=TOKEN_BLOB,
        client_priv_seed=CLIENT_PRIV_SEED,
        client_pubkey=CLIENT_PUBKEY,
        fresh_until=fresh_until,
        clock_offset_ms=0,
        last_server_sync=last_server_sync,
    )


def _require_rebuilt_verifier(license_api):
    if not license_api.is_compiled():
        pytest.skip("verifier.pyd 未编译")
    verifier = license_api._verifier
    if not hasattr(verifier, "mark_activation_revoked"):
        pytest.skip("verifier.pyx 已修改但 .pyd 尚未重建")
    return verifier


def test_dsa2_roundtrip_uses_eight_byte_prefixes_and_signed_clock_offset(
    isolated_license_dir,
):
    from doupool.license import storage

    storage.write_token_v032(
        license_token_blob=TOKEN_BLOB,
        client_priv_seed=CLIENT_PRIV_SEED,
        client_pubkey=CLIENT_PUBKEY,
        fresh_until=2_000_000_000,
        clock_offset_ms=-1250,
        last_server_sync=1_999_999_999,
        revoked_prefixes=(PREFIX_A, PREFIX_B),
    )

    stored = storage.read_token_v032()
    assert stored is not None
    assert stored.license_token_blob == TOKEN_BLOB
    assert stored.client_priv_seed == CLIENT_PRIV_SEED
    assert stored.client_pubkey == CLIENT_PUBKEY
    assert stored.clock_offset_ms == -1250
    assert stored.revoked_prefixes == (PREFIX_A, PREFIX_B)

    raw = storage.read_token()
    assert raw is not None
    assert raw[:4] == b"DSA2"
    assert len(raw) == 88 + (2 * 8) + len(TOKEN_BLOB)
    assert raw[88:96] == bytes.fromhex(PREFIX_A)
    assert raw[96:104] == bytes.fromhex(PREFIX_B)


def test_dsa1_upgrade_to_dsa2_preserves_token_and_client_keys(
    isolated_license_dir,
):
    from doupool.license import storage

    _write_v031(storage)
    storage.update_heartbeat_fields_v032(
        fresh_until=300,
        clock_offset_ms=-500,
        last_server_sync=200,
        revoked_prefixes=(PREFIX_A,),
    )

    assert storage.read_token_v031() is None
    stored = storage.read_token_v032()
    assert stored is not None
    assert stored.license_token_blob == TOKEN_BLOB
    assert stored.client_priv_seed == CLIENT_PRIV_SEED
    assert stored.client_pubkey == CLIENT_PUBKEY
    assert stored.fresh_until == 300
    assert stored.clock_offset_ms == -500
    assert stored.last_server_sync == 200
    assert stored.revoked_prefixes == (PREFIX_A,)
    assert storage.read_token_legacy() is None


def test_license_hmac_prefix_matches_server_protocol_helper(isolated_license_dir):
    from doupool.license import storage
    from server.app.crypto.kms_adapter import hmac_fingerprint_hex

    expected = hmac_fingerprint_hex(CLIENT_PUBKEY, FINGERPRINT_HEX)[:16]

    actual = storage.license_hmac_prefix(CLIENT_PUBKEY, FINGERPRINT_HEX)
    assert actual == expected
    assert len(actual) == 16
    assert bytes.fromhex(actual).hex() == actual


def test_mark_revoked_keeps_own_prefix_at_ten_thousand_entry_limit(
    isolated_license_dir,
):
    from doupool.license import storage

    own_prefix = storage.license_hmac_prefix(CLIENT_PUBKEY, FINGERPRINT_HEX)
    server_prefixes = tuple(
        prefix
        for i in range(10_001)
        if (prefix := f"{i:016x}") != own_prefix
    )[: storage.MAX_REVOKED_PREFIXES]
    storage.write_token_v032(
        license_token_blob=TOKEN_BLOB,
        client_priv_seed=CLIENT_PRIV_SEED,
        client_pubkey=CLIENT_PUBKEY,
        revoked_prefixes=server_prefixes,
    )

    assert storage.mark_current_license_revoked(
        fingerprint_hex=FINGERPRINT_HEX
    ) == own_prefix

    stored = storage.read_token_v032()
    assert stored is not None
    assert len(stored.revoked_prefixes) == storage.MAX_REVOKED_PREFIXES
    assert stored.revoked_prefixes[:-1] == server_prefixes[:-1]
    assert stored.revoked_prefixes[-1] == own_prefix


def test_storage_refuses_to_rewrite_mismatched_header_keys(isolated_license_dir):
    from doupool.license import storage

    storage.write_token_v032(
        license_token_blob=TOKEN_BLOB,
        client_priv_seed=CLIENT_PRIV_SEED,
        client_pubkey=b"z" * 32,
    )

    with pytest.raises(RuntimeError, match="header client_pubkey"):
        storage.update_heartbeat_fields_v032(
            fresh_until=100,
            clock_offset_ms=0,
            last_server_sync=90,
            revoked_prefixes=(),
        )
    with pytest.raises(RuntimeError, match="header client_pubkey"):
        storage.mark_current_license_revoked(fingerprint_hex=FINGERPRINT_HEX)


def test_compiled_verifier_reports_matching_dsa2_prefix_as_revoked(
    isolated_license_dir, monkeypatch
):
    import doupool.license as license_api
    from doupool.license import storage

    if not license_api.is_compiled():
        pytest.skip("verifier.pyd 未编译")

    verifier = license_api._verifier
    own_prefix = storage.license_hmac_prefix(CLIENT_PUBKEY, FINGERPRINT_HEX)
    now = int(time.time())
    storage.write_token_v032(
        license_token_blob=TOKEN_BLOB,
        client_priv_seed=CLIENT_PRIV_SEED,
        client_pubkey=CLIENT_PUBKEY,
        fresh_until=now + 86_400,
        last_server_sync=now,
        revoked_prefixes=(own_prefix,),
    )
    payload = {
        "fingerprint_hex": FINGERPRINT_HEX,
        "customer": "revoked-customer",
        "issued_at": now - 60,
        "expires_at": now + 86_400,
    }
    original_cache = dict(verifier._cached_status)
    monkeypatch.setattr(
        verifier,
        "verify_token",
        lambda _blob: (True, payload, "", TOKEN_EXTRA),
    )
    monkeypatch.setattr(verifier, "current_fingerprint", lambda: FINGERPRINT_HEX)
    try:
        assert license_api.reload_activation_status() == "revoked"
        assert license_api.get_activation_status() == "revoked"
    finally:
        verifier._cached_status.clear()
        verifier._cached_status.update(original_cache)


def test_verifier_gate_uses_exit_73_for_revoked(
    isolated_license_dir, monkeypatch
):
    import doupool.license as license_api
    from doupool.license import storage

    if not license_api.is_compiled():
        pytest.skip("verifier.pyd 未编译")
    verifier = license_api._verifier
    own_prefix = storage.license_hmac_prefix(CLIENT_PUBKEY, FINGERPRINT_HEX)
    now = int(time.time())
    storage.write_token_v032(
        license_token_blob=TOKEN_BLOB,
        client_priv_seed=CLIENT_PRIV_SEED,
        client_pubkey=CLIENT_PUBKEY,
        fresh_until=now + 86_400,
        last_server_sync=now,
        revoked_prefixes=(own_prefix,),
    )
    payload = {
        "fingerprint_hex": FINGERPRINT_HEX,
        "customer": "revoked-customer",
        "issued_at": now - 60,
        "expires_at": now + 86_400,
    }
    original_cache = dict(verifier._cached_status)
    monkeypatch.setattr(
        verifier,
        "verify_token",
        lambda _blob: (True, payload, "", TOKEN_EXTRA),
    )
    monkeypatch.setattr(verifier, "current_fingerprint", lambda: FINGERPRINT_HEX)
    try:
        verifier.reload_activation_status()
        with pytest.raises(SystemExit) as exc_info:
            verifier.ensure_activated_or_exit()
        assert exc_info.value.code == 73
    finally:
        verifier._cached_status.clear()
        verifier._cached_status.update(original_cache)


def test_new_v2_activation_is_persisted_as_dsa2(isolated_license_dir, monkeypatch):
    import doupool.license as license_api
    from doupool.license import storage

    if not license_api.is_compiled():
        pytest.skip("verifier.pyd 未编译")

    verifier = license_api._verifier
    payload = {
        "fingerprint_hex": FINGERPRINT_HEX,
        "customer": "new-customer",
        "issued_at": int(time.time()),
        "expires_at": int(time.time()) + 86_400,
    }
    extra = {
        "wire_version": 2,
        "client_priv_seed": CLIENT_PRIV_SEED,
        "client_pubkey": CLIENT_PUBKEY,
    }
    original_cache = dict(verifier._cached_status)
    monkeypatch.setattr(
        verifier,
        "verify_token",
        lambda _blob: (True, payload, "", extra),
    )
    try:
        success, error = verifier.activate("A" * 129)
        assert success, error
        assert storage.read_token_v031() is None
        stored = storage.read_token_v032()
        assert stored is not None
        assert stored.client_priv_seed == CLIENT_PRIV_SEED
        assert stored.client_pubkey == CLIENT_PUBKEY
        assert stored.revoked_prefixes == ()
    finally:
        verifier._cached_status.clear()
        verifier._cached_status.update(original_cache)


def test_reactivating_revoked_token_is_rejected_and_snapshot_is_preserved(
    isolated_license_dir, monkeypatch
):
    import doupool.license as license_api
    from doupool.license import storage

    verifier = _require_rebuilt_verifier(license_api)
    now = int(time.time())
    own_prefix = storage.license_hmac_prefix(CLIENT_PUBKEY, FINGERPRINT_HEX)
    storage.write_token_v032(
        license_token_blob=TOKEN_BLOB,
        client_priv_seed=CLIENT_PRIV_SEED,
        client_pubkey=CLIENT_PUBKEY,
        fresh_until=now + 86_400,
        last_server_sync=now,
        revoked_prefixes=(own_prefix,),
    )
    payload = {
        "fingerprint_hex": FINGERPRINT_HEX,
        "customer": "revoked-customer",
        "issued_at": now - 60,
        "expires_at": now + 86_400,
    }
    monkeypatch.setattr(
        verifier,
        "verify_token",
        lambda _blob: (True, payload, "", TOKEN_EXTRA),
    )
    monkeypatch.setattr(verifier, "current_fingerprint", lambda: FINGERPRINT_HEX)

    assert verifier.reload_activation_status() == "revoked"
    success, error = verifier.activate("A" * 129)

    assert success is False
    assert "已被撤销" in error
    assert verifier.get_activation_status() == "revoked"
    stored = storage.read_token_v032()
    assert stored is not None
    assert stored.license_token_blob == TOKEN_BLOB
    assert stored.revoked_prefixes == (own_prefix,)


def test_new_client_key_activation_preserves_old_revoked_snapshot(
    isolated_license_dir, monkeypatch
):
    import doupool.license as license_api
    from doupool.license import storage

    verifier = _require_rebuilt_verifier(license_api)
    now = int(time.time())
    old_prefix = storage.license_hmac_prefix(CLIENT_PUBKEY, FINGERPRINT_HEX)
    storage.write_token_v032(
        license_token_blob=TOKEN_BLOB,
        client_priv_seed=CLIENT_PRIV_SEED,
        client_pubkey=CLIENT_PUBKEY,
        fresh_until=now + 86_400,
        last_server_sync=now,
        revoked_prefixes=(old_prefix,),
    )
    new_priv = b"q" * 32
    new_pub = b"r" * 32
    new_extra = {
        "wire_version": 2,
        "client_priv_seed": new_priv,
        "client_pubkey": new_pub,
    }
    payload = {
        "fingerprint_hex": FINGERPRINT_HEX,
        "customer": "replacement-customer",
        "issued_at": now,
        "expires_at": now + 86_400,
    }
    monkeypatch.setattr(
        verifier,
        "verify_token",
        lambda _blob: (True, payload, "", new_extra),
    )
    monkeypatch.setattr(verifier, "current_fingerprint", lambda: FINGERPRINT_HEX)

    success, error = verifier.activate("B" * 129)

    assert success, error
    assert verifier.get_activation_status() == "valid"
    stored = storage.read_token_v032()
    assert stored is not None
    assert stored.client_priv_seed == new_priv
    assert stored.client_pubkey == new_pub
    assert stored.revoked_prefixes == (old_prefix,)


@pytest.mark.parametrize("schema", ["DSA1", "DSA2"])
def test_verifier_rejects_dsa_header_key_mismatch(
    isolated_license_dir, monkeypatch, schema
):
    import doupool.license as license_api
    from doupool.license import storage

    verifier = _require_rebuilt_verifier(license_api)
    now = int(time.time())
    writer = storage.write_token_v031 if schema == "DSA1" else storage.write_token_v032
    writer(
        license_token_blob=TOKEN_BLOB,
        client_priv_seed=CLIENT_PRIV_SEED,
        client_pubkey=b"z" * 32,
        fresh_until=now + 86_400,
        last_server_sync=now,
    )
    payload = {
        "fingerprint_hex": FINGERPRINT_HEX,
        "customer": "tampered-header",
        "issued_at": now - 60,
        "expires_at": now + 86_400,
    }
    monkeypatch.setattr(
        verifier,
        "verify_token",
        lambda _blob: (True, payload, "", TOKEN_EXTRA),
    )

    assert verifier.reload_activation_status() == "expired"
    assert verifier.get_activation_detail()["error"] == "storage_key_mismatch"


def test_bootstrap_success_persists_revocation_list_and_reloads_status(
    isolated_license_dir, monkeypatch
):
    import doupool.license as license_api
    from doupool.license import bootstrap, heartbeat, storage

    _write_v031(storage)
    reload_calls: list[bool] = []
    monkeypatch.setattr(license_api, "get_activation_status", lambda: "valid")
    monkeypatch.setattr(license_api, "current_fingerprint", lambda: FINGERPRINT_HEX)
    monkeypatch.setattr(
        license_api,
        "reload_activation_status",
        lambda: reload_calls.append(True) or "valid",
    )
    monkeypatch.setattr(
        heartbeat,
        "perform_handshake",
        lambda **_kwargs: heartbeat.HeartbeatResult(
            ok=True,
            fresh_until=500,
            server_timestamp=400,
            clock_offset_ms=-1000,
            revoked_prefixes=(PREFIX_A, PREFIX_B),
        ),
    )

    bootstrap.run_startup_handshake()

    stored = storage.read_token_v032()
    assert stored is not None
    assert stored.revoked_prefixes == (PREFIX_A, PREFIX_B)
    assert stored.fresh_until == 500
    assert stored.last_server_sync == 400
    assert reload_calls == [True]


def test_bootstrap_revoked_error_persists_local_prefix_and_reloads_status(
    isolated_license_dir, monkeypatch
):
    import doupool.license as license_api
    from doupool.license import bootstrap, heartbeat, storage

    _write_v031(storage, fresh_until=500, last_server_sync=400)
    own_prefix = storage.license_hmac_prefix(CLIENT_PUBKEY, FINGERPRINT_HEX)
    reload_calls: list[bool] = []
    monkeypatch.setattr(license_api, "get_activation_status", lambda: "valid")
    monkeypatch.setattr(license_api, "current_fingerprint", lambda: FINGERPRINT_HEX)
    monkeypatch.setattr(
        license_api,
        "reload_activation_status",
        lambda: reload_calls.append(True) or "revoked",
    )
    monkeypatch.setattr(
        heartbeat,
        "perform_handshake",
        lambda **_kwargs: heartbeat.HeartbeatResult(
            ok=False,
            error_code=heartbeat.ErrCode.REVOKED,
            server_timestamp=450,
        ),
    )

    bootstrap.run_startup_handshake()

    stored = storage.read_token_v032()
    assert stored is not None
    assert stored.license_token_blob == TOKEN_BLOB
    assert stored.fresh_until == 500
    assert stored.revoked_prefixes == (own_prefix,)
    assert reload_calls == [True]


def test_bootstrap_marks_process_revoked_before_persistence(
    isolated_license_dir, monkeypatch
):
    import doupool.license as license_api
    from doupool.license import bootstrap, heartbeat, storage

    _write_v031(storage, fresh_until=500, last_server_sync=400)
    verifier = license_api._verifier
    if verifier is None:
        pytest.skip("verifier.pyd 未编译")
    observed: list[str] = []
    monkeypatch.setattr(license_api, "get_activation_status", lambda: "valid")
    monkeypatch.setattr(license_api, "current_fingerprint", lambda: FINGERPRINT_HEX)
    monkeypatch.setattr(
        heartbeat,
        "perform_handshake",
        lambda **_kwargs: heartbeat.HeartbeatResult(
            ok=False,
            error_code=heartbeat.ErrCode.REVOKED,
        ),
    )

    def fail_persistence(**_kwargs):
        observed.append(verifier._cached_status.get("status"))
        raise OSError("simulated disk failure")

    monkeypatch.setattr(storage, "mark_current_license_revoked", fail_persistence)

    bootstrap.run_startup_handshake()

    assert observed == ["revoked"]
    assert verifier._cached_status["status"] == "revoked"


def test_daemon_reads_dsa2_and_updates_revocation_list(
    isolated_license_dir, monkeypatch
):
    from doupool.license import heartbeat, heartbeat_daemon, storage

    storage.write_token_v032(
        license_token_blob=TOKEN_BLOB,
        client_priv_seed=CLIENT_PRIV_SEED,
        client_pubkey=CLIENT_PUBKEY,
        fresh_until=100,
        clock_offset_ms=0,
        last_server_sync=90,
        revoked_prefixes=(PREFIX_A,),
    )
    captured: dict[str, object] = {}

    def fake_handshake(**kwargs):
        captured.update(kwargs)
        return heartbeat.HeartbeatResult(
            ok=True,
            fresh_until=700,
            server_timestamp=600,
            clock_offset_ms=250,
            revoked_prefixes=(PREFIX_B,),
        )

    monkeypatch.setattr(heartbeat_daemon, "_current_fingerprint_hex", lambda: FINGERPRINT_HEX)
    monkeypatch.setattr(heartbeat_daemon._hb, "perform_handshake", fake_handshake)

    heartbeat_daemon._run_one_heartbeat()

    assert captured["license_token_hex"] == TOKEN_BLOB.hex()
    assert captured["client_priv_seed"] == CLIENT_PRIV_SEED
    stored = storage.read_token_v032()
    assert stored is not None
    assert stored.fresh_until == 700
    assert stored.last_server_sync == 600
    assert stored.revoked_prefixes == (PREFIX_B,)


def test_daemon_marks_process_revoked_before_persistence(
    isolated_license_dir, monkeypatch
):
    import doupool.license as license_api
    from doupool.license import heartbeat, heartbeat_daemon, storage

    _write_v031(storage, fresh_until=500, last_server_sync=400)
    verifier = license_api._verifier
    if verifier is None:
        pytest.skip("verifier.pyd 未编译")
    observed: list[str] = []
    original_mark = storage.mark_current_license_revoked
    monkeypatch.setattr(
        heartbeat_daemon,
        "_current_fingerprint_hex",
        lambda: FINGERPRINT_HEX,
    )
    monkeypatch.setattr(
        heartbeat_daemon._hb,
        "perform_handshake",
        lambda **_kwargs: heartbeat.HeartbeatResult(
            ok=False,
            error_code=heartbeat.ErrCode.REVOKED,
        ),
    )
    monkeypatch.setattr(license_api, "reload_activation_status", lambda: "revoked")

    def tracked_persistence(**kwargs):
        observed.append(verifier._cached_status.get("status"))
        return original_mark(**kwargs)

    monkeypatch.setattr(storage, "mark_current_license_revoked", tracked_persistence)

    heartbeat_daemon._run_one_heartbeat()

    assert observed == ["revoked"]
    stored = storage.read_token_v032()
    assert stored is not None
    own_prefix = storage.license_hmac_prefix(CLIENT_PUBKEY, FINGERPRINT_HEX)
    assert stored.revoked_prefixes == (own_prefix,)


@pytest.mark.parametrize(
    ("license_status", "expected_error"),
    [
        ("revoked", "license_revoked"),
        ("uncompiled", "license_uncompiled"),
    ],
)
def test_protected_api_rejects_non_valid_license(
    monkeypatch, tmp_path, license_status, expected_error
):
    from fastapi.testclient import TestClient

    import doupool.license as license_api
    from doupool.api.app import create_app

    monkeypatch.setattr(
        license_api,
        "get_activation_status",
        lambda: license_status,
    )
    app = create_app(
        token="secret",
        frontend_dir=tmp_path / "missing",
        repository=None,
        login_service=None,
        current_version="0.3.12",
    )
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.get(
            "/api/accounts",
            headers={"X-DouPool-Token": "secret"},
        )
        sse_response = client.get(
            "/api/login-attempts/whatever/events?access_token=secret",
        )

    assert response.status_code == 403
    assert response.json()["detail"] == {"error": expected_error}
    assert sse_response.status_code == 403
    assert sse_response.json()["detail"] == {"error": expected_error}


@pytest.mark.parametrize(
    ("license_status", "expected_calls"),
    [
        ("valid", ["resume", "cron"]),
        ("revoked", []),
        ("uncompiled", []),
    ],
)
def test_lifespan_starts_video_runtime_only_for_valid_license(
    monkeypatch, tmp_path, license_status, expected_calls
):
    from fastapi.testclient import TestClient

    import doupool.license as license_api
    from doupool.api.app import create_app

    calls: list[str] = []

    class TrackedVideoService:
        async def resume_queued(self):
            calls.append("resume")

        def start_reset_cron(self):
            calls.append("cron")

        async def shutdown(self):
            calls.append("shutdown")

    monkeypatch.setattr(
        license_api,
        "get_activation_status",
        lambda: license_status,
    )
    app = create_app(
        token="secret",
        frontend_dir=tmp_path / "missing",
        repository=None,
        login_service=None,
        video_service=TrackedVideoService(),
        current_version="0.3.12",
    )

    with TestClient(app):
        assert calls == expected_calls

    assert calls == [*expected_calls, "shutdown"]


def test_license_activation_runs_handshake_and_starts_runtime_once(
    monkeypatch, tmp_path
):
    """Post-start activation performs one handshake and starts workers once."""
    from fastapi.testclient import TestClient

    import doupool.license as license_api
    from doupool.api.app import create_app
    from doupool.license import bootstrap

    calls: list[str] = []
    status = {"value": "missing"}

    class TrackedVideoService:
        async def resume_queued(self):
            calls.append("resume")

        def start_reset_cron(self):
            calls.append("cron")

        async def shutdown(self):
            calls.append("shutdown")

    monkeypatch.setattr(license_api, "get_activation_status", lambda: status["value"])
    monkeypatch.setattr(license_api, "activate", lambda _code: (True, ""))

    def fake_handshake():
        calls.append("handshake")
        # A network failure would leave this value as valid in the real
        # bootstrap path, preserving the existing grace-period semantics.
        status["value"] = "valid"

    monkeypatch.setattr(bootstrap, "run_startup_handshake", fake_handshake)
    app = create_app(
        token="secret",
        frontend_dir=tmp_path / "missing",
        repository=None,
        login_service=None,
        video_service=TrackedVideoService(),
        current_version="0.3.12",
    )
    with TestClient(app) as client:
        response = client.post("/api/license/activate", json={"code": "new-code"})
        assert response.status_code == 200
        assert calls == ["handshake", "resume", "cron"]

        # A repeated successful activation must not restart worker/cron.
        response = client.post("/api/license/activate", json={"code": "new-code"})
        assert response.status_code == 200
        assert calls == ["handshake", "resume", "cron", "handshake"]

    assert calls[-1] == "shutdown"


def test_license_activation_rejects_when_handshake_confirms_revoked(
    monkeypatch, tmp_path
):
    from fastapi.testclient import TestClient

    import doupool.license as license_api
    from doupool.api.app import create_app
    from doupool.license import bootstrap

    status = {"value": "missing"}
    monkeypatch.setattr(license_api, "get_activation_status", lambda: status["value"])
    monkeypatch.setattr(license_api, "activate", lambda _code: (True, ""))

    def fake_handshake():
        status["value"] = "revoked"

    monkeypatch.setattr(bootstrap, "run_startup_handshake", fake_handshake)
    app = create_app(
        token="secret",
        frontend_dir=tmp_path / "missing",
        repository=None,
        login_service=None,
        current_version="0.3.12",
    )
    with TestClient(app) as client:
        response = client.post("/api/license/activate", json={"code": "same-code"})

    assert response.status_code == 403
    assert response.json()["detail"] == {"error": "license_revoked"}


def test_importing_main_does_not_run_startup_handshake(monkeypatch):
    """Test discovery may import constants from main; imports must stay read-only."""
    import importlib

    from doupool.license import bootstrap, heartbeat_daemon

    calls: list[str] = []
    monkeypatch.setattr(
        bootstrap,
        "run_startup_handshake",
        lambda: calls.append("handshake"),
    )
    monkeypatch.setattr(heartbeat_daemon, "start", lambda: calls.append("daemon"))

    import doupool.main as app_main

    importlib.reload(app_main)

    assert calls == []


@pytest.mark.parametrize("revoked_count", [2, 10_001, 0xFFFFFFFF])
def test_dsa2_corrupt_or_oversized_prefix_count_fails_closed(
    isolated_license_dir, revoked_count
):
    from doupool.license import storage
    from doupool.license.storage_path import activated_bin_path

    storage.write_token_v032(
        license_token_blob=TOKEN_BLOB,
        client_priv_seed=CLIENT_PRIV_SEED,
        client_pubkey=CLIENT_PUBKEY,
        revoked_prefixes=(PREFIX_A,),
    )
    raw = bytearray(storage.read_token())
    raw[84:88] = struct.pack("<I", revoked_count)
    activated_bin_path().write_bytes(raw)

    assert storage.read_token_v032() is None
    assert storage.read_token_legacy() is None
