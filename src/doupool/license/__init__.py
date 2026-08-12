"""
v0.3.0:license 子包对外 API。

调用方应该 import from here,而不是 from doupool.license._license_verify
/ doupool.license.crypto etc. —— 这样调用方在 .pyd 缺失时仍能拿到 null
实现,不至于 throw ModuleNotFoundError 让前端 ambiguous。

未编译情形:.pyd 不在(sys.platform != win32 / 开发者没跑 setup.py /
MSVC 缺失) → 模块级 import 走可选分支,所有对外函数返降级值:
  - current_fingerprint() → "uncompiled"
  - get_activation_status() → "missing"
  - activate(code) → (False, "验证模块未编译")
  - ensure_activated_or_exit() → no-op
这样前端能跑起来,只是会卡在激活窗直到用户装回编译后的版本。
"""
from __future__ import annotations

import sys as _sys
from typing import Tuple as _Tuple

# 平台检查 —— Cython .pyd 只能 cp312-win_amd64 加载。其他平台一律跳过编译后模块
_IS_TARGET = (
    _sys.platform == "win32"
    and _sys.version_info >= (3, 12)
)

_verifier = None
_verify_failure: str = ""

try:
    if _IS_TARGET:
        from doupool.license import _license_verify as _verifier  # noqa: F401
except Exception as exc:
    _verify_failure = f"{type(exc).__name__}: {exc}"
    _verifier = None


# v0.3.1.1 monkey-patch 兜底:
# 当前 .pyd 编译产物里 _b32decode 的 ("=" * pad) 表达式被 Cython 字符串压缩
# 优化掉(运行时实际是 "")导致所有 base32 长度非 8 倍数的激活码被拒,报
# "激活码格式错误"。源码 + C 代码是对的(.pyx:104-107,.c:3783 PyNumber_Multiply),
# 重新 setup.py build_ext --inplace 出来的 .pyd 哈希与旧版 byte-for-byte 一致
# (137216 bytes, bc83bd98...) 说明 Cython 编译链某环节把 "=" 字符串压成了空串。
# 治本方案:查 Cython 字符串压缩 directive(见 TODO)。
# 治标方案:加载 .pyd 后立刻把 _b32decode 替换为纯 Python 实现,逻辑与 .pyx 源码
# 一致。安全保证:攻击者编辑 .pyd 也无效,因为 patch 在 .py 源码层,本来就是
# 明文的。后续真修复 .pyd 后这段可以删。
if _verifier is not None:
    import base64 as _b64
    _STALE_B32DECODE = _verifier._b32decode  # 留给未来诊断用的引用

    def _patched_b32decode(s):
        s = s.strip().replace(' ', '').replace('-', '').replace('=', '')
        pad = (-len(s)) % 8
        return _b64.b32decode(s + ('=' * pad))

    _verifier._b32decode = _patched_b32decode


def is_compiled() -> bool:
    """verifier .pyd 是否加载成功。前端 / 测试用它判断走 activate path vs 拒。"""
    return _verifier is not None


def current_fingerprint() -> str:
    if _verifier is None:
        return "uncompiled"
    try:
        return _verifier.current_fingerprint()
    except Exception:
        return "uncompiled"


def get_activation_status() -> str:
    """'valid' | 'expired' | 'missing' | 'uncompiled'。"""
    if _verifier is None:
        return "uncompiled"
    try:
        return _verifier.get_activation_status()
    except Exception:
        return "missing"


def activate(code: str) -> _Tuple[bool, str]:
    """返 (success, error_message)。"""
    if _verifier is None:
        return False, "验证模块未编译,请重新安装官方完整版软件"
    try:
        return _verifier.activate(code)
    except Exception as exc:
        return False, "激活失败,请重试"


def ensure_activated_or_exit() -> None:
    """import-time 闸门。缺 .pyd 不退出 —— 留给前端提示。"""
    if _verifier is None:
        return
    try:
        _verifier.ensure_activated_or_exit()
    except SystemExit:
        raise
    except Exception:
        return


__all__ = [
    "is_compiled",
    "current_fingerprint",
    "get_activation_status",
    "activate",
    "ensure_activated_or_exit",
]
