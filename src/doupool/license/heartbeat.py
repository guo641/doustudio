"""
v0.3.1:半在线心跳 — 客户端启动同步握手 + 后台 daemon 续期。

启动握手(同步,在主窗口渲染前调一次):
    POST https://license.<domain>/api/heartbeat
    {
        license_token(完整 v0.3.1 blob), fingerprint, client_pubkey,
        timestamp, client_sig, nonce
    }
    → 200 {ok, fresh_until, server_timestamp, revoked_prefixes, server_sig}
    → 4xx/5xx/timeout {ok: false, code, server_timestamp}

成功: 调用方写 activated.bin 的 fresh_until + clock_offset_ms → 用户进主 UI
失败: 不阻塞,仅记 log + 标记 grace 状态(verifier.pyx 的 grace 7d 决定后续能否用)

后台 daemon(每 24h 一次,daemon 线程):
    同上协议,异步跑,失败重试 3 次后 offline 标记。

**为什么 client 也要验 server_sig**:
    攻击者自建 fake server(无 server 私钥)能返 {ok:true, fresh_until: now+30y},
    客户端不验签就直接写入 activated.bin → 永久破解。Ed25519 验签 + clock skew
    ±300s 检查 + nonce 一次性 → 攻击者即使 dump 真实 server 响应也不能重放。

**为什么 handshake 不阻塞主窗口**:
    网络抽风不能影响用户进入主 UI,verifier.pyx 的 grace 7d 兜底。7 天不联
    才拒绝。daemon 后台线程也用同样的 grace。
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)

from . import _embedded_server_pubkey as _server_pk_module

logger = logging.getLogger(__name__)


# 常量(跟 server/app/config.py 对齐,集中维护)
DEFAULT_SERVER_URL: str = os.environ.get(
    "DOUSTUDIO_LICENSE_SERVER_URL",
    "https://license.example.com",  # 占位,生产部署时改成真域名
)
HANDSHAKE_TIMEOUT_SEC: int = 5  # 启动握手不能拖过 5s
NONCE_LEN: int = 16
CLOCK_SKEW_TOLERANCE_SEC: int = 300  # ±5 分钟


# 失败 error_code 集合
class ErrCode:
    NETWORK = "network_error"
    TIMEOUT = "timeout"
    BAD_RESPONSE = "bad_response"
    BAD_SIGNATURE = "bad_signature"
    REVOKED = "revoked"
    EXPIRED = "expired"
    UNKNOWN_LICENSE = "unknown_license"
    BAD_TIMESTAMP = "bad_timestamp"
    SERVER_DISABLED = "server_disabled"  # 开发期 server 端空实现


@dataclass
class HeartbeatResult:
    """握手结果。"""

    ok: bool
    error_code: str = ""
    fresh_until: int = 0  # 0 表示未拿到
    server_timestamp: int = 0
    clock_offset_ms: int = 0
    revoked_prefixes: tuple = ()  # 元组而非 list,方便 immutable 缓存


def _decode_server_pubkey() -> bytes:
    """XOR 解码 server 公钥(没配 → b"" → 跳过验签)。"""
    enc = _server_pk_module.ENCRYPTED_SERVER_PUBKEY
    mask = _server_pk_module.XOR_SERVER_MASK
    if len(enc) != 32 or len(mask) != 32:
        return b""
    decoded = bytes(a ^ b for a, b in zip(enc, mask))
    if decoded == b"\x00" * 32:
        return b""
    return decoded


_SERVER_PUBKEY: bytes = _decode_server_pubkey()


def server_pubkey_configured() -> bool:
    """是否嵌入了 server 公钥(没配 → 跳过 server_sig 校验,仅开发用)。"""
    return bool(_SERVER_PUBKEY)


def _sign_client_request(
    *,
    client_priv_seed: bytes,
    fingerprint_hex: str,
    timestamp: int,
    nonce: bytes,
) -> bytes:
    """客户端签名:Ed25519(priv, fingerprint + timestamp + nonce) → 64 bytes。

    client_priv_seed 是 32 字节 Ed25519 seed(从 activated.bin 读出,
    跟 server 端用 pub 验签互为逆运算)。
    """
    priv = Ed25519PrivateKey.from_private_bytes(client_priv_seed)
    msg = (
        fingerprint_hex.encode("ascii")
        + str(timestamp).encode("ascii")
        + nonce
    )
    return priv.sign(msg)


def _verify_server_response(
    response: dict,
    *,
    expected_client_pubkey_hex: str,
    expected_nonce: bytes,
    client_local_time: int,
) -> Tuple[bool, str]:
    """验 server 响应:server_sig + clock skew + nonce 一次性。

    Returns:
        (ok, error_code)
    """
    server_sig_hex = response.get("server_sig", "")
    server_timestamp = int(response.get("server_timestamp", 0))

    # clock skew 校验
    if abs(client_local_time - server_timestamp) > CLOCK_SKEW_TOLERANCE_SEC:
        return False, ErrCode.BAD_TIMESTAMP

    # nonce 必须匹配(防重放)
    resp_nonce_hex = response.get("nonce", "")
    try:
        resp_nonce = bytes.fromhex(resp_nonce_hex)
    except ValueError:
        return False, ErrCode.BAD_RESPONSE
    if resp_nonce != expected_nonce:
        return False, ErrCode.BAD_RESPONSE

    # 验签(如果 server pubkey 没嵌 → 跳过,仅 dev)
    if not _SERVER_PUBKEY:
        logger.debug("server pubkey 未嵌入,跳过 server_sig 验签(仅开发用)")
        return True, ""

    if not server_sig_hex or len(server_sig_hex) != 128:
        return False, ErrCode.BAD_SIGNATURE
    try:
        sig = bytes.fromhex(server_sig_hex)
        # server 签的 payload = json 序列化 {fresh_until, server_timestamp,
        # revoked_prefixes, client_pubkey, nonce},sort_keys
        signed_dict = {
            "fresh_until": int(response.get("fresh_until", 0)),
            "server_timestamp": server_timestamp,
            "revoked_prefixes": list(response.get("revoked_prefixes", [])),
            "client_pubkey": expected_client_pubkey_hex,
            "nonce": resp_nonce_hex,
        }
        msg = json.dumps(signed_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
        pub = Ed25519PublicKey.from_public_bytes(_SERVER_PUBKEY)
        pub.verify(sig, msg)
    except Exception as exc:
        logger.warning("server_sig 验签失败: %s", exc)
        return False, ErrCode.BAD_SIGNATURE

    return True, ""


def _do_http_post(url: str, payload: dict, timeout_sec: int) -> Tuple[Optional[dict], str]:
    """POST payload 到 url,返 (response_dict, error_code)。

    不引入额外 HTTP 依赖(用 stdlib urllib),避免 license 子包依赖膨胀。
    """
    import urllib.error
    import urllib.request

    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent": "DouStudio-Heartbeat/0.3.1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read()
            try:
                return json.loads(raw.decode("utf-8")), ""
            except (ValueError, UnicodeDecodeError) as exc:
                logger.warning("server response 解析失败: %s", exc)
                return None, ErrCode.BAD_RESPONSE
    except urllib.error.HTTPError as exc:
        # 4xx/5xx 都读 body,error_code 在 body 里
        try:
            data = json.loads(exc.read().decode("utf-8"))
        except Exception:
            return None, ErrCode.BAD_RESPONSE
        # 透传 server 给的 error code(供 grace 状态判断)
        return data, data.get("code", ErrCode.BAD_RESPONSE)
    except urllib.error.URLError as exc:
        msg = str(exc).lower()
        if "timed out" in msg or "timeout" in msg:
            return None, ErrCode.TIMEOUT
        return None, ErrCode.NETWORK
    except (TimeoutError, OSError) as exc:
        if "timed out" in str(exc).lower():
            return None, ErrCode.TIMEOUT
        return None, ErrCode.NETWORK


def perform_handshake(
    *,
    license_token_hex: str,
    client_priv_seed: bytes,
    fingerprint_hex: str,
    server_url: str = DEFAULT_SERVER_URL,
    timeout_sec: int = HANDSHAKE_TIMEOUT_SEC,
) -> HeartbeatResult:
    """启动握手(同步,主窗口前调一次)。

    Args:
        license_token_hex: 完整 v0.3.1 license_token 的 hex 编码
            (从 activated.bin 读出的 license_token_blob.hex())
        client_priv_seed: 32 字节 Ed25519 seed,从 activated.bin DSA1 头后读出
        fingerprint_hex: 当前机器 64-hex fingerprint
        server_url: 心跳服务器 URL
        timeout_sec: HTTP 超时秒数,默认 5

    Returns:
        HeartbeatResult。ok=True 时 fresh_until / clock_offset_ms / revoked_prefixes 都已填好
    """
    if len(client_priv_seed) != 32:
        return HeartbeatResult(ok=False, error_code=ErrCode.BAD_RESPONSE)

    # 1. 准备 client 签名(从 priv seed 派生 pub)
    try:
        client_priv = Ed25519PrivateKey.from_private_bytes(client_priv_seed)
        client_pub = client_priv.public_key().public_bytes_raw()
    except Exception:
        return HeartbeatResult(ok=False, error_code=ErrCode.BAD_RESPONSE)
    client_pubkey_hex = client_pub.hex()

    nonce = secrets.token_bytes(NONCE_LEN)
    local_ts = int(time.time())
    try:
        client_sig = _sign_client_request(
            client_priv_seed=client_priv_seed,
            fingerprint_hex=fingerprint_hex,
            timestamp=local_ts,
            nonce=nonce,
        )
    except Exception:
        return HeartbeatResult(ok=False, error_code=ErrCode.BAD_RESPONSE)

    payload = {
        "license_token": license_token_hex,
        "fingerprint": fingerprint_hex,
        "client_pubkey": client_pubkey_hex,
        "timestamp": local_ts,
        "client_sig": client_sig.hex(),
        "nonce": nonce.hex(),
    }

    # 2. POST
    url = server_url.rstrip("/") + "/api/heartbeat"
    response, error_code = _do_http_post(url, payload, timeout_sec)
    if response is None:
        return HeartbeatResult(ok=False, error_code=error_code)

    # 3. 检查 ok 标志
    if not response.get("ok", False):
        return HeartbeatResult(
            ok=False,
            error_code=response.get("code", ErrCode.BAD_RESPONSE),
            server_timestamp=int(response.get("server_timestamp", 0)),
        )

    # 4. 验 server_sig + clock skew
    ok, err = _verify_server_response(
        response,
        expected_client_pubkey_hex=client_pubkey_hex,
        expected_nonce=nonce,
        client_local_time=local_ts,
    )
    if not ok:
        return HeartbeatResult(
            ok=False,
            error_code=err,
            server_timestamp=int(response.get("server_timestamp", 0)),
        )

    server_ts = int(response.get("server_timestamp", 0))
    fresh_until = int(response.get("fresh_until", 0))
    revoked = tuple(response.get("revoked_prefixes", []))

    return HeartbeatResult(
        ok=True,
        fresh_until=fresh_until,
        server_timestamp=server_ts,
        clock_offset_ms=(server_ts - local_ts) * 1000,
        revoked_prefixes=revoked,
    )