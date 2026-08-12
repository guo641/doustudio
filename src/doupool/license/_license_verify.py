"""
v0.3.0:信任根的 **纯 Python 备用实现**。

Cython 编译的 `_license_verify.cp312-win_amd64.pyd` 是首选 —— 反汇编难度比
.py 高一个数量级。**这个文件只在 .pyd 缺失时被 import**(Python 模块查找
顺序:.pyd → .py,先找到先得)。

什么时候会用到这个 .py 版本:
  1. 开发者本地没装 MSVC,只跑 `pip install -e .` 没编 .pyd
  2. 跨平台开发(Linux / macOS 不出 Windows .pyd)
  3. CI 在没有 MSVC 的 runner 上跑

安全权衡:
  - 信任根逻辑完全相同,只是源码是明文 .py(不是 .pyd 二进制)
  - PyInstaller 打包后,.py 也会被打进 PYZ archive,逆向门槛跟普通 .pyc 一样
  - 但是!激活流程仍然需要正确密钥 → 拿不到私钥就不能签新码,跟 .pyd
    路径是同样的密码学保证

所有逻辑必须跟 verifier.pyx 同步(同 plan §D 同一份 spec)。
"""
from __future__ import annotations

import base64
import json
import sys
import time
from typing import Tuple

from doupool.license import anti_debug
from doupool.license import crypto as _crypto
from doupool.license import fingerprint as _fingerprint
from doupool.license import storage as _storage
from doupool.license._embedded_pubkey import ENCRYPTED_PUBKEY, XOR_MASK


_MIN_APP_VERSION: str = "0.3.0"

# 模块级状态(纯 Python 全局变量,跟 .pyx 的 cdef 等价)
_pubkey: bytes = b""
_initialized: bool = False

_cached_status: dict = {
    "status": "missing",
    "fingerprint_hex": "",
    "customer": "",
    "issued_at": 0,
    "expires_at": 0,
    "loaded": False,
}


def _decode_pubkey() -> bytes:
    if len(XOR_MASK) != 32 or len(ENCRYPTED_PUBKEY) != 32:
        return b""
    return bytes(a ^ b for a, b in zip(XOR_MASK, ENCRYPTED_PUBKEY))


def _init_pyx() -> None:
    """import-time side-effect 解码公钥 + 跑反调试。
    注意:不在这里读 activated.bin —— 那是 verify_token / activate 的职责。
    """
    global _pubkey, _initialized
    _pubkey = _decode_pubkey()
    _initialized = bool(_pubkey)
    try:
        anti_debug.run_checks()
    except Exception:
        # 反调试本身抛 → 当作未检测
        pass


def _b32decode(s: str) -> bytes:
    s = s.strip().replace(" ", "").replace("-", "").replace("=", "")
    pad = (-len(s)) % 8
    return base64.b32decode(s + ("=" * pad))


def _b32encode(b: bytes) -> str:
    return base64.b32encode(b).decode("ascii").rstrip("=")


def _version_tuple(v: str) -> tuple:
    """'0.3.0' → (0, 3, 0)。'1.2' → (1, 2, 0)。不能解析 → (-1,) 让其小于一切。"""
    try:
        parts = []
        for seg in v.split("."):
            seg = seg.strip()
            if not seg.isdigit():
                return (-1,)
            parts.append(int(seg))
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])
    except Exception:
        return (-1,)


def current_fingerprint() -> str:
    """64 hex chars —— HMAC-SHA256(embedded pubkey, raw_fingerprint)。
    启动时缓存(模块导入完成后第一次调),之后任何调用都返回同一个值。
    """
    pubkey_hex = _pubkey.hex() if _pubkey else "00" * 32
    return _fingerprint.hmac_hex(pubkey_hex)


def _load_status_from_disk() -> dict:
    """读 activated.bin → 跑 verify_token → 缓存结果。无文件 → status='missing'。"""
    blob = _storage.read_token()
    if not blob:
        return {
            "status": "missing",
            "fingerprint_hex": "",
            "customer": "",
            "issued_at": 0,
            "expires_at": 0,
        }
    success, payload, error = verify_token(blob)
    if not success or not payload:
        return {
            "status": "expired",
            "fingerprint_hex": "",
            "customer": "",
            "issued_at": 0,
            "expires_at": 0,
            "error": error,
        }
    return {
        "status": "valid",
        "fingerprint_hex": payload.get("fingerprint_hex", ""),
        "customer": payload.get("customer", ""),
        "issued_at": int(payload.get("issued_at", 0)),
        "expires_at": int(payload.get("expires_at", 0)),
    }


def get_activation_status() -> str:
    """返 'valid' | 'expired' | 'missing'。
    只读 disk + 跑 verify_token,**不**做 ensure-activated 闸门判断。
    """
    global _cached_status
    if not _cached_status.get("loaded"):
        _cached_status = _load_status_from_disk()
        _cached_status["loaded"] = True
    return _cached_status.get("status", "missing")


def verify_token(token_blob: bytes) -> Tuple[bool, dict | None, str]:
    """一站式 token 校验 —— 所有调用方都应该走这里。

    Args:
        token_blob: 完整激活码字节串(已 strip 的 str.encode('utf-8'))。

    Returns:
        (success: bool, payload: dict | None, error: str)
    """
    # 反调试 —— 检测到调试器就当 token 失效,不暴露「检测到了」
    if anti_debug.TAMPER_DETECTED:
        return False, None, "激活码无效"

    if not _initialized or not _pubkey:
        return False, None, "应用未正确初始化"

    # 解码
    try:
        text = bytes(token_blob).decode("ascii", errors="replace")
    except Exception:
        return False, None, "激活码格式错误"
    if "." not in text:
        return False, None, "激活码格式错误"
    parts = text.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False, None, "激活码格式错误"

    try:
        payload_bytes = _b32decode(parts[0])
        sig_bytes = _b32decode(parts[1])
    except Exception:
        return False, None, "激活码格式错误"

    if len(sig_bytes) != 64:
        return False, None, "激活码格式错误"

    # payload 解析
    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return False, None, "激活码格式错误"

    if not isinstance(payload, dict):
        return False, None, "激活码格式错误"

    # 版本校验
    if int(payload.get("v", 0)) != 1:
        return False, None, "激活码格式错误"
    min_app = str(payload.get("min_app_version", "0.0.0"))
    if _version_tuple(min_app) > _version_tuple(_MIN_APP_VERSION):
        return False, None, "应用版本过旧,请升级"

    # 签名校验
    if not _crypto.verify(_pubkey, payload_bytes, sig_bytes):
        return False, None, "激活码无效"

    # 机器指纹 HMAC 校验
    expected_fp = current_fingerprint()
    payload_fp = str(payload.get("fingerprint_hex", ""))
    if not payload_fp or payload_fp != expected_fp:
        return False, None, "激活码与本机不匹配"

    # 过期校验
    expires_at = int(payload.get("expires_at", 0))
    if expires_at > 0 and int(time.time()) >= expires_at:
        return False, None, "激活码已过期"

    return True, payload, ""


def activate(code: str) -> Tuple[bool, str]:
    """校验 code,通过 → write_token(persist)。返 (success, error_message)。"""
    cleaned = (code or "").strip().replace(" ", "").replace("-", "").replace("\n", "").replace("\r", "").replace("\t", "")
    cleaned = cleaned.replace("=", "")
    if len(cleaned) < 10 or len(cleaned) > 512:
        return False, "激活码格式错误"
    blob = cleaned.encode("ascii", errors="replace")
    success, payload, error = verify_token(blob)
    if not success:
        return False, error or "激活码无效"
    try:
        _storage.write_token(blob)
    except OSError:
        return False, "无法写入激活信息,请检查目录权限"
    global _cached_status
    _cached_status = {
        "status": "valid",
        "fingerprint_hex": payload.get("fingerprint_hex", ""),
        "customer": payload.get("customer", ""),
        "issued_at": int(payload.get("issued_at", 0)),
        "expires_at": int(payload.get("expires_at", 0)),
        "loaded": True,
    }
    return True, ""


def ensure_activated_or_exit() -> None:
    """闸门 —— 启动时 import-time 调一次。失败 sys.exit(7)。
    expired → 静默 sys.exit(0);missing / valid → 继续。
    """
    status = get_activation_status()
    if status == "expired":
        sys.exit(0)
    # missing / valid → 继续


def make_payload_json(raw_fingerprint_hex: str, customer: str, issued_at: int, expires_at: int, nonce: str) -> str:
    """签发工具内部用 —— 生成 payload JSON 字符串。"""
    payload = {
        "v": 1,
        "fingerprint_hex": raw_fingerprint_hex,
        "customer": customer,
        "issued_at": int(issued_at),
        "expires_at": int(expires_at),
        "min_app_version": _MIN_APP_VERSION,
        "nonce": nonce,
        "features": [],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def encode_code(payload_json: str, signature_64bytes: bytes) -> str:
    """签发工具内部用 —— payload + sig → 激活码字符串。"""
    payload_b32 = _b32encode(payload_json.encode("utf-8"))
    sig_b32 = _b32encode(signature_64bytes)
    return f"{payload_b32}.{sig_b32}"


# 触发 import-time side-effect —— 必须在所有函数定义之后
_init_pyx()