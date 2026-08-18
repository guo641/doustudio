"""License server signer backed by one encrypted local Ed25519 private key.

The private key is generated once by ``scripts/gen_server_key.py`` and is
encrypted at rest. ``KmsAdapter`` keeps its historical public API so the
heartbeat and verification modules do not need protocol-facing changes.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Optional

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import load_pem_private_key

from ..config import SERVER_KEY_PASSPHRASE

logger = logging.getLogger(__name__)


_SERVER_PRIVATE_PATH = (
    Path(__file__).resolve().parent.parent.parent
    / "scripts"
    / "server_signing_key.pem"
)


class KmsAdapter:
    """Load the encrypted server key and expose sign/public-key/verify methods."""

    def __init__(self) -> None:
        if not SERVER_KEY_PASSPHRASE:
            raise RuntimeError(
                "DOUSTUDIO_SERVER_KEY_PASSPHRASE 未配置,拒绝启动 license server"
            )
        if not _SERVER_PRIVATE_PATH.is_file():
            raise RuntimeError(
                f"服务端签名私钥不存在:{_SERVER_PRIVATE_PATH};"
                "请先运行 python scripts/gen_server_key.py"
            )

        try:
            loaded_key = load_pem_private_key(
                _SERVER_PRIVATE_PATH.read_bytes(),
                password=SERVER_KEY_PASSPHRASE.encode("utf-8"),
            )
        except (OSError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "服务端签名私钥解密失败,请检查 DOUSTUDIO_SERVER_KEY_PASSPHRASE"
            ) from exc

        if not isinstance(loaded_key, Ed25519PrivateKey):
            raise RuntimeError("服务端签名私钥不是 Ed25519 私钥,拒绝启动")

        self._local_priv = loaded_key
        self._local_pub = loaded_key.public_key()
        logger.info("已加载加密 Ed25519 服务端签名私钥:%s", _SERVER_PRIVATE_PATH)

    def public_key_bytes(self) -> bytes:
        """Return the raw 32-byte server public key."""
        return self._local_pub.public_bytes_raw()

    def sign(self, payload: bytes) -> bytes:
        """Sign payload and return a raw 64-byte Ed25519 signature."""
        return self._local_priv.sign(payload)

    def verify(
        self,
        public_key_32bytes: bytes,
        payload: bytes,
        signature_64bytes: bytes,
    ) -> bool:
        """Verify a raw Ed25519 signature with an external public key."""
        if len(public_key_32bytes) != 32 or len(signature_64bytes) != 64:
            return False
        try:
            pub = Ed25519PublicKey.from_public_bytes(public_key_32bytes)
            pub.verify(signature_64bytes, payload)
            return True
        except (InvalidSignature, ValueError):
            return False


def hmac_fingerprint_hex(public_key_32bytes: bytes, fingerprint_hex: str) -> str:
    """HMAC-SHA256(pubkey, fingerprint) as 64 lowercase hex characters."""
    import hmac

    return hmac.new(
        public_key_32bytes,
        fingerprint_hex.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def license_token_to_keys(
    license_token_blob: bytes,
) -> Optional[tuple[bytes, bytes, bytes]]:
    """Extract ``(private_seed, public_key, payload_json)`` from a v0.3.1 token."""
    if len(license_token_blob) < 129:
        return None
    priv_seed = license_token_blob[:32]
    pub = license_token_blob[32:64]
    payload_bytes = license_token_blob[64:-64]
    if not payload_bytes:
        return None
    return priv_seed, pub, payload_bytes


def license_token_to_pubkey(license_token_blob: bytes) -> Optional[bytes]:
    """Return only the public-key segment from a v0.3.1 license token."""
    keys = license_token_to_keys(license_token_blob)
    if keys is None:
        return None
    return keys[1]
