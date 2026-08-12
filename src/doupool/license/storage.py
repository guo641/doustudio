"""
v0.3.0 + v0.3.1:activated.bin 持久化 —— 原子写最关键。

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

读路径兼容: detect schema 版本,两种格式都能 read,但 **写路径只写 v0.3.1**。
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

import os
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

# 固定字段长度(v0.3.1)
_HEADER_SIZE: int = 4 + 32 + 32 + 8 + 4 + 4  # = 84 bytes


@dataclass
class StoredToken:
    """read_token_v031 的解析结果。"""

    license_token_blob: bytes  # 完整 v0.3.1 wire format(已 strip)
    client_priv_seed: bytes    # 32B,heartbeat 用
    client_pubkey: bytes       # 32B,server 验签用
    fresh_until: int           # unix sec,半在线心跳到期时间
    clock_offset_ms: int       # 客户端-服务器时钟偏差(ms)
    last_server_sync: int      # 最近一次心跳 unix sec


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


def read_token_legacy() -> Optional[bytes]:
    """读 v0.3.0 旧格式的 activated.bin(无 magic 头)。

    Returns:
        license_token blob bytes 或 None(文件不存在 / 已升级 v0.3.1)
    """
    raw = read_token()
    if raw is None or len(raw) < 16:
        return None
    if raw[:4] == MAGIC_V031:
        return None  # 已经是 v0.3.1
    return raw


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