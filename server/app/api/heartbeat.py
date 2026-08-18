"""v0.3.1 license server: POST /api/heartbeat 端点。

完整协议:
  request:
    {
      license_token: <64 chars hex, 32 bytes>,
      fingerprint: <64 hex>,
      client_pubkey: <64 hex, 32 bytes>,
      timestamp: <int unix sec>,
      client_sig: <128 hex, 64 bytes>,
      nonce: <32 hex, 16 bytes>
    }

  response 200:
    {
      ok: true,
      fresh_until: <int unix sec>,
      server_timestamp: <int unix sec>,
      revoked_prefixes: [<8-char hex>, ...],
      server_sig: <128 hex, 64 bytes>
    }

  response 4xx:
    {
      ok: false,
      code: "bad_signature" | "expired" | "revoked" | "fingerprint_mismatch" | "bad_timestamp" | "unknown_license",
      server_timestamp: <int unix sec>
    }

为什么失败响应也要带 server_timestamp:
  - 客户端用这个校正本地时钟
  - 防客户端绕过 grace 改本地时间
"""
from __future__ import annotations

import json
import logging
import time
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..config import FRESH_DAYS, REVOKED_PREFIX_LEN
from ..crypto.kms_adapter import KmsAdapter
from ..crypto.verify import verify_heartbeat_request
from ..storage.db import db_connection, upsert_license_activation

logger = logging.getLogger(__name__)

router = APIRouter()


class HeartbeatRequest(BaseModel):
    # v0.3.1 license_token blob = [32 priv][32 pub][payload][64 sig] 至少 129 bytes
    # → hex 编码至少 258 chars,实际 ~300-700 chars 取决于 payload 大小
    license_token: str = Field(..., min_length=258, max_length=4096)
    fingerprint: str = Field(..., min_length=64, max_length=64)
    client_pubkey: str = Field(..., min_length=64, max_length=64)
    timestamp: int = Field(..., ge=0)
    client_sig: str = Field(..., min_length=128, max_length=128)
    nonce: str = Field(..., min_length=32, max_length=32)


class HeartbeatError(BaseModel):
    ok: bool = False
    code: str
    server_timestamp: int


class HeartbeatOk(BaseModel):
    ok: bool = True
    fresh_until: int
    server_timestamp: int
    revoked_prefixes: list[str]
    server_sig: str  # 128 hex
    # v0.3.1 协议:回显 client 发的 nonce + client_pubkey,防止攻击者替换成旧响应。
    # server_sig 已经签名了这两个字段(见下面 resp_payload_dict),所以回显 = 验签覆盖。
    nonce: str
    client_pubkey: str


def get_kms() -> KmsAdapter:
    """FastAPI dependency:每次请求拿一个 KmsAdapter 单例。"""
    # 简单做法:模块级 singleton。FastAPI Depends 链不需要复杂生命周期。
    from ..main import _kms_singleton
    return _kms_singleton


@router.post("/api/heartbeat", response_model=None)
def heartbeat(req: HeartbeatRequest, kms: Annotated[KmsAdapter, Depends(get_kms)]) -> dict:
    server_time = int(time.time())

    with db_connection() as conn:
        # 验签 + 状态检查
        result = verify_heartbeat_request(
            payload=req.model_dump(),
            db_conn=conn,
            server_time=server_time,
        )

        if not result.ok:
            return HeartbeatError(
                code=result.error_code,
                server_timestamp=server_time,
            ).model_dump()

        # 算 fresh_until
        fresh_until = server_time + FRESH_DAYS * 86400

        # 取 revoked 前缀列表(限制最多 10000 条,避免响应过大)
        cursor = conn.execute(
            "SELECT prefix FROM revoked_license_hmac_prefixes LIMIT 10000"
        )
        revoked_prefixes = [row["prefix"] for row in cursor.fetchall()]

        # upsert 该 license 的最后活跃时间
        upsert_license_activation(
            conn,
            license_hmac=result.license_hmac,
            fingerprint_hex=result.fingerprint_hex,
            expires_at=None,  # license 自身的 expires_at 在 verify 时已检查过
            server_time=server_time,
        )

    # 签响应
    resp_payload_dict = {
        "fresh_until": fresh_until,
        "server_timestamp": server_time,
        "revoked_prefixes": revoked_prefixes,
        "client_pubkey": req.client_pubkey,
        "nonce": req.nonce,
    }
    msg = json.dumps(resp_payload_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
    server_sig = kms.sign(msg)

    return HeartbeatOk(
        fresh_until=fresh_until,
        server_timestamp=server_time,
        revoked_prefixes=revoked_prefixes,
        server_sig=server_sig.hex(),
        nonce=req.nonce,
        client_pubkey=req.client_pubkey,
    ).model_dump()