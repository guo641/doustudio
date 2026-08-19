from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SERVER_PUBKEY_HEX = "b031bb728d70debf494a5da996b2d07c7e2286785a2da1207f2ef7ebf55f4a4e"


def test_embedded_server_pubkey_decodes_to_release_key():
    import importlib

    from doupool.license import _embedded_server_pubkey as embedded
    from doupool.license import heartbeat

    embedded = importlib.reload(embedded)
    heartbeat = importlib.reload(heartbeat)
    decoded = bytes(
        left ^ right
        for left, right in zip(
            embedded.ENCRYPTED_SERVER_PUBKEY,
            embedded.XOR_SERVER_MASK,
        )
    )
    assert len(embedded.ENCRYPTED_SERVER_PUBKEY) == 32
    assert len(embedded.XOR_SERVER_MASK) == 32
    assert decoded.hex() == SERVER_PUBKEY_HEX
    assert heartbeat._SERVER_PUBKEY == bytes.fromhex(SERVER_PUBKEY_HEX)


def test_embed_server_pubkey_script_accepts_direct_hex(tmp_path):
    output = tmp_path / "embedded.py"
    command = [
        sys.executable,
        str(REPO_ROOT / "tools/license_keygen/scripts/embed_server_pubkey.py"),
        "--pubkey-hex",
        SERVER_PUBKEY_HEX,
        "--out",
        str(output),
    ]
    completed = subprocess.run(command, check=True, capture_output=True, text=True)
    assert SERVER_PUBKEY_HEX in completed.stdout
    namespace: dict[str, object] = {}
    exec(output.read_text(encoding="utf-8"), namespace)
    decoded = bytes(
        left ^ right
        for left, right in zip(
            namespace["ENCRYPTED_SERVER_PUBKEY"],
            namespace["XOR_SERVER_MASK"],
        )
    )
    assert decoded == bytes.fromhex(SERVER_PUBKEY_HEX)


def _response_for_nonce(nonce: bytes, client_pubkey_hex: str = "11" * 32) -> dict:
    return {
        "server_timestamp": int(time.time()),
        "nonce": nonce.hex(),
        "server_sig": "00" * 64,
        "client_pubkey": client_pubkey_hex,
    }


def test_missing_server_pubkey_fails_closed(monkeypatch):
    from doupool.license import heartbeat

    monkeypatch.setattr(heartbeat, "_SERVER_PUBKEY", b"")
    monkeypatch.delenv("DOUSTUDIO_DEV_SKIP_SIG", raising=False)
    ok, error = heartbeat._verify_server_response(
        _response_for_nonce(b"n" * 16),
        expected_client_pubkey_hex="11" * 32,
        expected_nonce=b"n" * 16,
        client_local_time=int(time.time()),
    )
    assert not ok
    assert error == heartbeat.ErrCode.BAD_SIGNATURE


def test_missing_server_pubkey_skip_requires_explicit_dev_switch(monkeypatch):
    from doupool.license import heartbeat

    monkeypatch.setattr(heartbeat, "_SERVER_PUBKEY", b"")
    monkeypatch.setenv("DOUSTUDIO_DEV_SKIP_SIG", "1")
    ok, error = heartbeat._verify_server_response(
        _response_for_nonce(b"n" * 16),
        expected_client_pubkey_hex="11" * 32,
        expected_nonce=b"n" * 16,
        client_local_time=int(time.time()),
    )
    assert (ok, error) == (True, "")


def test_non_https_endpoint_is_rejected_without_dev_switch(monkeypatch):
    from doupool.license import heartbeat

    monkeypatch.delenv("DOUSTUDIO_DEV_ALLOW_HTTP", raising=False)
    response, error = heartbeat._do_http_post(
        "http://127.0.0.1:1/api/heartbeat",
        {},
        timeout_sec=1,
    )
    assert response is None
    assert error == heartbeat.ErrCode.NETWORK


def test_spki_pin_format_is_stable_for_embedded_release_pin():
    from doupool.license import heartbeat

    assert heartbeat.SERVER_SPKI_PIN == (
        "sha256/HnTrss/ACEvZ47WvSMdfdIvdhkwlwC2BUuw9m3LJ+4w="
    )


def test_persisted_base32_token_is_normalized_to_wire_hex():
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from doupool.license import heartbeat

    client = Ed25519PrivateKey.generate()
    wire = client.private_bytes_raw() + client.public_key().public_bytes_raw()
    wire += b"{}" + (b"s" * 64)
    persisted_text = base64.b32encode(wire).decode("ascii").rstrip("=")
    assert heartbeat._stored_token_hex_to_wire_hex(persisted_text.encode().hex()) == wire.hex()
    assert heartbeat._stored_token_hex_to_wire_hex(wire.hex()) == wire.hex()
