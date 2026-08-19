"""
Embed the license server's Ed25519 public key into the client source.

The server key is a release input, not a secret.  It is split into a random
XOR mask and an encrypted byte string so the raw key is not present as one
recognizable literal in the client bundle.  ``heartbeat.py`` reconstructs it
at import time using the exact constant names emitted here.

Examples::

    python tools/license_keygen/scripts/embed_server_pubkey.py \
        --pubkey-hex b031bb728d70debf494a5da996b2d07c7e2286785a2da1207f2ef7ebf55f4a4e

    python tools/license_keygen/scripts/embed_server_pubkey.py \
        --public server/scripts/server_public.key
"""
from __future__ import annotations

import argparse
import os
import secrets
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PUBLIC_PATH = REPO_ROOT / "server" / "scripts" / "server_public.key"
DEFAULT_OUTPUT_PATH = REPO_ROOT / "src" / "doupool" / "license" / "_embedded_server_pubkey.py"


def _read_raw_public_key(path: Path) -> bytes:
    """Read an Ed25519 public key from raw/hex text or PEM."""
    data = path.read_bytes().strip()
    # The Phase-1 keygen writes the raw public key as one 64-character hex line.
    try:
        decoded = bytes.fromhex(data.decode("ascii"))
    except (UnicodeDecodeError, ValueError):
        from cryptography.hazmat.primitives.serialization import load_pem_public_key

        public_key = load_pem_public_key(data)
        decoded = public_key.public_bytes_raw()
    if len(decoded) != 32:
        raise SystemExit(f"server 公钥长度异常(期望 32 bytes,实际 {len(decoded)})")
    return decoded


def _read_pubkey_hex(value: str) -> bytes:
    try:
        decoded = bytes.fromhex(value.strip())
    except ValueError as exc:
        raise SystemExit(f"--pubkey-hex 不是合法 hex: {exc}") from exc
    if len(decoded) != 32:
        raise SystemExit(f"--pubkey-hex 长度异常(期望 64 hex chars,实际 {len(value.strip())})")
    return decoded


def _format_bytes(name: str, data: bytes) -> str:
    body = ", ".join(f"{byte:#04x}" for byte in data)
    return f"{name} = bytes([{body}])\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="XOR 编码 server Ed25519 公钥并嵌入客户端")
    source = parser.add_mutually_exclusive_group()
    source.add_argument(
        "--public",
        type=Path,
        default=DEFAULT_PUBLIC_PATH,
        help="Ed25519 PEM 或 64-character hex 公钥文件路径",
    )
    source.add_argument(
        "--pubkey-hex",
        help="直接提供 64-character raw public-key hex,跳过文件解析",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUTPUT_PATH,
        help="生成的 _embedded_server_pubkey.py 路径",
    )
    args = parser.parse_args()

    if args.pubkey_hex is not None:
        raw_pub = _read_pubkey_hex(args.pubkey_hex)
    else:
        if not args.public.exists():
            print(f"找不到 server 公钥文件: {args.public}", file=sys.stderr)
            return 1
        raw_pub = _read_raw_public_key(args.public)

    mask = secrets.token_bytes(32)
    encrypted = bytes(a ^ b for a, b in zip(raw_pub, mask))
    content = (
        '"""\n'
        "自动生成的 server Ed25519 公钥嵌入文件。\n\n"
        "不要手工编辑；请重新运行 embed_server_pubkey.py 生成。\n"
        "heartbeat.py 在 import 时按字节异或恢复原始公钥。\n"
        '"""\n\n'
        "# DO NOT EDIT — regenerate via tools/license_keygen/scripts/embed_server_pubkey.py\n"
        + _format_bytes("ENCRYPTED_SERVER_PUBKEY", encrypted)
        + _format_bytes("XOR_SERVER_MASK", mask)
    )

    output_path = args.out
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8", newline="\n") as output_file:
        output_file.write(content)
    os.chmod(output_path, 0o644)
    print(f"[embed] 写入 {output_path}")
    print("[embed] ENCRYPTED_SERVER_PUBKEY / XOR_SERVER_MASK 各 32 bytes")
    print(f"[embed] server 公钥 hex: {raw_pub.hex()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
