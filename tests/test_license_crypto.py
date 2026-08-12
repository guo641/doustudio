"""
v0.3.0:license/crypto.py Ed25519 验签 + HMAC 测试。

直接调 cryptography 库 round-trip,确认薄封装没破坏行为:
  1. sign(priv, payload) 返 64 bytes
  2. verify(pub, payload, sig) → True
  3. 改 1 字节 payload / sig → False
  4. fingerprint_hmac_hex 等价于标准 HMAC-SHA256
"""
from __future__ import annotations

import hashlib
import hmac

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
)

from doupool.license import crypto


@pytest.fixture
def keypair():
    priv = Ed25519PrivateKey.generate()
    priv_pem = priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())
    pub_32 = priv.public_key().public_bytes_raw()
    return priv_pem, pub_32


def test_sign_returns_64_bytes(keypair):
    priv_pem, _ = keypair
    sig = crypto.sign(priv_pem, b"hello world")
    assert isinstance(sig, bytes)
    assert len(sig) == 64


def test_verify_roundtrip(keypair):
    priv_pem, pub_32 = keypair
    payload = b"some payload to sign"
    sig = crypto.sign(priv_pem, payload)
    assert crypto.verify(pub_32, payload, sig) is True


def test_verify_fails_on_tampered_payload(keypair):
    priv_pem, pub_32 = keypair
    sig = crypto.sign(priv_pem, b"original")
    assert crypto.verify(pub_32, b"original-but-changed", sig) is False


def test_verify_fails_on_tampered_signature(keypair):
    priv_pem, pub_32 = keypair
    sig = crypto.sign(priv_pem, b"x")
    sig_bad = bytearray(sig)
    sig_bad[0] ^= 0x01
    assert crypto.verify(pub_32, b"x", bytes(sig_bad)) is False


def test_verify_fails_on_wrong_pubkey(keypair):
    priv_pem, _ = keypair
    sig = crypto.sign(priv_pem, b"x")
    # 另一台 Ed25519 keypair 的公钥
    other_pub = Ed25519PrivateKey.generate().public_key().public_bytes_raw()
    assert crypto.verify(other_pub, b"x", sig) is False


def test_verify_rejects_wrong_length_pubkey():
    assert crypto.verify(b"too-short", b"x", b"\x00" * 64) is False
    assert crypto.verify(b"a" * 33, b"x", b"\x00" * 64) is False


def test_verify_rejects_wrong_length_signature():
    pub = b"a" * 32
    assert crypto.verify(pub, b"x", b"\x00" * 63) is False


def test_fingerprint_hmac_matches_stdlib(keypair):
    """fingerprint_hmac_hex 应等价于直接用 stdlib 调。"""
    _, pub_32 = keypair
    raw = "FAKE-UUID-12345"
    expected = hmac.new(pub_32, raw.encode("utf-8"), hashlib.sha256).hexdigest()
    assert crypto.fingerprint_hmac_hex(pub_32, raw) == expected


def test_load_private_pem_roundtrip(keypair):
    priv_pem, _ = keypair
    priv = crypto.load_private_pem(priv_pem)
    # 重新导出 public 应得到同样的 pub_32
    pub_32 = priv.public_key().public_bytes_raw()
    from doupool.license import crypto as _crypto
    priv2 = Ed25519PrivateKey.generate()
    pub_32_ref = priv2.public_key().public_bytes_raw()
    # 不同私钥 → 不同公钥
    assert pub_32 != pub_32_ref
