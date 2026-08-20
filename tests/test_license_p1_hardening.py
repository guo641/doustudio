"""
P1 可选加固回归测试 — v0.3.9 Phase-2 后续加固。

测试三项 P1 修复:
  1. 防本地时钟回拨 (verifier.pyx)
  2. fresh_until<=0 永不过期洞 (verifier.pyx)
  3. 响应 client_pubkey 回显显式校验 (heartbeat.py)
"""
from __future__ import annotations

import time
from unittest.mock import patch

import pytest


def test_clock_rollback_is_rejected_during_verification(monkeypatch):
    """P1-A: 系统时钟回拨到 last_server_sync 之前 → status=expired。"""
    pytest.importorskip("doupool.license._license_verify", reason="需要编译的 .pyd")
    from doupool.license import _license_verify as verifier

    # 伪造 payload + last_server_sync=1000000000 (2001-09-09)
    payload = {
        "fingerprint_hex": "aa" * 32,
        "customer": "test-customer",
        "issued_at": 1000000000,
        "expires_at": 2000000000,
    }
    last_server_sync = 1000000000  # 上次心跳时间
    now_rollback = 999990000       # 系统时钟回拨 10000 秒

    # monkeypatch time.time 返回回拨后的时间
    monkeypatch.setattr(time, "time", lambda: now_rollback)

    result = verifier._evaluate_status_with_fresh(
        payload,
        fresh_until=1000100000,  # 未来,本应 valid
        last_server_sync=last_server_sync,
    )

    assert result["status"] == "expired", "时钟回拨应触发 expired"
    assert result.get("error") == "clock_rollback"
    assert result["needs_heartbeat"] is True


def test_small_clock_skew_within_tolerance_is_allowed(monkeypatch):
    """v0.3.13: 本地时钟比 last_server_sync 慢几秒时不应误判 expired。"""
    pytest.importorskip("doupool.license._license_verify", reason="需要编译的 .pyd")
    from doupool.license import _license_verify as verifier

    payload = {
        "fingerprint_hex": "cc" * 32,
        "customer": "test-customer",
        "issued_at": 1000000000,
        "expires_at": 2000000000,
    }
    last_server_sync = 1000000000
    now_slightly_behind = last_server_sync - 2

    monkeypatch.setattr(time, "time", lambda: now_slightly_behind)

    result = verifier._evaluate_status_with_fresh(
        payload,
        fresh_until=1000100000,
        last_server_sync=last_server_sync,
    )

    assert result["status"] == "valid", "几秒时钟漂移不应误判过期"
    assert result.get("error") != "clock_rollback"


def test_fresh_until_zero_with_expired_grace_period(monkeypatch):
    """P1-B: fresh_until=0 + last_server_sync 超过 7 天 → status=expired。

    旧逻辑: fresh_until<=0 永远给 grace,永不过期
    新逻辑: fresh_until<=0 视为"已过期",基于 last_server_sync 计算 grace
    """
    pytest.importorskip("doupool.license._license_verify", reason="需要编译的 .pyd")
    from doupool.license import _license_verify as verifier

    payload = {
        "fingerprint_hex": "bb" * 32,
        "customer": "test-customer",
        "issued_at": 1000000000,
        "expires_at": 2000000000,
    }
    last_server_sync = 1000000000  # 7 天前
    now = last_server_sync + 8 * 86400  # 8 天后,grace 用完

    monkeypatch.setattr(time, "time", lambda: now)

    result = verifier._evaluate_status_with_fresh(
        payload,
        fresh_until=0,  # 恶意 server 返回 0
        last_server_sync=last_server_sync,
    )

    assert result["status"] == "expired", "fresh_until=0 且 grace 用完应 expired"
    assert result["needs_heartbeat"] is True
    assert result["grace"] is False


def test_fresh_until_zero_within_grace_period_allows_temporary_use(monkeypatch):
    """P1-B: fresh_until=0 但 last_server_sync 在 7 天内 → status=valid,grace=True。"""
    pytest.importorskip("doupool.license._license_verify", reason="需要编译的 .pyd")
    from doupool.license import _license_verify as verifier

    payload = {
        "fingerprint_hex": "cc" * 32,
        "customer": "test-customer",
        "issued_at": 1000000000,
        "expires_at": 2000000000,
    }
    last_server_sync = 1000000000
    now = last_server_sync + 3 * 86400  # 3 天后,仍在 grace

    monkeypatch.setattr(time, "time", lambda: now)

    result = verifier._evaluate_status_with_fresh(
        payload,
        fresh_until=0,
        last_server_sync=last_server_sync,
    )

    assert result["status"] == "valid", "仍在 grace 期内应放行"
    assert result["needs_heartbeat"] is True
    assert result["grace"] is True


def test_fresh_until_zero_without_sync_uses_issued_at_anchor(monkeypatch):
    """P1-B: 首次心跳前 last_server_sync=0 也必须按 issued_at 结束 grace。"""
    pytest.importorskip("doupool.license._license_verify", reason="需要编译的 .pyd")
    from doupool.license import _license_verify as verifier

    issued_at = 1000000000
    payload = {
        "fingerprint_hex": "dd" * 32,
        "customer": "test-customer",
        "issued_at": issued_at,
        "expires_at": 2000000000,
    }

    monkeypatch.setattr(time, "time", lambda: issued_at + 8 * 86400)

    result = verifier._evaluate_status_with_fresh(
        payload,
        fresh_until=0,
        last_server_sync=0,
    )

    assert result["status"] == "expired"
    assert result["needs_heartbeat"] is True
    assert result["grace"] is False


def test_fresh_until_zero_without_sync_allows_issued_at_grace(monkeypatch):
    """P1-B: 首次心跳前仍保留 issued_at 后 7 天内的离线 grace。"""
    pytest.importorskip("doupool.license._license_verify", reason="需要编译的 .pyd")
    from doupool.license import _license_verify as verifier

    issued_at = 1000000000
    payload = {
        "fingerprint_hex": "ee" * 32,
        "customer": "test-customer",
        "issued_at": issued_at,
        "expires_at": 2000000000,
    }

    monkeypatch.setattr(time, "time", lambda: issued_at + 3 * 86400)

    result = verifier._evaluate_status_with_fresh(
        payload,
        fresh_until=0,
        last_server_sync=0,
    )

    assert result["status"] == "valid"
    assert result["needs_heartbeat"] is True
    assert result["grace"] is True


def test_response_client_pubkey_mismatch_is_rejected():
    """P1-C: 响应里的 client_pubkey 与请求不匹配 → bad_response。"""
    from doupool.license import heartbeat

    expected_pubkey = "aa" * 32
    nonce = b"n" * 16

    response = {
        "server_timestamp": int(time.time()),
        "nonce": nonce.hex(),
        "client_pubkey": "bb" * 32,  # 不匹配
        "server_sig": "00" * 64,
        "fresh_until": int(time.time()) + 86400,
        "revoked_prefixes": [],
    }

    ok, error = heartbeat._verify_server_response(
        response,
        expected_client_pubkey_hex=expected_pubkey,
        expected_nonce=nonce,
        client_local_time=int(time.time()),
    )

    assert not ok, "client_pubkey 不匹配应拒绝"
    assert error == heartbeat.ErrCode.BAD_RESPONSE


def test_response_client_pubkey_match_passes_echo_check(monkeypatch):
    """P1-C: 响应里的 client_pubkey 匹配请求 → 通过回显校验(后续可能因签名失败)。"""
    from doupool.license import heartbeat

    # 跳过签名验证,只测回显
    monkeypatch.setattr(heartbeat, "_SERVER_PUBKEY", b"")
    monkeypatch.setenv("DOUSTUDIO_DEV_SKIP_SIG", "1")

    expected_pubkey = "aa" * 32
    nonce = b"n" * 16

    response = {
        "server_timestamp": int(time.time()),
        "nonce": nonce.hex(),
        "client_pubkey": expected_pubkey,  # 匹配
        "server_sig": "00" * 64,
        "fresh_until": int(time.time()) + 86400,
        "revoked_prefixes": [],
    }

    ok, error = heartbeat._verify_server_response(
        response,
        expected_client_pubkey_hex=expected_pubkey,
        expected_nonce=nonce,
        client_local_time=int(time.time()),
    )

    assert ok, "client_pubkey 匹配 + skip_sig 应通过"
    assert error == ""


def test_response_missing_client_pubkey_is_rejected():
    """P1-C: 响应缺少 client_pubkey 字段 → bad_response。"""
    from doupool.license import heartbeat

    expected_pubkey = "aa" * 32
    nonce = b"n" * 16

    response = {
        "server_timestamp": int(time.time()),
        "nonce": nonce.hex(),
        # client_pubkey 缺失
        "server_sig": "00" * 64,
        "fresh_until": int(time.time()) + 86400,
        "revoked_prefixes": [],
    }

    ok, error = heartbeat._verify_server_response(
        response,
        expected_client_pubkey_hex=expected_pubkey,
        expected_nonce=nonce,
        client_local_time=int(time.time()),
    )

    assert not ok, "缺少 client_pubkey 应拒绝"
    assert error == heartbeat.ErrCode.BAD_RESPONSE
