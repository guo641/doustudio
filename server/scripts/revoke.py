"""v0.3.1: CLI 加黑名单。

用法:
    python scripts/revoke.py --license-token <hex> --fingerprint <64hex> --reason "用户退款"

生产环境通过 SSH 登录服务器后直接运行,不暴露 HTTP admin 端点。
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.crypto.kms_adapter import (  # noqa: E402
    hmac_fingerprint_hex,
    license_token_to_pubkey,
)
from app.storage.db import db_connection, init_db  # noqa: E402
from app.config import REVOKED_PREFIX_LEN  # noqa: E402
import time  # noqa: E402


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--license-token", required=True, help="hex 编码的 v0.3.1 license_token")
    p.add_argument("--fingerprint", required=True, help="用户的 64-hex fingerprint")
    p.add_argument("--reason", default="", help="撤销原因")
    args = p.parse_args()

    fingerprint = args.fingerprint.strip().lower()
    if len(fingerprint) != 64:
        print(f"❌ fingerprint 必须 64 hex chars,当前 {len(fingerprint)}", file=sys.stderr)
        return 1
    try:
        bytes.fromhex(fingerprint)
    except ValueError:
        print("❌ fingerprint 必须是合法 hex", file=sys.stderr)
        return 1

    init_db()

    try:
        license_token = bytes.fromhex(args.license_token)
    except ValueError:
        print("❌ license-token 必须是合法 hex", file=sys.stderr)
        return 1
    client_pubkey = license_token_to_pubkey(license_token)
    if client_pubkey is None:
        print("❌ license-token 长度或 v0.3.1 结构不合法", file=sys.stderr)
        return 1
    license_hmac = hmac_fingerprint_hex(client_pubkey, fingerprint)
    prefix = license_hmac[:REVOKED_PREFIX_LEN]

    with db_connection() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO revoked_license_hmac_prefixes (prefix, revoked_at, reason) VALUES (?, ?, ?)",
            (prefix, int(time.time()), args.reason),
        )

    print(f"✅ 已撤销 license HMAC 前缀: {prefix}")
    print(f"   完整 HMAC: {license_hmac}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
