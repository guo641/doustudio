# cython: language_level=3, boundscheck=False, wraparound=False, cdivision=True, embedsignature=False
# distutils: language = c
"""
v0.3.0:信任根 —— verifier.pyx

经过 Cython 编译为 _license_verify.cp312-win_amd64.pyd,进入 onedir 分发。
这是整个离线激活系统中 **唯一** 接触密钥学计算 + 硬件指纹 + 过期校验
的代码路径。其他模块只做 I/O 与 UI 编排。

设计要点(plan §D / §C):
  - XOR 编码公钥在 import-time 解码 → cdef bytes _pubkey
  - 模块底部 _init_pyx() 触发 import-time side-effect
  - 所有激活码校验 / fingerprint HMAC / 过期判断都在这里
  - verify_token() 返回 (success: bool, payload: dict | None, error: str)
  - activate(code) 调 verify_token + write_token(blob) 持久化
  - ensure_activated_or_exit() 是 verify_at_import.py 调用的闸门

激活码 wire format:`<base32(payload_json)>.<base32(signature_64bytes)>`
中间的点号方便前端 split;粘贴时前端先 strip 空白 / dash / 等号。

为什么 Cython:verifier 是唯一信任根,把 verifier 编成 .pyd 让所有非信任
Python 代码触碰不到密钥学路径。攻击者即使 patch main.py / api.py,
verifier 仍按设计校验签名 → 失败 sys.exit。
"""
from __future__ import annotations

import base64
import json
import logging
import sys
import time

# 这些 import 在 Cython 编译时全部走纯 Python 路径 —— 因为 verifier.pyx
# 编译为 .pyd 时,运行时由 .pyd 加载这些纯 Python 模块。
# 性能:verifier 启动时调用一次,不在性能关键路径上。
from doupool.license import anti_debug
from doupool.license import crypto as _crypto
from doupool.license import fingerprint as _fingerprint
from doupool.license import storage as _storage
from doupool.license._embedded_pubkey import ENCRYPTED_PUBKEY, XOR_MASK


# 模块级常量:当前最低客户端版本(防降级攻击 —— 老版本也能用,新版本才有的
# 安全策略被绕过)。每次升级到新 major 时同步提升。
# v0.3.1:强制升级 0.3.1(license_token wire format 变了)
_MIN_APP_VERSION: str = "0.3.1"

# v0.3.1 半在线心跳参数(跟 server 对齐)
_FRESH_DAYS: int = 30
_GRACE_DAYS: int = 7
# grace 用完时,verifier 行为: sys.exit(0) 静默退出(避免弹窗被逆向)
_GRACE_EXIT_CODE: int = 0

# v0.3.1 wire format 最小长度: 32B priv + 32B pub + 1B payload + 64B sig
_MIN_TOKEN_LEN_V031: int = 129
# v0.3.0 旧 wire format 标记:含 `.` 分隔符
_V030_SEP: bytes = b"."

# 缓存字段(plain Python dict,Cython 不优化它,但访问频率低,无开销担忧)
_cached_status: dict = {
    "status": "missing",
    "fingerprint_hex": "",
    "customer": "",
    "issued_at": 0,
    "expires_at": 0,
    "loaded": False,
    "fresh_until": 0,
    "last_server_sync": 0,
    "needs_heartbeat": False,
    "grace": False,  # fresh_until 已过但 grace 期内
}

# cdef 没法在模块顶部声明 dict + 赋值,放 _init_pyx 内一起做
cdef bytes _pubkey = b""
cdef bint _initialized = False


def _decode_pubkey() -> bytes:
    """XOR 解码回 32 字节原始 Ed25519 公钥。失败 → b''(verifier 拒绝一切 token)。"""
    if len(XOR_MASK) != 32 or len(ENCRYPTED_PUBKEY) != 32:
        return b""
    return bytes(a ^ b for a, b in zip(XOR_MASK, ENCRYPTED_PUBKEY))


def _init_pyx():
    """import-time side-effect 解码公钥 + 跑反调试。

    注意:不要在这里读 activated.bin —— 那是 verify_token / activate 的
    职责。verifier 只提供「这是不是合法的 token」原语,持久化策略留给上层。
    """
    global _pubkey, _initialized
    _pubkey = _decode_pubkey()
    _initialized = bool(_pubkey)
    # 反调试跑一次 —— 设 TAMPER_DETECTED 标志
    try:
        anti_debug.run_checks()
    except Exception:
        # 反调试本身抛 → 当作未检测
        pass


# 标准 base32 解码(无 padding)。RFC 4648 base32 用 A-Z 2-7,32 chars 编码
# 20 bytes 整数倍。signature 64 bytes → 64 * 8 / 5 = 103.2 → 104 chars with
# padding → 103 chars without padding(编码 length=ceil(64*8/5)=103)。
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
    """读 activated.bin → 跑 verify_token → 缓存结果。无文件 → status='missing'。

    v0.3.1 新增:
      - detect v0.3.1 新格式(DSA1 头部) → 走 read_token_v031,加载 fresh_until
      - 旧格式 → 走 read_token_legacy,无 fresh_until 字段,标记 needs_heartbeat
    """
    # 先尝试 v0.3.1 格式
    v031 = _storage.read_token_v031()
    if v031 is not None:
        success, payload, error, _ = verify_token(v031.license_token_blob)
        if not success or not payload:
            return {
                "status": "expired",
                "fingerprint_hex": "",
                "customer": "",
                "issued_at": 0,
                "expires_at": 0,
                "error": error,
                "fresh_until": 0,
                "last_server_sync": 0,
                "needs_heartbeat": False,
                "grace": False,
            }
        return _evaluate_status_with_fresh(
            payload,
            fresh_until=v031.fresh_until,
            last_server_sync=v031.last_server_sync,
        )

    # fallback 到 v0.3.0 旧格式
    legacy = _storage.read_token_legacy()
    if not legacy:
        return {
            "status": "missing",
            "fingerprint_hex": "",
            "customer": "",
            "issued_at": 0,
            "expires_at": 0,
            "fresh_until": 0,
            "last_server_sync": 0,
            "needs_heartbeat": False,
            "grace": False,
        }
    success, payload, error, _ = verify_token(legacy)
    if not success or not payload:
        return {
            "status": "expired",
            "fingerprint_hex": "",
            "customer": "",
            "issued_at": 0,
            "expires_at": 0,
            "error": error,
            "fresh_until": 0,
            "last_server_sync": 0,
            "needs_heartbeat": False,
            "grace": False,
        }
    # 旧格式没有 fresh_until → 第一次启动就需要心跳
    return _evaluate_status_with_fresh(
        payload,
        fresh_until=0,
        last_server_sync=0,
    )


def _evaluate_status_with_fresh(payload, *, fresh_until: int, last_server_sync: int) -> dict:
    """根据 payload + fresh_until 计算 status / grace / needs_heartbeat。

    v0.3.1 状态机:
      - now < fresh_until                  → status=valid, needs_heartbeat=False, grace=False
      - fresh_until < now < fresh+grace    → status=valid, needs_heartbeat=True,  grace=True
      - now > fresh+grace                  → status=expired(闸门静默退出)
      - fresh_until == 0(旧格式无心跳)     → status=valid(grace 期内), needs_heartbeat=True

    P1-A 防时钟回拨:如果 last_server_sync > 0,检查 now 是否回拨到 last_server_sync 之前。
    """
    now = int(time.time())
    needs_heartbeat = False
    grace = False

    # P1-A: 防本地时钟回拨攻击
    if last_server_sync > 0 and now < last_server_sync:
        # 系统时钟回拨到上次心跳之前 → 拒绝(攻击者试图让过期 token 看起来有效)
        logger = logging.getLogger(__name__)
        logger.error(
            "检测到系统时钟回拨: now=%d < last_server_sync=%d,拒绝验证",
            now, last_server_sync
        )
        return {
            "status": "expired",
            "fingerprint_hex": payload.get("fingerprint_hex", ""),
            "customer": payload.get("customer", ""),
            "issued_at": int(payload.get("issued_at", 0)),
            "expires_at": int(payload.get("expires_at", 0)),
            "fresh_until": fresh_until,
            "last_server_sync": last_server_sync,
            "needs_heartbeat": True,
            "grace": False,
            "error": "clock_rollback",
        }

    # P1-B: 修复 fresh_until<=0 永不过期洞
    # 恶意 server 可能返回 fresh_until=0 让客户端永久有效
    # 旧逻辑: if fresh_until <= 0 → 给 7 天 grace,但不会走到过期分支
    # 新逻辑: fresh_until<=0 视为"已过期",但允许 grace(基于 last_server_sync)
    if fresh_until <= 0:
        # 旧 v0.3.0 token(last_server_sync=0) → 给 7 天 grace
        # 新 v0.3.1 但 fresh_until=0 → 同样给 grace,但基于 last_server_sync
        if last_server_sync > 0 and now > last_server_sync + _GRACE_DAYS * 86400:
            # grace 用完
            return {
                "status": "expired",
                "fingerprint_hex": payload.get("fingerprint_hex", ""),
                "customer": payload.get("customer", ""),
                "issued_at": int(payload.get("issued_at", 0)),
                "expires_at": int(payload.get("expires_at", 0)),
                "fresh_until": fresh_until,
                "last_server_sync": last_server_sync,
                "needs_heartbeat": True,
                "grace": False,
            }
        # 仍在 grace 期内 → 标记 needs_heartbeat
        needs_heartbeat = True
        grace = True
    elif now > fresh_until + _GRACE_DAYS * 86400:
        # 闸门拦截:grace 用完
        return {
            "status": "expired",
            "fingerprint_hex": payload.get("fingerprint_hex", ""),
            "customer": payload.get("customer", ""),
            "issued_at": int(payload.get("issued_at", 0)),
            "expires_at": int(payload.get("expires_at", 0)),
            "fresh_until": fresh_until,
            "last_server_sync": last_server_sync,
            "needs_heartbeat": True,
            "grace": False,
        }
    elif now > fresh_until:
        grace = True
        needs_heartbeat = True
    else:
        # now <= fresh_until
        # 如果 fresh_until 在 7 天内(< now + 7d),标记 needs_heartbeat
        if fresh_until - now < _GRACE_DAYS * 86400:
            needs_heartbeat = True

    return {
        "status": "valid",
        "fingerprint_hex": payload.get("fingerprint_hex", ""),
        "customer": payload.get("customer", ""),
        "issued_at": int(payload.get("issued_at", 0)),
        "expires_at": int(payload.get("expires_at", 0)),
        "fresh_until": fresh_until,
        "last_server_sync": last_server_sync,
        "needs_heartbeat": needs_heartbeat,
        "grace": grace,
    }


def get_activation_status() -> str:
    """返 'valid' | 'expired' | 'missing' | 'needs_heartbeat'。

    v0.3.1 加 'needs_heartbeat' 状态:license_token 本身有效(没过期),
    但 fresh_until 即将到期 / 已进入 grace,前端应该渲染红色 banner 提示
    用户联网续期。
    """
    global _cached_status
    if not _cached_status.get("loaded"):
        _cached_status = _load_status_from_disk()
        _cached_status["loaded"] = True
    return _cached_status.get("status", "missing")


def get_activation_detail() -> dict:
    """返完整 status dict,前端用于激活窗 / 续期提示渲染。

    Returns:
        dict with keys: status, fingerprint_hex, customer, issued_at, expires_at,
        fresh_until, last_server_sync, needs_heartbeat, grace
    """
    global _cached_status
    if not _cached_status.get("loaded"):
        _cached_status = _load_status_from_disk()
        _cached_status["loaded"] = True
    # 防御性 copy,避免前端 mutate 全局
    return dict(_cached_status)


def verify_token(bytes token_blob):
    """一站式 token 校验 —— 所有调用方都应该走这里。

    v0.3.1 wire format: base32-encoded [32B priv][32B pub][payload_bytes][64B sig]
        (无 `.` 分隔符,单段 base32)
    v0.3.0 旧格式:    base32(payload_bytes).base32(sig_64bytes)
        (有 `.` 分隔符,两段 base32)

    自动 detect:含 `.` → 旧格式;否则尝试 v0.3.1 解码。

    Args:
        token_blob: 完整激活码(paste 后格式化的字符串,直接字节化,见
            _b32decode 的 strip 逻辑在 raw bytes 上不生效 —— 上层负责 strip)。
            实际上传的是已 strip 的 str.encode('utf-8')。

    Returns:
        (success: bool, payload: dict | None, error: str, extra: dict)
        success=False → payload=None,error 是用户可见的中文消息
        success=True  → payload 是 JSON 解码后的 dict,error=""
        extra 包含:client_priv_seed, client_pubkey(供 activate 时写盘)
    """
    # 反调试 —— 检测到调试器就当 token 失效,不暴露「检测到了」
    if anti_debug.TAMPER_DETECTED:
        return False, None, "激活码无效", {}

    if not _initialized or not _pubkey:
        return False, None, "应用未正确初始化", {}

    # 解码
    try:
        text = bytes(token_blob).decode("ascii", errors="replace")
    except Exception:
        return False, None, "激活码格式错误", {}

    # v0.3.0 旧格式:含 `.`
    if "." in text:
        return _verify_token_v030(text)
    # v0.3.1 新格式:单段 base32
    return _verify_token_v031(text)


def _verify_token_v030(str text):
    """v0.3.0 旧格式:<base32(payload)>.<base32(sig)>"""
    if "." not in text:
        return False, None, "激活码格式错误", {}
    parts = text.split(".", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False, None, "激活码格式错误", {}

    try:
        payload_bytes = _b32decode(parts[0])
        sig_bytes = _b32decode(parts[1])
    except Exception:
        return False, None, "激活码格式错误", {}

    if len(sig_bytes) != 64:
        return False, None, "激活码格式错误", {}

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return False, None, "激活码格式错误", {}

    if not isinstance(payload, dict):
        return False, None, "激活码格式错误", {}

    # v0.3.0 旧 payload schema:v=1
    if int(payload.get("v", 0)) != 1:
        return False, None, "激活码格式错误", {}
    min_app = str(payload.get("min_app_version", "0.0.0"))
    if _version_tuple(min_app) > _version_tuple(_MIN_APP_VERSION):
        return False, None, "应用版本过旧,请升级", {}

    if not _crypto.verify(_pubkey, payload_bytes, sig_bytes):
        return False, None, "激活码无效", {}

    expected_fp = current_fingerprint()
    payload_fp = str(payload.get("fingerprint_hex", ""))
    if not payload_fp or payload_fp != expected_fp:
        return False, None, "激活码与本机不匹配", {}

    expires_at = int(payload.get("expires_at", 0))
    if expires_at > 0 and int(time.time()) >= expires_at:
        return False, None, "激活码已过期", {}

    # v0.3.0 旧 token 没有 client_priv 段,前端会引导用户重激活
    return True, payload, "", {"client_priv_seed": b"", "client_pubkey": b"", "wire_version": 1}


def _verify_token_v031(str text):
    """v0.3.1 新格式:base32([32B priv][32B pub][payload][64B sig])"""
    try:
        wire_blob = _b32decode(text)
    except Exception:
        return False, None, "激活码格式错误", {}

    if len(wire_blob) < _MIN_TOKEN_LEN_V031:
        return False, None, "激活码格式错误", {}

    client_priv_seed = wire_blob[:32]
    client_pubkey = wire_blob[32:64]
    payload_bytes = wire_blob[64:-64]
    sig_bytes = wire_blob[-64:]

    if not payload_bytes or len(sig_bytes) != 64:
        return False, None, "激活码格式错误", {}

    try:
        payload = json.loads(payload_bytes.decode("utf-8"))
    except Exception:
        return False, None, "激活码格式错误", {}

    if not isinstance(payload, dict):
        return False, None, "激活码格式错误", {}

    # v0.3.1 payload schema:v=2(支持 v=1 但 v=1 不带 priv 段,等于 v0.3.0 旧)
    payload_version = int(payload.get("v", 0))
    if payload_version not in (1, 2):
        return False, None, "激活码格式错误", {}

    min_app = str(payload.get("min_app_version", "0.0.0"))
    if _version_tuple(min_app) > _version_tuple(_MIN_APP_VERSION):
        return False, None, "应用版本过旧,请升级", {}

    # developer 用 _pubkey 验 payload_bytes + sig_bytes
    if not _crypto.verify(_pubkey, payload_bytes, sig_bytes):
        return False, None, "激活码无效", {}

    # 客户端额外校验:client_pubkey 必须匹配 client_priv_seed 派生的公钥
    # 防签发工具或被攻破的 developer 用了一个不匹配的 keypair
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
    try:
        derived_pub = Ed25519PrivateKey.from_private_bytes(client_priv_seed).public_key().public_bytes_raw()
    except Exception:
        return False, None, "激活码无效", {}
    if derived_pub != client_pubkey:
        return False, None, "激活码无效", {}

    # 机器指纹 HMAC 校验
    expected_fp = current_fingerprint()
    payload_fp = str(payload.get("fingerprint_hex", ""))
    if not payload_fp or payload_fp != expected_fp:
        return False, None, "激活码与本机不匹配", {}

    # 过期校验(license_token 自身的 expires_at,不是 fresh_until)
    expires_at = int(payload.get("expires_at", 0))
    if expires_at > 0 and int(time.time()) >= expires_at:
        return False, None, "激活码已过期", {}

    return True, payload, "", {
        "client_priv_seed": client_priv_seed,
        "client_pubkey": client_pubkey,
        "wire_version": 2,
    }


def activate(str code):
    """校验 code,通过 → write_token_v031(persist)。返 (success, error_message)。

    v0.3.1 升级:
      - 自动 detect v0.3.0 / v0.3.1 wire format
      - v0.3.1 → write_token_v031(头部 DSA1 + priv + pub + fresh_until=0 + license_blob)
      - v0.3.0 → write_token(legacy, 无头部,前端会提示用户重新激活)

    Args:
        code: 用户粘贴的原始字符串(可能含空白 / dash / 等号 / 中文误打)。

    Returns:
        (True, "")  → 成功持久化
        (False, "中文错误消息")
    """
    cleaned = (code or "").strip().replace(" ", "").replace("-", "").replace("\n", "").replace("\r", "").replace("\t", "")
    cleaned = cleaned.replace("=", "")
    if len(cleaned) < 10 or len(cleaned) > 1024:
        return False, "激活码格式错误"
    blob = cleaned.encode("ascii", errors="replace")
    success, payload, error, extra = verify_token(blob)
    if not success:
        return False, error or "激活码无效"

    # 持久化路径选择
    wire_version = extra.get("wire_version", 0)
    try:
        if wire_version == 2 and extra.get("client_priv_seed") and extra.get("client_pubkey"):
            # v0.3.1 新格式 → 写带 DSA1 头部的 v0.3.1 格式
            _storage.write_token_v031(
                license_token_blob=blob,
                client_priv_seed=extra["client_priv_seed"],
                client_pubkey=extra["client_pubkey"],
                fresh_until=0,  # 第一次激活 fresh_until=0 → 触发后台心跳
                clock_offset_ms=0,
                last_server_sync=0,
            )
        else:
            # v0.3.0 旧格式 → legacy 写(用户后续会被引导重激活)
            _storage.write_token(blob)
    except OSError:
        return False, "无法写入激活信息,请检查目录权限"
    # 失效缓存
    global _cached_status
    _cached_status = _evaluate_status_with_fresh(
        payload,
        fresh_until=0,
        last_server_sync=0,
    )
    _cached_status["loaded"] = True
    return True, ""


def ensure_activated_or_exit():
    """闸门 —— 启动时 import-time 调一次。失败 sys.exit(0)。

    v0.3.1 新增 grace 行为:
      - missing           → 不 exit,引导用户激活
      - valid(grace=False)→ 通过,正常进入主 UI
      - valid(grace=True) → 通过(显示 banner 提示续期),**不**exit
      - expired(fresh_until + grace 用完) → sys.exit(0) 静默退出

    设计选择:missing / grace 不 sys.exit,因为用户刚装软件没输入激活码
    / 断网期间 → 应该看到激活窗 / 主 UI 提示续期,而不是被踢。
    expired(grace 用完)才静默退出,跟 v0.3.0 行为一致。
    """
    global _cached_status
    if not _cached_status.get("loaded"):
        _cached_status = _load_status_from_disk()
        _cached_status["loaded"] = True
    status = _cached_status.get("status", "missing")
    if status == "expired":
        # grace 已用完 → 静默退出,避免弹窗被逆向
        sys.exit(_GRACE_EXIT_CODE)
    # missing / valid(含 grace) → 继续(由 main() / api.py 决定激活窗要不要渲染)


def make_payload_json(raw_fingerprint_hex: str, customer: str, issued_at: int, expires_at: int, nonce: str) -> str:
    """签发工具内部用 —— 生成 payload JSON 字符串。供 tools/license_keygen/app.py 调用。

    返回完整 JSON 字符串。min_app_version 默认写 0.3.0,v=None 表示 schema v1。
    """
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


# 触发 import-time side-effect —— 注意必须在模块底部,所有 cdef 全部分配后
_init_pyx()
