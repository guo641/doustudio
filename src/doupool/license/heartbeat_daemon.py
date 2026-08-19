"""
v0.3.1:后台心跳 daemon 线程。

每 24h 调一次 perform_handshake,成功 → update_heartbeat_fields 写盘。
失败 → 累计连续失败次数,3 次后标记 offline 但不阻塞。

**生命周期**:
  - main.py bootstrap_runtime 里 start() 启动一次
  - daemon=True 线程,主进程退出时自动杀
  - 不主动 stop()(进程退出时 OS 回收,简单可靠)

**为什么 daemon=True 而不是加 stop_event**:
  - 后台心跳 24h 才跑一次,进程退出时大概率在 sleep,没在握手
  - 加 stop_event 反而增加复杂度,得不偿失
  - 真要优雅退出可以 send SIGTERM → main.py 处理 → sys.exit
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from . import heartbeat as _hb
from . import storage as _storage

logger = logging.getLogger(__name__)

# 默认每 24h 一次(可被环境变量覆盖,测试用 10s)
DEFAULT_INTERVAL_SEC: int = int(
    __import__("os").environ.get(
        "DOUSTUDIO_HEARTBEAT_INTERVAL_SEC", str(24 * 3600)
    )
)
# 连续失败上限 → 标记 offline 状态
MAX_CONSECUTIVE_FAILURES: int = 3

_daemon_thread: Optional[threading.Thread] = None
_daemon_lock = threading.Lock()


def _daemon_loop(interval_sec: int) -> None:
    """daemon 主循环:间隔 interval_sec 跑一次 handshake。"""
    consecutive_failures = 0
    while True:
        # 睡 interval_sec,期间被 OS 杀就完事
        time.sleep(interval_sec)
        try:
            _run_one_heartbeat()
            consecutive_failures = 0
        except Exception as exc:
            consecutive_failures += 1
            logger.warning(
                "后台心跳失败 (%d/%d): %s",
                consecutive_failures, MAX_CONSECUTIVE_FAILURES, exc,
            )
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                # 不退出,继续重试(下次间隔会再 sleep)
                logger.warning("连续失败 %d 次,继续后台重试(不阻塞用户)", consecutive_failures)
                consecutive_failures = 0  # 重置避免日志刷屏


def _run_one_heartbeat() -> None:
    """读 activated.bin → 调 handshake → 成功就 update 字段。"""
    from . import reload_activation_status

    stored = _storage.read_token_v032()
    if stored is None:
        stored = _storage.read_token_v031()
    if stored is None:
        # v0.3.0 旧格式 / 无 token → 不跑心跳
        return

    fingerprint_hex = _current_fingerprint_hex()
    result = _hb.perform_handshake(
        license_token_hex=stored.license_token_blob.hex(),
        client_priv_seed=stored.client_priv_seed,
        fingerprint_hex=fingerprint_hex,
    )
    if not result.ok:
        if result.error_code == _hb.ErrCode.REVOKED:
            from .bootstrap import _mark_process_revoked

            _mark_process_revoked()
            _storage.mark_current_license_revoked(fingerprint_hex=fingerprint_hex)
            if reload_activation_status() != "revoked":
                _mark_process_revoked()
            logger.warning("后台心跳确认授权已撤销,已持久化本机撤销状态")
            return
        logger.info("后台心跳失败: %s", result.error_code)
        return

    # 成功 → 更新 activated.bin 的 heartbeat 字段
    _storage.update_heartbeat_fields_v032(
        fresh_until=result.fresh_until,
        clock_offset_ms=result.clock_offset_ms,
        last_server_sync=result.server_timestamp,
        revoked_prefixes=result.revoked_prefixes,
    )
    status_after_sync = reload_activation_status()
    logger.info(
        "后台心跳成功 fresh_until=%d offset=%dms revoked_count=%d status=%s",
        result.fresh_until,
        result.clock_offset_ms,
        len(result.revoked_prefixes),
        status_after_sync,
    )


def _current_fingerprint_hex() -> str:
    """读当前机器的 fingerprint hex(从 verifier 拿,verifier 是唯一信任根)。"""
    # 避免 import 循环,延迟 import
    from . import current_fingerprint as _cf
    return _cf()


def start(interval_sec: int = DEFAULT_INTERVAL_SEC) -> None:
    """启动 daemon 线程(只启动一次,重复调用 no-op)。"""
    global _daemon_thread
    with _daemon_lock:
        if _daemon_thread is not None and _daemon_thread.is_alive():
            logger.debug("daemon 已在跑,跳过")
            return
        _daemon_thread = threading.Thread(
            target=_daemon_loop,
            args=(interval_sec,),
            name="DouStudio-Heartbeat",
            daemon=True,
        )
        _daemon_thread.start()
        logger.info("后台心跳 daemon 已启动,interval=%ds", interval_sec)


def is_running() -> bool:
    """测试用:daemon 线程是否在跑。"""
    return _daemon_thread is not None and _daemon_thread.is_alive()
