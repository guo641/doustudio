"""
v0.3.1:启动时一次性握手 — 在主 UI 渲染前调一次 perform_handshake。

**为什么这是单独的模块(而不是塞在 main.py)**:
  - main.py 已经导 doupool.license.verify_at_import 触发闸门;
  - 启动握手跟闸门**逻辑上独立** — 闸门问"是否过期",握手问"服务器有没有撤销我"。
  - 抽到 license 子包内,方便测试 + 复用(daemon 之后调同一个 wrapper)。

**失败语义**:
  - 网络抽风 / server 不可达 → log warning → 进程继续(grace 7d 兜底)
  - revoked prefix 命中 → log error → 仍让用户进 UI(闸门只看 expires_at)
  - 协议错 / 验签失败 → log error → 进程继续(下次握手修复)
**绝不** sys.exit:用户手里可能有 7 天 grace,不应被网络抽风踢出。

**调用顺序**(main.py):
  1. import verify_at_import    → 闸门(expired 静默 exit)
  2. import bootstrap (no-op)   → 仅函数定义,不触发
  3. configure_runtime_environment (paths)
  4. bootstrap.run_startup_handshake() → 同步握手,失败也继续
  5. heartbeat_daemon.start()  → 后台 daemon 线程,再 24h 一次
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)


def run_startup_handshake() -> None:
    """启动握手 — 同步,主 UI 渲染前调一次。

    流程:
      1. 读 activated.bin (v0.3.1 schema)
      2. 没 v0.3.1 token → 跳过(用户未激活 / v0.3.0 旧码,稍后由 UI 引导重激活)
      3. 调 perform_handshake → 成功 update_heartbeat_fields
      4. 任何失败 → log,不阻塞
    """
    # 延迟 import — 避免循环 import:
    #   bootstrap → heartbeat → storage → verifier → _license_verify.pyd
    #   自从 verify_at_import 已经先 import doupool.license,verifier 已经加载
    from doupool.license import current_fingerprint, get_activation_status
    from doupool.license import heartbeat as _hb
    from doupool.license import storage as _storage

    # 闸门拦截:status 已是 expired / missing → 不发请求(节流)
    status = get_activation_status()
    if status in ("expired", "missing", "uncompiled"):
        logger.debug("启动握手跳过:status=%s", status)
        return

    # v0.3.1 路径才需要 priv_seed / heartbeat_fields
    stored = _storage.read_token_v031()
    if stored is None:
        # v0.3.0 旧码 / 无 token → UI 会引导重激活,不发心跳
        logger.debug("启动握手跳过:不是 v0.3.1 schema")
        return

    fp = current_fingerprint()
    if fp == "uncompiled" or not fp:
        # .pyd 没编(开发机 / 跨平台) → 跳过心跳(也不该有 v0.3.1 落盘文件)
        logger.warning("verifier 未编译,启动握手跳过")
        return

    try:
        result = _hb.perform_handshake(
            license_token_hex=stored.license_token_blob.hex(),
            client_priv_seed=stored.client_priv_seed,
            fingerprint_hex=fp,
        )
    except Exception as exc:
        logger.warning("启动握手异常: %s", exc)
        return

    if not result.ok:
        # 启动期失败是常态(离线 / network_error / revoked 都算 ok 失败)
        logger.info("启动握手失败: %s (grace 7d 兜底,不阻塞)", result.error_code)
        return

    try:
        _storage.update_heartbeat_fields(
            fresh_until=result.fresh_until,
            clock_offset_ms=result.clock_offset_ms,
            last_server_sync=result.server_timestamp or int(time.time()),
        )
        logger.info(
            "启动握手成功: fresh_until=%s offset=%dms",
            result.fresh_until, result.clock_offset_ms,
        )
    except Exception as exc:
        # 写盘失败不应该阻塞主 UI — 启动握手只是 perf 优化,下次 daemon 还会试
        logger.warning("启动握手写盘失败: %s", exc)
