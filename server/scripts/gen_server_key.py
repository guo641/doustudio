#!/usr/bin/env python3
"""Generate the license server's encrypted Ed25519 signing key once.

The script deliberately refuses to overwrite either output.  Run it as the
non-root service account so the 0600 private key is readable by that account.
"""
from __future__ import annotations

import getpass
import os
import sys
import tempfile
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import (
    BestAvailableEncryption,
    Encoding,
    PrivateFormat,
    PublicFormat,
)


SCRIPT_DIR = Path(__file__).resolve().parent
PRIVATE_PATH = SCRIPT_DIR / "server_signing_key.pem"
PUBLIC_PATH = SCRIPT_DIR / "server_public.key"


def _passphrase() -> str:
    configured = os.environ.get("DOUSTUDIO_SERVER_KEY_PASSPHRASE")
    if configured is not None:
        if not configured:
            raise RuntimeError("DOUSTUDIO_SERVER_KEY_PASSPHRASE must not be empty")
        return configured

    first = getpass.getpass("Server key passphrase: ")
    second = getpass.getpass("Repeat server key passphrase: ")
    if not first:
        raise RuntimeError("server key passphrase must not be empty")
    if first != second:
        raise RuntimeError("server key passphrases do not match")
    return first


def _atomic_create(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Create *path* without replacing an existing file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".pem", dir=path.parent
    )
    temporary_path = Path(temporary_name)
    try:
        os.chmod(temporary_path, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            # A hard-link creation is atomic and fails instead of replacing a
            # file another process created after the initial existence check.
            os.link(temporary_path, path)
        except FileExistsError as exc:
            raise RuntimeError(f"refusing to overwrite existing {path}") from exc
        os.chmod(path, mode)
    finally:
        try:
            temporary_path.unlink()
        except FileNotFoundError:
            pass


def main() -> int:
    if PRIVATE_PATH.exists() or PUBLIC_PATH.exists():
        raise RuntimeError(
            f"refusing to overwrite existing key output ({PRIVATE_PATH} or {PUBLIC_PATH}); "
            "remove it explicitly only if rotation is intended"
        )

    old_umask = os.umask(0o177)
    try:
        passphrase = _passphrase()
        private_key = Ed25519PrivateKey.generate()
        private_pem = private_key.private_bytes(
            Encoding.PEM,
            PrivateFormat.PKCS8,
            BestAvailableEncryption(passphrase.encode("utf-8")),
        )
        public_bytes = private_key.public_key().public_bytes(
            Encoding.Raw, PublicFormat.Raw
        )
        public_text = (public_bytes.hex() + "\n").encode("ascii")

        _atomic_create(PRIVATE_PATH, private_pem)
        try:
            _atomic_create(PUBLIC_PATH, public_text)
        except Exception:
            # Do not leave a mismatched public/private pair after a failed
            # second write.  The private key is ignored by git and can be
            # removed explicitly before retrying generation.
            try:
                PRIVATE_PATH.unlink()
            except FileNotFoundError:
                pass
            raise
    finally:
        os.umask(old_umask)

    # Keep the machine-readable value on one line for Phase-2 pinning.
    print(f"server_private_key={PRIVATE_PATH}")
    print(f"server_public_key={PUBLIC_PATH}")
    print(f"server_public_key_hex={public_bytes.hex()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
