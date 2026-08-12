"""
v0.3.0:license/fingerprint.py 测试。

覆盖:
  1. collect_raw / collect_hex 返回稳定 hash(同一 raw 同一 hex)
  2. mock PowerShell 输出后,fingerprint 按预期变化
  3. non-Windows 平台 → "non-windows" 占位
  4. hmac_hex 用 64-hex key 返 64-hex output
"""
from __future__ import annotations

import hashlib
from unittest.mock import patch

import pytest

from doupool.license import fingerprint


def test_collect_hex_is_64_chars_lowercase():
    """无论平台,hex 必须是 64 个小写 hex 字符(SHA-256 输出)。"""
    fingerprint.reset_cache()
    h = fingerprint.collect_hex()
    assert len(h) == 64
    assert all(c in "0123456789abcdef" for c in h)


def test_collect_hex_caches():
    """连续调用返同一结果(模块级缓存)。"""
    fingerprint.reset_cache()
    h1 = fingerprint.collect_hex()
    h2 = fingerprint.collect_hex()
    assert h1 == h2


def test_collect_raw_stable_across_calls():
    """raw 缓存:reset 之外,同一进程不变。"""
    fingerprint.reset_cache()
    r1 = fingerprint.collect_raw()
    r2 = fingerprint.collect_raw()
    assert r1 == r2


def test_mocked_powershell_output_changes_hash():
    """mock 调 PowerShell,改 1 字节 → 整个 hex 改变。"""
    fingerprint.reset_cache()
    with patch("doupool.license.fingerprint._collect_raw") as mock:
        mock.return_value = "FAKE-UUID-001"
        fingerprint.reset_cache()
        h1 = fingerprint.collect_hex()
        assert h1 == hashlib.sha256(b"FAKE-UUID-001").hexdigest()

        mock.return_value = "FAKE-UUID-002"  # 改 1 字节
        fingerprint.reset_cache()
        h2 = fingerprint.collect_hex()
        assert h2 != h1


def test_non_windows_returns_marker():
    """模拟非 Windows 平台 → collect_raw 返 "non-windows"。"""
    fingerprint.reset_cache()
    with patch("doupool.license.fingerprint.platform.system", return_value="Linux"):
        r = fingerprint._collect_raw()
    assert r == "non-windows"
    fingerprint.reset_cache()


def test_hmac_hex_format():
    """hmac_hex 必返 64 hex chars。"""
    out = fingerprint.hmac_hex("aa" * 32)
    assert len(out) == 64
    assert all(c in "0123456789abcdef" for c in out)


def test_hmac_hex_different_keys_yield_different_output():
    a = fingerprint.hmac_hex("aa" * 32)
    b = fingerprint.hmac_hex("bb" * 32)
    assert a != b


def test_fingerprint_hmac_format():
    """fingerprint_hmac 必须返 64 hex chars —— 签发工具对外契约。"""
    fingerprint.reset_cache()
    raw = fingerprint.collect_raw()
    out = fingerprint.fingerprint_hmac(raw)
    assert len(out) == 64
    assert all(c in "0123456789abcdef" for c in out)


def test_fingerprint_hmac_rejects_none():
    """raw_fingerprint 为 None → 立刻 ValueError,不悄悄兜底。"""
    try:
        fingerprint.fingerprint_hmac(None)  # type: ignore[arg-type]
    except ValueError:
        return
    raise AssertionError("expected ValueError for None raw_fingerprint")


def test_fingerprint_hmac_matches_hmac_hex():
    """v0.3.0 关键不变量:签发端 fingerprint_hmac(raw) 必须跟 verifier 用的
    hmac_hex(pubkey_hex) 产出 bit-for-bit 等价 —— 两者都使用 _pubkey.hex()
    后 64-char ASCII 作 HMAC key,等价于 HMAC(pubkey_hex_ascii, raw)。

    真实路径:
      - verifier(`verifier.pyx:119`):`pubkey_hex = _pubkey.hex()`,再调
        `hmac_hex(pubkey_hex)` → `HMAC(pubkey_hex_ascii, raw)`
      - keygen(`fingerprint.py:fingerprint_hmac`):`_decoded_pubkey().hex()`
        → `HMAC(pubkey_hex_ascii, raw)`

    这条不变量保证 self-test 链路通:keygen 在开发者机器上算出本机 fingerprint,
    跟 verifier 在同一台机器上算出来的 fingerprint 一致,签出的码能自激活。
    """
    import hashlib
    import hmac as _hmac

    import pytest

    raw = "uuid-1234|board-5678|cpuid-9abc"
    pubkey_32 = bytes(range(32))
    pubkey_hex = pubkey_32.hex()

    monkey = pytest.MonkeyPatch()
    try:
        # 让两条路径都拿到同一段 raw + 同一段 pubkey
        monkey.setattr(fingerprint, "collect_raw", lambda: raw)
        monkey.setattr(fingerprint, "_decoded_pubkey", lambda: pubkey_32)

        # 路径 1:verifier 端 → hmac_hex(pubkey_hex_ascii)
        via_hmac_hex = fingerprint.hmac_hex(pubkey_hex)

        # 路径 2:签发端 → fingerprint_hmac(raw),内部 _decoded_pubkey().hex()
        via_fingerprint_hmac = fingerprint.fingerprint_hmac(raw)

        # 期望值:HMAC(pubkey_hex_ascii, raw_bytes)
        expected = _hmac.new(
            pubkey_hex.encode("ascii"),
            raw.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

        assert via_hmac_hex == expected
        assert via_fingerprint_hmac == expected
        # 关键:两条路径产出完全一致 —— 这就是「签发端的 HMAC 在 verifier 端能验过」的保证
        assert via_fingerprint_hmac == via_hmac_hex
    finally:
        monkey.undo()


def test_both_returns_tuple():
    """both() 返 (raw, hex) 元组,长度合理。"""
    fingerprint.reset_cache()
    raw, hx = fingerprint.both()
    assert isinstance(raw, str)
    assert isinstance(hx, str)
    assert len(hx) == 64
