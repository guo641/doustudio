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

import base64
import binascii
import hashlib
import hmac
import http.client
import json
import logging
import os
import secrets
import ssl
import time
import urllib.request
from dataclasses import dataclass
from typing import Optional, Tuple

from cryptography import x509
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from . import _embedded_server_pubkey as _server_pk_module

logger = logging.getLogger(__name__)


# 常量(跟 server/app/config.py 对齐,集中维护)
DEFAULT_SERVER_URL: str = os.environ.get(
    "DOUSTUDIO_LICENSE_SERVER_URL",
    "https://124.221.210.12:8443",
)
SERVER_SPKI_PIN = "sha256/HnTrss/ACEvZ47WvSMdfdIvdhkwlwC2BUuw9m3LJ+4w="
HANDSHAKE_TIMEOUT_SEC: int = 5  # 启动握手不能拖过 5s
NONCE_LEN: int = 16
CLOCK_SKEW_TOLERANCE_SEC: int = 300  # ±5 分钟
REVOKED_PREFIX_HEX_LEN: int = 16
MAX_REVOKED_PREFIXES: int = 10_000


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


def _stored_token_hex_to_wire_hex(token_hex: str) -> str:
    """Normalize the persisted token representation to protocol wire hex.

    v0.3.1 activation files created by the Cython verifier retain the
    user-facing Base32 text as bytes, while test/new callers may already pass
    the raw wire blob encoded as hex.  The heartbeat protocol always sends the
    raw wire blob as hex; this compatibility step changes no field or schema.
    """
    try:
        stored = bytes.fromhex(token_hex)
    except (TypeError, ValueError):
        return token_hex

    # Base32 text is ASCII and contains only RFC 4648 alphabet characters.
    # Decode only when the whole stored value has that shape; arbitrary raw
    # wire bytes remain untouched.
    try:
        text = stored.decode("ascii").strip().replace("=", "")
    except UnicodeDecodeError:
        return stored.hex()
    if not text or any(char not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for char in text.upper()):
        return stored.hex()
    try:
        padding = "=" * ((8 - len(text) % 8) % 8)
        wire = base64.b32decode(text.upper() + padding, casefold=True)
    except (ValueError, binascii.Error):
        return stored.hex()
    if len(wire) < 129:
        return stored.hex()
    return wire.hex()


def _spki_pin_from_certificate_der(certificate_der: bytes) -> str:
    """Return the Chrome-style ``sha256/<base64>`` pin for a leaf cert."""
    certificate = x509.load_der_x509_certificate(certificate_der)
    spki = certificate.public_key().public_bytes(
        Encoding.DER,
        PublicFormat.SubjectPublicKeyInfo,
    )
    return "sha256/" + base64.b64encode(hashlib.sha256(spki).digest()).decode("ascii")


def _decode_server_pubkey() -> bytes:
    """XOR 解码 server 公钥;缺失或全零占位时返回空字节。"""
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
    """是否已解码出 server 公钥。"""
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

    P1-C: 显式校验响应里的 client_pubkey 回显是否匹配请求。

    Returns:
        (ok, error_code)
    """
    server_sig_hex = response.get("server_sig", "")
    server_timestamp = int(response.get("server_timestamp", 0))

    # P1-C: 响应 client_pubkey 回显显式校验
    # 防止攻击者返回不同的 pubkey 混淆客户端状态
    response_client_pubkey = response.get("client_pubkey", "")
    if response_client_pubkey != expected_client_pubkey_hex:
        logger.error(
            "响应 client_pubkey 不匹配: expected=%s, got=%s",
            expected_client_pubkey_hex[:16] + "...",
            response_client_pubkey[:16] + "..." if response_client_pubkey else "(empty)",
        )
        return False, ErrCode.BAD_RESPONSE

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

    # 生产环境公钥缺失必须拒绝。只有显式的独立开发开关允许跳过，
    # 不能再把“空公钥”本身当成放行条件。
    if not _SERVER_PUBKEY:
        if os.environ.get("DOUSTUDIO_DEV_SKIP_SIG") == "1":
            logger.warning(
                "SECURITY WARNING: DOUSTUDIO_DEV_SKIP_SIG=1,跳过 server_sig 验签;"
                "该模式仅允许本地开发使用"
            )
            return True, ""
        logger.error("server 公钥未嵌入或解码失败,拒绝心跳响应")
        return False, ErrCode.BAD_SIGNATURE

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


def _normalize_revoked_prefixes(value) -> Optional[tuple[str, ...]]:
    """Validate the signed server snapshot before it reaches disk."""
    if not isinstance(value, list) or len(value) > MAX_REVOKED_PREFIXES:
        return None
    normalized: list[str] = []
    seen: set[str] = set()
    for prefix in value:
        if not isinstance(prefix, str):
            return None
        item = prefix.strip().lower()
        if len(item) != REVOKED_PREFIX_HEX_LEN:
            return None
        try:
            if len(bytes.fromhex(item)) != REVOKED_PREFIX_HEX_LEN // 2:
                return None
        except ValueError:
            return None
        if item not in seen:
            seen.add(item)
            normalized.append(item)
    return tuple(normalized)


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    """HTTPS connection that accepts the self-signed cert only by SPKI pin."""

    def connect(self) -> None:
        super().connect()
        if self.sock is None:
            raise OSError("TLS socket 未建立")
        try:
            certificate_der = self.sock.getpeercert(binary_form=True)
            actual_pin = _spki_pin_from_certificate_der(certificate_der)
            if not hmac.compare_digest(actual_pin, SERVER_SPKI_PIN):
                raise ssl.SSLError("server certificate SPKI pin 不匹配")
        except Exception as exc:
            self.close()
            raise OSError("server certificate SPKI pin 校验失败") from exc


class _PinnedHTTPSHandler(urllib.request.HTTPSHandler):
    """urllib handler using an unverified TLS context plus mandatory SPKI pin."""

    def __init__(self) -> None:
        # The certificate is intentionally self-signed. Identity is established
        # exclusively by _PinnedHTTPSConnection's SPKI pin check above.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        super().__init__(context=context, check_hostname=False)

    def https_open(self, req):
        return self.do_open(_PinnedHTTPSConnection, req, context=self._context)


def _do_http_post(url: str, payload: dict, timeout_sec: int) -> Tuple[Optional[dict], str]:
    """POST payload 到 url,返 (response_dict, error_code)。

    不引入额外 HTTP 依赖(用 stdlib urllib),避免 license 子包依赖膨胀。
    """
    import urllib.error
    import urllib.parse
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
        scheme = urllib.parse.urlsplit(url).scheme.lower()
        if scheme == "https":
            opener = urllib.request.build_opener(_PinnedHTTPSHandler())
            response_context = opener.open(req, timeout=timeout_sec)
        elif scheme == "http" and os.environ.get("DOUSTUDIO_DEV_ALLOW_HTTP") == "1":
            logger.warning(
                "SECURITY WARNING: DOUSTUDIO_DEV_ALLOW_HTTP=1,使用未加密 HTTP 心跳;"
                "该模式仅允许本地开发测试"
            )
            response_context = urllib.request.urlopen(req, timeout=timeout_sec)
        else:
            logger.error("拒绝非 HTTPS license server URL: %s", url)
            return None, ErrCode.NETWORK
        with response_context as resp:
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
        "license_token": _stored_token_hex_to_wire_hex(license_token_hex),
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
    revoked = _normalize_revoked_prefixes(response.get("revoked_prefixes"))
    if revoked is None:
        return HeartbeatResult(
            ok=False,
            error_code=ErrCode.BAD_RESPONSE,
            server_timestamp=server_ts,
        )

    return HeartbeatResult(
        ok=True,
        fresh_until=fresh_until,
        server_timestamp=server_ts,
        clock_offset_ms=(server_ts - local_ts) * 1000,
        revoked_prefixes=revoked,
    )
