"""v0.3.1 license server: 客户端请求验签 + payload 验证。

握手协议:
  client → server:
    {
      license_token: bytes,
      fingerprint: <64hex>,
      client_pubkey: <32 bytes hex>,
      timestamp: int (unix seconds),
      client_sig: <64 bytes hex>,
      nonce: <16 bytes hex>
    }

  server 必须验证:
    1. license_token 里的 client_pubkey 跟请求里声明的一致
    2. timestamp 在合理范围(±60s)
    3. client_sig = Ed25519(private_key_matching_client_pubkey, fingerprint+timestamp+nonce)
       —— 注意 license_token blob 里前 32 bytes 就是 client 公钥
    4. fingerprint 跟某个已知 license 绑定(HMAC 匹配)
    5. 该 HMAC 不在 revoked 表里
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from .kms_adapter import KmsAdapter, hmac_fingerprint_hex, license_token_to_keys
from .. import config as _server_config
# 故意用 `from .. import config` 而不是 `from ..config import CLIENT_PUBKEY_HEX`,
# 这样测试可改 config.CLIENT_PUBKEY_HEX 实时生效(re-import 进 module 不更新
# 已 import 的具名常量)。

logger = logging.getLogger(__name__)


# 时间戳容忍窗口:客户端时钟漂移 + 网络延迟。±300s 覆盖大部分场景
TIMESTAMP_TOLERANCE_SEC = 300


@dataclass
class VerifyResult:
    """验签结果。"""

    ok: bool
    error_code: str = ""  # "bad_signature" | "expired" | "revoked" | "fingerprint_mismatch" | "bad_timestamp" | "unknown_license"
    fingerprint_hex: str = ""
    license_hmac: str = ""


def verify_heartbeat_request(
    payload: dict,
    db_conn,
    server_time: int,
) -> VerifyResult:
    """核心验签逻辑。

    v0.3.1 流程:
      1. 解析请求字段
      2. license_token blob → (priv_seed, pub, payload_bytes)
      3. pub 必须跟请求声明的 client_pubkey 一致
      4. payload_bytes 是 v0.3.0/v0.3.1 通用 JSON 格式
      5. 用 developer pubkey (CLIENT_PUBKEY_HEX) 验 payload_bytes 上的 sig
      6. 验 client_sig(client 用 priv 签 timestamp+nonce+fingerprint)
      7. 查 license_activations 表(没记录 = unknown_license,首次握手)
    """
    try:
        license_token_hex = payload["license_token"]
        fingerprint_hex = payload["fingerprint"]
        client_pubkey_hex = payload["client_pubkey"]
        client_timestamp = int(payload["timestamp"])
        client_sig_hex = payload["client_sig"]
        nonce_hex = payload["nonce"]
    except (KeyError, ValueError, TypeError) as exc:
        logger.warning("请求缺字段: %s", exc)
        return VerifyResult(ok=False, error_code="bad_signature")

    # 字段长度校验
    if len(fingerprint_hex) != 64:
        return VerifyResult(ok=False, error_code="fingerprint_mismatch")
    if len(client_pubkey_hex) != 64:
        return VerifyResult(ok=False, error_code="bad_signature")
    if len(client_sig_hex) != 128:
        return VerifyResult(ok=False, error_code="bad_signature")
    if len(nonce_hex) != 32:
        return VerifyResult(ok=False, error_code="bad_signature")

    try:
        license_token = bytes.fromhex(license_token_hex)
        client_pubkey = bytes.fromhex(client_pubkey_hex)
        client_sig = bytes.fromhex(client_sig_hex)
        nonce = bytes.fromhex(nonce_hex)
    except ValueError:
        return VerifyResult(ok=False, error_code="bad_signature")

    # 1. license_token blob → (priv_seed, pub, payload_bytes)
    #    payload_bytes 已是 developer 签过 sig 的 JSON(license_token_to_keys 已剥掉
    #    blob 末尾 64B sig + 头 64B metadata),所以下面直接拿它去验 dev sig。
    keys = license_token_to_keys(license_token)
    if keys is None:
        return VerifyResult(ok=False, error_code="bad_signature")
    priv_seed, token_pub, actual_payload_bytes = keys

    # 2. license_token 里的公钥跟声明的一致
    if token_pub != client_pubkey:
        return VerifyResult(ok=False, error_code="bad_signature")

    # 3. developer 用 priv_key 签的 sig 验 payload_bytes
    #    sig 在 blob 末尾 64B,已由 license_token_to_keys 剥出,这里重新读出
    if len(license_token) < 64 + 1 + 64:  # 至少 metadata + 1B JSON + 64B sig
        return VerifyResult(ok=False, error_code="bad_signature")
    developer_sig = license_token[-64:]
    developer_pubkey_hex = _server_config.CLIENT_PUBKEY_HEX.strip()
    if not developer_pubkey_hex or len(developer_pubkey_hex) != 64:
        if os.environ.get("DOUSTUDIO_ALLOW_UNSIGNED") == "1":
            logger.warning(
                "SECURITY WARNING: DOUSTUDIO_ALLOW_UNSIGNED=1,跳过 developer sig 验签;"
                "该模式仅允许本地 mock 使用"
            )
        else:
            logger.error(
                "DOUSTUDIO_CLIENT_PUBKEY_HEX 未配置或长度非法,拒绝心跳请求"
            )
            return VerifyResult(ok=False, error_code="bad_signature")
    else:
        try:
            dev_pub = Ed25519PublicKey.from_public_bytes(bytes.fromhex(developer_pubkey_hex))
            dev_pub.verify(developer_sig, actual_payload_bytes)
        except Exception as exc:
            logger.info("developer sig 验签失败: %s", exc)
            return VerifyResult(ok=False, error_code="bad_signature")

    # 4. payload 里的 fingerprint 跟请求里的 fingerprint 一致
    try:
        actual_payload = json.loads(actual_payload_bytes.decode("utf-8"))
    except Exception:
        return VerifyResult(ok=False, error_code="bad_signature")
    if actual_payload.get("fingerprint_hex", "").lower() != fingerprint_hex.lower():
        return VerifyResult(ok=False, error_code="fingerprint_mismatch")

    # 5. 时间戳窗口
    if abs(server_time - client_timestamp) > TIMESTAMP_TOLERANCE_SEC:
        logger.info(
            "时间戳超窗: client=%d server=%d diff=%d",
            client_timestamp, server_time, server_time - client_timestamp,
        )
        return VerifyResult(ok=False, error_code="bad_timestamp")

    # 6. 验签: payload = fingerprint + timestamp + nonce(client 用 priv 签)
    msg = fingerprint_hex.encode("ascii") + str(client_timestamp).encode("ascii") + nonce
    try:
        pub = Ed25519PublicKey.from_public_bytes(client_pubkey)
        pub.verify(client_sig, msg)
    except Exception:
        logger.info("客户端签名验证失败: fingerprint=%s...", fingerprint_hex[:16])
        return VerifyResult(ok=False, error_code="bad_signature")

    # 7. 计算 HMAC,查 license_activations 表
    license_hmac = hmac_fingerprint_hex(client_pubkey, fingerprint_hex)
    cursor = db_conn.execute(
        "SELECT expires_at FROM license_activations WHERE license_hmac = ?",
        (license_hmac,),
    )
    row = cursor.fetchone()
    if row is None:
        # 首次握手 → 自动 insert,放行
        logger.info("首次心跳,自动 insert license HMAC %s...", license_hmac[:16])
        # 注:首次 insert 在 heartbeat 端点处统一做,这里只标志
        return VerifyResult(
            ok=True,
            fingerprint_hex=fingerprint_hex,
            license_hmac=license_hmac,
        )

    # 8. 检查 license 本身是否过期(payload 里的 expires_at 字段)
    expires_at = row["expires_at"]
    if expires_at is not None and server_time > expires_at:
        return VerifyResult(ok=False, error_code="expired")

    # 9. 检查 revoked 表(前缀匹配)
    prefix = license_hmac[:_server_config.REVOKED_PREFIX_LEN]
    cursor = db_conn.execute(
        "SELECT 1 FROM revoked_license_hmac_prefixes WHERE prefix = ?",
        (prefix,),
    )
    if cursor.fetchone() is not None:
        logger.info("已 revoke: HMAC 前缀=%s", prefix)
        return VerifyResult(ok=False, error_code="revoked")

    return VerifyResult(
        ok=True,
        fingerprint_hex=fingerprint_hex,
        license_hmac=license_hmac,
    )
