from __future__ import annotations

from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import (
    RSAPrivateKey,
    generate_private_key,
)
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    NoEncryption,
    PrivateFormat,
)

from server.app.crypto import kms_adapter


PASSWORD = "correct horse battery staple"


def _write_private(path: Path, key, password: str | None) -> None:
    protection = (
        BestAvailableEncryption(password.encode("utf-8"))
        if password is not None
        else NoEncryption()
    )
    path.write_bytes(key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, protection))


def _adapter(monkeypatch, path: Path, password: str):
    monkeypatch.setattr(kms_adapter, "_SERVER_PRIVATE_PATH", path)
    monkeypatch.setattr(kms_adapter, "SERVER_KEY_PASSPHRASE", password)
    return kms_adapter.KmsAdapter()


def test_missing_passphrase_fails_loud(monkeypatch, tmp_path):
    monkeypatch.setattr(kms_adapter, "_SERVER_PRIVATE_PATH", tmp_path / "key.pem")
    monkeypatch.setattr(kms_adapter, "SERVER_KEY_PASSPHRASE", "")

    with pytest.raises(RuntimeError, match="PASSPHRASE"):
        kms_adapter.KmsAdapter()


def test_missing_key_fails_loud(monkeypatch, tmp_path):
    monkeypatch.setattr(kms_adapter, "_SERVER_PRIVATE_PATH", tmp_path / "missing.pem")
    monkeypatch.setattr(kms_adapter, "SERVER_KEY_PASSPHRASE", PASSWORD)

    with pytest.raises(RuntimeError, match="私钥不存在"):
        kms_adapter.KmsAdapter()


def test_wrong_passphrase_fails_loud(monkeypatch, tmp_path):
    path = tmp_path / "key.pem"
    _write_private(path, Ed25519PrivateKey.generate(), PASSWORD)
    monkeypatch.setattr(kms_adapter, "_SERVER_PRIVATE_PATH", path)
    monkeypatch.setattr(kms_adapter, "SERVER_KEY_PASSPHRASE", "wrong")

    with pytest.raises(RuntimeError, match="解密失败"):
        kms_adapter.KmsAdapter()


def test_unencrypted_private_key_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / "key.pem"
    _write_private(path, Ed25519PrivateKey.generate(), None)

    with pytest.raises(RuntimeError, match="解密失败"):
        _adapter(monkeypatch, path, PASSWORD)


def test_non_ed25519_private_key_is_rejected(monkeypatch, tmp_path):
    path = tmp_path / "key.pem"
    rsa_key: RSAPrivateKey = generate_private_key(public_exponent=65537, key_size=2048)
    _write_private(path, rsa_key, PASSWORD)

    with pytest.raises(RuntimeError, match="不是 Ed25519"):
        _adapter(monkeypatch, path, PASSWORD)


def test_encrypted_ed25519_signer_roundtrip(monkeypatch, tmp_path):
    path = tmp_path / "key.pem"
    _write_private(path, Ed25519PrivateKey.generate(), PASSWORD)
    signer = _adapter(monkeypatch, path, PASSWORD)
    payload = b"heartbeat-response"
    signature = signer.sign(payload)

    assert len(signer.public_key_bytes()) == 32
    assert len(signature) == 64
    assert signer.verify(signer.public_key_bytes(), payload, signature)
    assert not signer.verify(signer.public_key_bytes(), b"tampered", signature)
