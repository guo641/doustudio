"""
v0.3.0:机器指纹采集 + HMAC 绑定。

三层硬件 ID(任一层为空 → 退化为其他层相加,最终仍要拼出非空 hash):
  1. Win32_ComputerSystemProduct.UUID    —— SMBIOS UUID,主板焊接值
  2. Win32_BaseBoard.SerialNumber        —— 主板序列号
  3. Win32_Processor.ProcessorId         —— CPU ProcessorId(型号+特性+序列号)

为什么 PowerShell 而不是 ctypes(P/Invoke):ctypes 调 GetSystemFirmwareTable
要拆 SMBIOS 表头,字段偏移跨厂商不固定,代码量翻倍。PowerShell Get-CimInstance
解析由 .NET CIM 库完成,字段名稳定,跨 Windows 10 / 11 / Server 通用。

非 Windows 平台:返回 "(non-windows)" marker,仍 SHA-256,签名/激活码照常工作
(开发者在 macOS / Linux 开发的 SI 不是真实用户机器)。

timeout=4s:Get-CimInstance 在断网 / WMI 服务卡时可能挂死,4s 强杀。

两根对外 API:
  - collect_raw() / collect_hex() —— 原始 / SHA-256 指纹
  - hmac_hex(hex_key)              —— verifier 用的版本,key 是公钥的 hex 字符串
  - fingerprint_hmac(raw)          —— 签发工具用的版本,内部解 XOR 取真公钥后
                                     HMAC(raw_fingerprint),跟 verifier 产出
                                     bit-for-bit 一致
"""
from __future__ import annotations

import hashlib
import hmac as _hmac
import os
import platform
import subprocess
import sys
from typing import Tuple


# 1. SMBIOS UUID + 2. 主板序列号 + 3. CPU ProcessorId
# 每个值单独 Trim(),WMI 末尾空格常出现。空值(empty string)保留为空段然后在
# sha256 输入里以 \n 分隔,避免 "AA" + "" + "BB" ≡ "AA" + "BB" 的哈希冲突。
_CIM_QUERY = (
    "Get-CimInstance Win32_ComputerSystemProduct | Select-Object -ExpandProperty UUID;"
    "Get-CimInstance Win32_BaseBoard | Select-Object -ExpandProperty SerialNumber;"
    "Get-CimInstance Win32_Processor | Select-Object -ExpandProperty ProcessorId"
)


def _collect_raw() -> str:
    """调 PowerShell 收集三层硬件 ID,拼成单行 raw 字符串。失败 → "non-windows" 占位。"""
    if platform.system() != "Windows":
        return "non-windows"

    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", _CIM_QUERY],
            capture_output=True,
            text=True,
            timeout=4,
            check=False,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return "non-windows"

    if proc.returncode != 0:
        return "non-windows"

    # PowerShell 输出格式:
    #   <uuid>\n
    #   <serial>\n
    #   <cpuid>\n
    # (中间可能有空行 —— 字段为空时)
    parts = [seg.strip() for seg in proc.stdout.splitlines()]
    parts = [p for p in parts if p]
    if not parts:
        return "non-windows"
    # "|" 分隔,避免 "ABC" + "DEF" 字典序撞 "AB" + "CDEF" 这种拼接歧义
    return "|".join(parts)


# 应用启动一次之后缓存 —— 每次 API 调用都跑 PowerShell 慢且影响稳定性
_cached_raw: str | None = None
_cached_hex: str | None = None


def collect_raw() -> str:
    """未摘要的 raw fingerprint(多设备 + 字符串 + 整机 hash 的混合)。"""
    global _cached_raw
    if _cached_raw is None:
        _cached_raw = _collect_raw()
    return _cached_raw


def collect_hex() -> str:
    """SHA-256 hex digest(raw fingerprint)。64 个 hex 字符。"""
    global _cached_hex
    if _cached_hex is None:
        _cached_hex = hashlib.sha256(collect_raw().encode("utf-8")).hexdigest()
    return _cached_hex


def reset_cache() -> None:
    """测试专用 —— 强制下次调用重新采集 raw(模拟机器硬件变化)。"""
    global _cached_raw, _cached_hex
    _cached_raw = None
    _cached_hex = None


def hmac_hex(hex_key: str) -> str:
    """用 32 字节(或 64 hex chars) key 对 raw fingerprint 做 HMAC-SHA256,再 hex 编码。

    key 来自 verifier 解码出的 Ed25519 公钥(raw 32 bytes,hex 化为 64 chars),
   使得:fingerprint 不能在公开渠道复用 —— 攻击者拿到 raw fingerprint 字符串
    但拿不到 embedded_pubkey,无法构造合法 fingerprint_hex。
    """
    raw = collect_raw().encode("utf-8")
    return _hmac.new(hex_key.encode("ascii"), raw, hashlib.sha256).hexdigest()


def _decoded_pubkey() -> bytes:
    """解 XOR 取真 32 字节 Ed25519 公钥 —— 跟 verifier._init_pyx 同一段逻辑。

    只是为了让签发工具(fingerprint_hmac)能复现 verifier 算出来的 64-hex HMAC,
    避免「签发端和验签端用不同 key 算出不同 HMAC → 激活码无效」的死结。
    复用 doupool.license._embedded_pubkey 这个**已存在**的常量 module,不再
    单独维护一份密钥 —— 单一信任源。
    """
    from doupool.license import _embedded_pubkey  # noqa: WPS433 — 故意 late import
    mask = bytes(_embedded_pubkey.XOR_MASK)
    enc = bytes(_embedded_pubkey.ENCRYPTED_PUBKEY)
    if len(mask) != 32 or len(enc) != 32:
        raise RuntimeError(
            f"embedded pubkey 长度异常: mask={len(mask)} enc={len(enc)} (应为 32)"
        )
    return bytes(a ^ b for a, b in zip(mask, enc))


def fingerprint_hmac(raw_fingerprint: str) -> str:
    """签发工具专用:对 raw_fingerprint 算 HMAC-SHA256(embedded_pubkey_hex, raw)。

    关键:必须跟 verifier(`_license_verify.py:103` → `hmac_hex(pubkey_hex)`)
    使用**同一段 key**。verifier 端 key 是 `_pubkey.hex()` 后的 64 个 ASCII
    字符,所以签发端也必须用 hex 化 64 chars —— 千万**不要**直接用 raw 32
    字节,HMAC(同 raw,不同 key)产出截然不同,签完的码 100% 验不过。

    raw_fingerprint 从 collect_raw() 来。签发流程里 /api/fingerprint-self-test
    会调本函数,生成可在本机直接激活的 code 验证链路通。
    """
    # 走 fingerprint 自己的 raw 采集,而不是让调用方传 —— 调用方常忘记调
    # collect_raw(),传空字符串进来还是能跑但出空 HMAC。失败 loud。
    if raw_fingerprint is None:
        raise ValueError("raw_fingerprint 不能为 None")
    pubkey_hex = _decoded_pubkey().hex()
    return _hmac.new(
        pubkey_hex.encode("ascii"),
        raw_fingerprint.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def both() -> Tuple[str, str]:
    """返回 (raw, hex) 元组 —— 用于 /api/license/status 给用户展示。"""
    return collect_raw(), collect_hex()
