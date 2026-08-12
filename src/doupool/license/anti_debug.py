"""
v0.3.0:反调试 —— 威慑级,非绝对防护。

三个层面的检查(Windwos-only):
  1. IsDebuggerPresent          —— kernel32,几乎无开销
  2. CheckRemoteDebuggerPresent —— 也查外部 IDA / x64dbg
  3. NtQueryInformationProcess(ProcessDebugPort) —— 内核层面,绕过 1+2

策略:run_checks() 在 import-time 跑一次,设 TAMPER_DETECTED 全局标志。
verifier 看到 TAMPER_DETECTED → 抛"激活码无效"等价错误(不暴露自己)。

明显拥抱的局限(见 plan §H.6):
  - ScyllaHide 0 成本绕过
  - 硬件断点 / VMX / 改 .pyd 都能绕
  - 我们不假装"绝对防护",只把成本从 0 提到 5 分钟
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


# 模块级缓存 —— 跑一次就够了
TAMPER_DETECTED: bool = False


def _is_debugger_present() -> bool:
    """kernel32!IsDebuggerPresent —— 进程是否被调试器附加。"""
    try:
        return bool(ctypes.windll.kernel32.IsDebuggerPresent())
    except (AttributeError, OSError):
        return False


def _check_remote_debugger() -> bool:
    """kernel32!CheckRemoteDebuggerPresent —— 外部调试器(IDA / x64dbg)。"""
    try:
        kernel32 = ctypes.windll.kernel32
        # BOOL CheckRemoteDebuggerPresent(HANDLE Process, PBOOL DebuggerPresent)
        kernel32.CheckRemoteDebuggerPresent.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL)]
        kernel32.CheckRemoteDebuggerPresent.restype = wintypes.BOOL
        present = wintypes.BOOL(False)
        ok = kernel32.CheckRemoteDebuggerPresent(kernel32.GetCurrentProcess(), ctypes.byref(present))
        return bool(ok and present.value)
    except (AttributeError, OSError):
        return False


def _nt_query_debug_port() -> bool:
    """ntdll!NtQueryInformationProcess(ProcessDebugPort) —— 内核端口调试器 marker。"""
    try:
        ntdll = ctypes.windll.ntdll
        # 由于 ntdll 没有官方 type stubs,运行时手动声明
        ntdll.NtQueryInformationProcess.restype = ctypes.c_long
        ntdll.NtQueryInformationProcess.argtypes = [
            wintypes.HANDLE,
            ctypes.c_uint,
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
        ]
        # ProcessDebugPort = 7
        debug_port = ctypes.c_uint(0)
        ret_len = ctypes.c_ulong(0)
        status = ntdll.NtQueryInformationProcess(
            ctypes.windll.kernel32.GetCurrentProcess(),
            7,
            ctypes.byref(debug_port),
            ctypes.sizeof(debug_port),
            ctypes.byref(ret_len),
        )
        # STATUS_SUCCESS = 0,非 0 = 失败 === 未调试
        if status != 0:
            return False
        return debug_port.value != 0
    except (AttributeError, OSError):
        return False


def run_checks() -> bool:
    """跑全部检查,更新 TAMPER_DETECTED。返回当前是否被检测为调试。"""
    global TAMPER_DETECTED
    if sys.platform != "win32":
        TAMPER_DETECTED = False
        return False
    detected = _is_debugger_present() or _check_remote_debugger() or _nt_query_debug_port()
    TAMPER_DETECTED = detected
    return detected


def reset_for_test() -> None:
    """测试专用 —— 重置 TAMPER_DETECTED 标志(modify state,不要在生产里调)。"""
    global TAMPER_DETECTED
    TAMPER_DETECTED = False
