"""
v0.3.0 + v0.3.1 + v0.3.2:activated.bin 持久化 —— 原子写最关键。

v0.3.1 schema 升级:
  magic 'DSA1' (4B) +
  client_priv_seed (32B) +
  client_pubkey (32B) +
  fresh_until (8B LE int)  -- 半在线心跳服务器推的到期时间
  clock_offset_ms (4B LE int)  -- 客户端-服务器时钟偏差
  last_server_sync (4B LE int)  -- 最近一次成功心跳的 unix 时间
  license_token_blob (rest)  -- 完整 v0.3.1 wire format (含 priv + pub + payload + sig)

v0.3.0 旧格式 detect:
  - 文件 < 4+32+8+4+4=52B → 一定不是 v0.3.1 → 当 v0.3.0 旧 token blob 处理
  - 文件头不是 'DSA1' → 同上

v0.3.2 schema 在 v0.3.1 固定头后增加完整撤销前缀快照:
  magic 'DSA2' (4B) +
  client_priv_seed (32B) + client_pubkey (32B) +
  fresh_until (8B LE uint) + clock_offset_ms (4B LE int) +
  last_server_sync (4B LE uint) + revoked_count (4B LE uint) +
  revoked_prefixes (count * 8B) + license_token_blob (rest)

服务端 REVOKED_PREFIX_LEN=16 指 16 个 hex 字符,即每个前缀 8 字节,不是
16 字节。v0.3.2 读取兼容 v0.3.1;成功心跳时原子升级到 DSA2。
v0.3.0 旧 token 读到后需要用户重新激活(走 v0.3.1 协议)才能升级。

为什么原子写:激活成功的瞬间硬盘断电 / 进程被杀,半截 token 落盘。下次启动
会读"无 token" → 用户被踢回激活窗,体验糟糕到让人怀疑软件坏掉。

两步走:
  1. 写入 activated.bin.tmp(NamedTemporaryFile + fsync)
  2. Path.replace(tmp, target) —— POSIX / NTFS 都保证原子(同分区)

失败回滚:replace 失败 → 删 tmp,保留原 activated.bin。下次启动仍能用
之前的成功 token,不丢失授权。
"""
from __future__ import annotations

import base64
import binascii
import os
import hashlib
import hmac
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from doupool.license.storage_path import (
    activated_bin_path,
    tmp_activated_bin_path,
)

# v0.3.1 magic header(避免误识别 v0.3.0 老文件)
MAGIC_V031: bytes = b"DSA1"
MAGIC_V032: bytes = b"DSA2"

# 固定字段长度(v0.3.1)
_HEADER_SIZE: int = 4 + 32 + 32 + 8 + 4 + 4  # = 84 bytes
_HEADER_SIZE_V032: int = 4 + 32 + 32 + 8 + 4 + 4 + 4  # = 88 bytes
REVOKED_PREFIX_HEX_LEN: int = 16
_REVOKED_PREFIX_BYTES: int = REVOKED_PREFIX_HEX_LEN // 2
MAX_REVOKED_PREFIXES: int = 10_000
_MIN_LICENSE_TOKEN_BLOB_LEN: int = 129


@dataclass
class StoredToken:
    """read_token_v031 的解析结果。"""

    license_token_blob: bytes  # 完整 v0.3.1 wire format(已 strip)
    client_priv_seed: bytes    # 32B,heartbeat 用
    client_pubkey: bytes       # 32B,server 验签用
    fresh_until: int           # unix sec,半在线心跳到期时间
    clock_offset_ms: int       # 客户端-服务器时钟偏差(ms)
    last_server_sync: int      # 最近一次心跳 unix sec


@dataclass
class StoredTokenV032:
    """read_token_v032 的解析结果 (v0.3.2 含撤销前缀快照)。"""

    license_token_blob: bytes
    client_priv_seed: bytes
    client_pubkey: bytes
    fresh_until: int
    clock_offset_ms: int
    last_server_sync: int
    revoked_prefixes: tuple[str, ...]


def read_token() -> Optional[bytes]:
    """读 activated.bin(纯字节,不知道 schema)。

    调用方一般不会用这个,直接用 read_token_v031 / read_token_legacy 即可。
    """
    path = activated_bin_path()
    if not path.exists():
        return None
    try:
        return path.read_bytes()
    except OSError:
        return None


def read_token_v031() -> Optional[StoredToken]:
    """读 v0.3.1 格式的 activated.bin。

    Returns:
        StoredToken 或 None(文件不存在 / 不是 v0.3.1 格式 / 解析失败)
    """
    raw = read_token()
    if raw is None or len(raw) < _HEADER_SIZE:
        return None
    if raw[:4] != MAGIC_V031:
        return None
    # 解码固定字段(Little-Endian)
    client_priv_seed = raw[4:36]
    client_pubkey = raw[36:68]
    fresh_until, clock_offset_ms, last_server_sync = struct.unpack(
        "<QII", raw[68:84]
    )
    license_token_blob = raw[_HEADER_SIZE:]
    if not license_token_blob:
        return None
    return StoredToken(
        license_token_blob=license_token_blob,
        client_priv_seed=client_priv_seed,
        client_pubkey=client_pubkey,
        fresh_until=fresh_until,
        clock_offset_ms=clock_offset_ms,
        last_server_sync=last_server_sync,
    )


def read_token_v032() -> Optional[StoredTokenV032]:
    """读 v0.3.2 格式的 activated.bin (含撤销前缀快照)。"""
    raw = read_token()
    if raw is None or len(raw) < _HEADER_SIZE_V032 + _MIN_LICENSE_TOKEN_BLOB_LEN:
        return None
    if raw[:4] != MAGIC_V032:
        return None

    client_priv_seed = raw[4:36]
    client_pubkey = raw[36:68]
    try:
        fresh_until, clock_offset_ms, last_server_sync, revoked_count = struct.unpack(
            "<QiII", raw[68:_HEADER_SIZE_V032]
        )
    except struct.error:
        return None
    if revoked_count > MAX_REVOKED_PREFIXES:
        return None

    token_offset = _HEADER_SIZE_V032 + revoked_count * _REVOKED_PREFIX_BYTES
    if token_offset + _MIN_LICENSE_TOKEN_BLOB_LEN > len(raw):
        return None

    revoked_prefixes: list[str] = []
    seen: set[str] = set()
    offset = _HEADER_SIZE_V032
    for _ in range(revoked_count):
        prefix = raw[offset:offset + _REVOKED_PREFIX_BYTES].hex()
        if prefix not in seen:
            seen.add(prefix)
            revoked_prefixes.append(prefix)
        offset += _REVOKED_PREFIX_BYTES

    license_token_blob = raw[token_offset:]
    return StoredTokenV032(
        license_token_blob=license_token_blob,
        client_priv_seed=client_priv_seed,
        client_pubkey=client_pubkey,
        fresh_until=fresh_until,
        clock_offset_ms=clock_offset_ms,
        last_server_sync=last_server_sync,
        revoked_prefixes=tuple(revoked_prefixes),
    )


def read_token_legacy() -> Optional[bytes]:
    """读 v0.3.0 旧格式的 activated.bin(无 magic 头)。

    Returns:
        license_token blob bytes 或 None(文件不存在 / 已升级 v0.3.1)
    """
    raw = read_token()
    if raw is None or len(raw) < 16:
        return None
    if raw[:4] in (MAGIC_V031, MAGIC_V032):
        return None  # 已经是结构化 schema
    return raw


def _normalize_revoked_prefixes(prefixes: tuple[str, ...]) -> tuple[str, ...]:
    if len(prefixes) > MAX_REVOKED_PREFIXES:
        raise ValueError(
            f"revoked prefix 最多 {MAX_REVOKED_PREFIXES} 条,实际 {len(prefixes)}"
        )

    normalized: list[str] = []
    seen: set[str] = set()
    for prefix in prefixes:
        if not isinstance(prefix, str):
            raise ValueError(f"revoked prefix 必须是字符串,实际 {type(prefix).__name__}")
        value = prefix.strip().lower()
        if len(value) != REVOKED_PREFIX_HEX_LEN:
            raise ValueError(
                f"revoked prefix 必须是 {REVOKED_PREFIX_HEX_LEN} 字符 hex,实际: {prefix}"
            )
        try:
            raw = bytes.fromhex(value)
        except ValueError as exc:
            raise ValueError(f"revoked prefix 不是合法 hex: {prefix}") from exc
        if len(raw) != _REVOKED_PREFIX_BYTES:
            raise ValueError(f"revoked prefix 长度非法: {prefix}")
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return tuple(normalized)


def compute_license_hmac_prefix(client_pubkey: bytes, fingerprint_hex: str) -> str:
    """按 license server 契约计算本机 64-bit HMAC 前缀。"""
    if len(client_pubkey) != 32:
        raise ValueError("client_pubkey 必须 32 字节")
    if not isinstance(fingerprint_hex, str) or len(fingerprint_hex) != 64:
        raise ValueError("fingerprint 必须是 64 字符 hex")
    try:
        bytes.fromhex(fingerprint_hex)
    except ValueError as exc:
        raise ValueError("fingerprint 不是合法 hex") from exc
    return hmac.new(
        client_pubkey,
        fingerprint_hex.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:REVOKED_PREFIX_HEX_LEN]


# Short public name used by the heartbeat/bootstrap path and contract tests.
license_hmac_prefix = compute_license_hmac_prefix


def token_v031_keypair(license_token_blob: bytes) -> tuple[bytes, bytes] | None:
    """Extract the client keypair from a persisted v0.3.1 token.

    ``activate()`` persists the user-facing Base32 text, while integration
    fixtures and older callers may persist the decoded wire bytes.  The key
    segments are part of that token in both cases.  They are the identity
    anchor checked by the compiled verifier; the duplicate DSA header fields
    must never be trusted independently.
    """
    if not isinstance(license_token_blob, (bytes, bytearray)):
        return None
    stored = bytes(license_token_blob)
    wire = stored
    try:
        text = stored.decode("ascii").strip().replace("=", "")
    except UnicodeDecodeError:
        text = ""
    if text and all(char in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for char in text.upper()):
        try:
            padding = "=" * ((8 - len(text) % 8) % 8)
            decoded = base64.b32decode(text.upper() + padding, casefold=True)
            canonical = base64.b32encode(decoded).decode("ascii").rstrip("=")
            if (
                len(decoded) >= _MIN_LICENSE_TOKEN_BLOB_LEN
                and canonical == text.upper()
            ):
                wire = decoded
        except (ValueError, binascii.Error):
            pass
    if len(wire) < _MIN_LICENSE_TOKEN_BLOB_LEN:
        return None
    return wire[:32], wire[32:64]


def _validated_stored_token_keypair(stored: StoredToken | StoredTokenV032) -> tuple[bytes, bytes]:
    token_keys = token_v031_keypair(stored.license_token_blob)
    if token_keys is None:
        raise RuntimeError("activated.bin 中的 v0.3.1 token 无法解析 client keypair")
    if not hmac.compare_digest(stored.client_priv_seed, token_keys[0]):
        raise RuntimeError("activated.bin header client_priv_seed 与签名 token 不一致")
    if not hmac.compare_digest(stored.client_pubkey, token_keys[1]):
        raise RuntimeError("activated.bin header client_pubkey 与签名 token 不一致")
    return token_keys


def _append_required_revoked_prefix(
    prefixes: tuple[str, ...], required_prefix: str
) -> tuple[str, ...]:
    """Keep the local revoked marker even at the 10,000-entry wire limit."""
    normalized = _normalize_revoked_prefixes(tuple(prefixes))
    required = _normalize_revoked_prefixes((required_prefix,))[0]
    if required in normalized:
        return normalized
    if len(normalized) >= MAX_REVOKED_PREFIXES:
        normalized = normalized[: MAX_REVOKED_PREFIXES - 1]
    return normalized + (required,)


def write_token_v031(
    *,
    license_token_blob: bytes,
    client_priv_seed: bytes,
    client_pubkey: bytes,
    fresh_until: int = 0,
    clock_offset_ms: int = 0,
    last_server_sync: int = 0,
) -> None:
    """v0.3.1 原子写。失败抛 OSError。

    字段长度校验: priv/pub 必须是 32 字节;license_token_blob 至少 129 字节
    (v0.3.1 最小 = 32 priv + 32 pub + 1 payload + 64 sig)。
    """
    if len(client_priv_seed) != 32:
        raise ValueError(f"client_priv_seed 必须 32 字节,实际 {len(client_priv_seed)}")
    if len(client_pubkey) != 32:
        raise ValueError(f"client_pubkey 必须 32 字节,实际 {len(client_pubkey)}")
    if len(license_token_blob) < 129:
        raise ValueError(
            f"license_token_blob 至少 129 字节(v0.3.1 最小),实际 {len(license_token_blob)}"
        )
    if fresh_until < 0 or last_server_sync < 0:
        raise ValueError("fresh_until / last_server_sync 不能为负")

    header = (
        MAGIC_V031
        + client_priv_seed
        + client_pubkey
        + struct.pack("<QII", fresh_until, clock_offset_ms, last_server_sync)
    )
    blob = header + license_token_blob

    target = activated_bin_path()
    tmp = tmp_activated_bin_path()
    try:
        with open(tmp, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_token_v032(
    *,
    license_token_blob: bytes,
    client_priv_seed: bytes,
    client_pubkey: bytes,
    fresh_until: int = 0,
    clock_offset_ms: int = 0,
    last_server_sync: int = 0,
    revoked_prefixes: tuple[str, ...] = (),
) -> None:
    """v0.3.2 原子写 (含撤销前缀快照)。失败抛 OSError。"""
    if len(client_priv_seed) != 32:
        raise ValueError(f"client_priv_seed 必须 32 字节,实际 {len(client_priv_seed)}")
    if len(client_pubkey) != 32:
        raise ValueError(f"client_pubkey 必须 32 字节,实际 {len(client_pubkey)}")
    if len(license_token_blob) < _MIN_LICENSE_TOKEN_BLOB_LEN:
        raise ValueError(
            f"license_token_blob 至少 {_MIN_LICENSE_TOKEN_BLOB_LEN} 字节,"
            f"实际 {len(license_token_blob)}"
        )
    if not 0 <= fresh_until <= 0xFFFFFFFFFFFFFFFF:
        raise ValueError("fresh_until 超出 uint64 范围")
    if not -(2 ** 31) <= clock_offset_ms <= 2 ** 31 - 1:
        raise ValueError("clock_offset_ms 超出 int32 范围")
    if not 0 <= last_server_sync <= 0xFFFFFFFF:
        raise ValueError("last_server_sync 超出 uint32 范围")

    prefixes = _normalize_revoked_prefixes(tuple(revoked_prefixes))
    header = (
        MAGIC_V032
        + client_priv_seed
        + client_pubkey
        + struct.pack(
            "<QiII",
            fresh_until,
            clock_offset_ms,
            last_server_sync,
            len(prefixes),
        )
    )
    revoked_blob = b"".join(bytes.fromhex(prefix) for prefix in prefixes)
    blob = header + revoked_blob + license_token_blob

    target = activated_bin_path()
    tmp = tmp_activated_bin_path()
    try:
        with open(tmp, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def write_token(blob: bytes) -> None:
    """兼容 v0.3.0 旧接口 —— 直接写 license_token blob(无 header)。

    **v0.3.1 起不推荐使用**。新代码请用 write_token_v031。
    """
    if not isinstance(blob, (bytes, bytearray)):
        raise TypeError(f"token blob 必须是 bytes,不是 {type(blob).__name__}")
    target = activated_bin_path()
    tmp = tmp_activated_bin_path()
    try:
        with open(tmp, "wb") as f:
            f.write(blob)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def update_heartbeat_fields(
    *,
    fresh_until: int,
    clock_offset_ms: int,
    last_server_sync: int,
) -> None:
    """只更新 activated.bin 里的 fresh_until / clock_offset_ms / last_server_sync。

    用于后台心跳:不重写整个文件,只覆盖固定字段区(前 84 字节之后部分不变)。
    仍然走 tempfile + replace 保证原子性。
    """
    raw = read_token()
    if raw is None or len(raw) < _HEADER_SIZE or raw[:4] != MAGIC_V031:
        raise RuntimeError("activated.bin 不是 v0.3.1 格式,无法 update heartbeat 字段")

    # 替换 68-84 段的 3 个字段
    new_header_fields = struct.pack(
        "<QII", fresh_until, clock_offset_ms, last_server_sync
    )
    new_raw = raw[:68] + new_header_fields + raw[84:]

    target = activated_bin_path()
    tmp = tmp_activated_bin_path()
    try:
        with open(tmp, "wb") as f:
            f.write(new_raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def update_heartbeat_fields_v032(
    *,
    fresh_until: int,
    clock_offset_ms: int,
    last_server_sync: int,
    revoked_prefixes: tuple[str, ...],
) -> None:
    """更新心跳字段和撤销快照;DSA1 会在同一次原子写中升级为 DSA2。"""
    raw = read_token()
    if raw is None or len(raw) < 4:
        raise RuntimeError("activated.bin 不存在或损坏")

    if raw[:4] == MAGIC_V031:
        stored = read_token_v031()
    elif raw[:4] == MAGIC_V032:
        stored = read_token_v032()
    else:
        stored = None
    if stored is None:
        raise RuntimeError(
            f"activated.bin 不是有效 v0.3.1/v0.3.2 格式 (magic={raw[:4].hex()})"
        )

    _validated_stored_token_keypair(stored)

    write_token_v032(
        license_token_blob=stored.license_token_blob,
        client_priv_seed=stored.client_priv_seed,
        client_pubkey=stored.client_pubkey,
        fresh_until=fresh_until,
        clock_offset_ms=clock_offset_ms,
        last_server_sync=last_server_sync,
        revoked_prefixes=revoked_prefixes,
    )


def mark_current_license_revoked(*, fingerprint_hex: str) -> str:
    """持久化当前 license 的撤销前缀,不延长任何心跳有效期。"""
    stored = read_token_v032()
    if stored is None:
        stored = read_token_v031()
    if stored is None:
        raise RuntimeError("activated.bin 不是有效 v0.3.1/v0.3.2 格式")

    _, token_pubkey = _validated_stored_token_keypair(stored)
    own_prefix = compute_license_hmac_prefix(token_pubkey, fingerprint_hex)
    existing = tuple(getattr(stored, "revoked_prefixes", ()))
    prefixes = _append_required_revoked_prefix(existing, own_prefix)
    write_token_v032(
        license_token_blob=stored.license_token_blob,
        client_priv_seed=stored.client_priv_seed,
        client_pubkey=stored.client_pubkey,
        fresh_until=stored.fresh_until,
        clock_offset_ms=stored.clock_offset_ms,
        last_server_sync=stored.last_server_sync,
        revoked_prefixes=prefixes,
    )
    return own_prefix


def clear_token() -> bool:
    """主动清除 activated.bin(开发 / 调试 / 「卸载授权」按钮)。返回是否真的删除了。"""
    path = activated_bin_path()
    if not path.exists():
        return False
    try:
        path.unlink()
        return True
    except OSError:
        return False
