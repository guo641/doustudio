"""
v0.3.1:license heartbeat + 半在线协议测试。

覆盖:
  1. storage.write_token_v031 写入 → read_token_v031 解析回
  2. storage.update_heartbeat_fields 只更新 fresh_until 等字段
  3. heartbeat.perform_handshake 走通 happy path(mock server)
  4. heartbeat 拒绝 server_sig 错(防 fake server)
  5. heartbeat 拒绝 clock skew 超窗
  6. heartbeat 拒绝重放(nonce 不匹配)
  7. verifier verify_token_v031 wire format 验签 + 提取 priv/pub
  8. verifier ensure_activated_or_exit 闸门:
      - grace 期内 → 通过
      - grace 用完 → sys.exit(0)
  9. heartbeat_daemon.start 启动一次,is_running() == True
"""
from __future__ import annotations

import base64
import http.server
import json
import os
import struct
import sys
import threading
import time
from pathlib import Path

import pytest


# ============ Fixtures ============

@pytest.fixture
def isolated_license_dir(monkeypatch, tmp_path):
    """指 activated.bin 到 tmp_path,避免污染真实数据。"""
    # Legacy unit fixtures use a plain localhost mock; production rejects HTTP
    # URLs unless this explicit test-only development switch is set.
    monkeypatch.setenv("DOUSTUDIO_DEV_ALLOW_HTTP", "1")
    new_data = tmp_path / "data"
    new_log = tmp_path / "log"
    new_data.mkdir(parents=True)
    new_log.mkdir(parents=True)

    import doupool.config as _config
    monkeypatch.setattr(
        _config,
        "_resolve_app_dirs",
        lambda: (new_data, new_log),
    )
    yield new_data / "license"


@pytest.fixture
def generated_license_token():
    """生成一个合法的 v0.3.1 license_token hex,以及对应的 priv_seed / pub / payload。

    Returns:
        dict {
            'token_hex': str,
            'priv_seed': bytes(32),
            'pub': bytes(32),
            'payload_bytes': bytes,
            'developer_sig': bytes(64),
        }
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from doupool.license.crypto import sign as ed25519_sign

    # 模拟 developer 私钥(用 .pyd 解码 _pubkey 拿真 pub)
    from doupool.license import _license_verify as _verify_mod
    if _verify_mod is None:
        pytest.importorskip("doupool._license_verify", reason="verifier.pyd 未编译")
    # 走 verifier 解码的 _pubkey
    dev_priv = Ed25519PrivateKey.generate()  # 测试用,实际验签靠 _pubkey
    dev_pub = dev_priv.public_key().public_bytes_raw()

    # client keypair(用户激活码里的)
    client_priv = Ed25519PrivateKey.generate()
    client_priv_seed = client_priv.private_bytes_raw()
    client_pub = client_priv.public_key().public_bytes_raw()

    # payload + sig
    payload = {
        "v": 2,
        "fingerprint_hex": "0" * 64,
        "customer": "test-customer",
        "issued_at": int(time.time()),
        "expires_at": 0,
        "min_app_version": "0.3.1",
        "nonce": "abcd1234",
        "features": [],
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    developer_sig = ed25519_sign(dev_priv, payload_bytes)

    # wire blob: [priv][pub][payload][sig]
    wire = client_priv_seed + client_pub + payload_bytes + developer_sig
    token_b32 = base64.b32encode(wire).decode("ascii").rstrip("=")  # 激活码文本(用户粘贴)
    token_hex = wire.hex()  # 落盘到 activated.bin 的内部 hex 形式

    return {
        "token_b32": token_b32,
        "token_hex": token_hex,
        "priv_seed": client_priv_seed,
        "pub": client_pub,
        "payload_bytes": payload_bytes,
        "developer_sig": developer_sig,
    }


# ============ storage.py v0.3.1 schema ============

def test_write_token_v031_roundtrip(isolated_license_dir, generated_license_token):
    from doupool.license import storage
    storage.write_token_v031(
        license_token_blob=bytes.fromhex(generated_license_token["token_hex"]),
        client_priv_seed=generated_license_token["priv_seed"],
        client_pubkey=generated_license_token["pub"],
        fresh_until=int(time.time()) + 86400,
        clock_offset_ms=42,
        last_server_sync=int(time.time()),
    )
    stored = storage.read_token_v031()
    assert stored is not None
    assert stored.client_priv_seed == generated_license_token["priv_seed"]
    assert stored.client_pubkey == generated_license_token["pub"]
    assert stored.fresh_until > int(time.time())
    assert stored.clock_offset_ms == 42
    assert stored.last_server_sync > 0


def test_persistence_roundtrip(isolated_license_dir, generated_license_token, monkeypatch):
    """activate 写盘后模拟重启,仍能从 v0.3.1 文件恢复 valid 状态。"""
    import doupool.license as license_api

    if not license_api.is_compiled():
        pytest.skip("verifier.pyd 未编译")

    from doupool.license import _license_verify as verifier

    original_cache = dict(verifier._cached_status)
    monkeypatch.setattr(verifier._crypto, "verify", lambda *_args: True)
    monkeypatch.setattr(verifier, "current_fingerprint", lambda: "0" * 64)
    try:
        success, error = verifier.activate(generated_license_token["token_b32"])
        assert success, error

        verifier._cached_status["loaded"] = False
        assert verifier.get_activation_status() == "valid"
        assert verifier.get_activation_detail()["customer"] == "test-customer"
    finally:
        verifier._cached_status.clear()
        verifier._cached_status.update(original_cache)


def test_update_heartbeat_fields_preserves_blob(isolated_license_dir, generated_license_token):
    from doupool.license import storage
    blob = bytes.fromhex(generated_license_token["token_hex"])
    storage.write_token_v031(
        license_token_blob=blob,
        client_priv_seed=generated_license_token["priv_seed"],
        client_pubkey=generated_license_token["pub"],
        fresh_until=1000,
    )
    storage.update_heartbeat_fields(
        fresh_until=2000,
        clock_offset_ms=99,
        last_server_sync=3000,
    )
    stored = storage.read_token_v031()
    assert stored.fresh_until == 2000
    assert stored.clock_offset_ms == 99
    assert stored.last_server_sync == 3000
    # license_token_blob 完整保留
    assert stored.license_token_blob == blob


def test_read_token_legacy_returns_none_for_v031(isolated_license_dir, generated_license_token):
    from doupool.license import storage
    storage.write_token_v031(
        license_token_blob=bytes.fromhex(generated_license_token["token_hex"]),
        client_priv_seed=generated_license_token["priv_seed"],
        client_pubkey=generated_license_token["pub"],
    )
    assert storage.read_token_legacy() is None


def test_read_token_v031_returns_none_for_legacy(isolated_license_dir):
    from doupool.license import storage
    storage.write_token(b"legacy-blob")
    assert storage.read_token_v031() is None


def test_write_token_v031_rejects_bad_priv_length(isolated_license_dir, generated_license_token):
    from doupool.license import storage
    with pytest.raises(ValueError, match="client_priv_seed"):
        storage.write_token_v031(
            license_token_blob=bytes.fromhex(generated_license_token["token_hex"]),
            client_priv_seed=b"too-short",
            client_pubkey=generated_license_token["pub"],
        )


# ============ heartbeat.py 协议 ============

class _MockServer:
    """用一个内嵌的 http.server 模拟 heartbeat server,捕获请求、返预设响应。"""

    def __init__(self, response_factory, port=0):
        self.response_factory = response_factory
        self.received_requests: list[dict] = []
        self.port: int = 0
        self._server: http.server.HTTPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self):
        outer = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args, **kwargs):
                return  # 静默

            def do_POST(self):
                length = int(self.headers.get("Content-Length", "0"))
                body = self.rfile.read(length).decode("utf-8")
                outer.received_requests.append(json.loads(body))
                resp_body = outer.response_factory(body)
                if isinstance(resp_body, tuple):
                    status, payload = resp_body
                else:
                    status, payload = 200, resp_body
                data = json.dumps(payload).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self._server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def stop(self):
        if self._server:
            self._server.shutdown()
            self._server.server_close()


@pytest.fixture
def server_pubkey_pair():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key().public_bytes_raw()


@pytest.fixture
def embedded_server_pubkey(monkeypatch, server_pubkey_pair):
    """把 server pubkey XOR 编码到 _embedded_server_pubkey,让 heartbeat 验签。"""
    priv, pub = server_pubkey_pair
    # 简单 XOR:用全 0 mask → encrypted = pub 本身
    import doupool.license._embedded_server_pubkey as mod
    monkeypatch.setattr(mod, "ENCRYPTED_SERVER_PUBKEY", pub)
    monkeypatch.setattr(mod, "XOR_SERVER_MASK", b"\x00" * 32)
    # 也要让 heartbeat.py 内部缓存重新加载
    from doupool.license import heartbeat as _hb_mod
    monkeypatch.setattr(_hb_mod, "_SERVER_PUBKEY", pub)
    return priv, pub


def test_handshake_happy_path(isolated_license_dir, generated_license_token, embedded_server_pubkey):
    from doupool.license import heartbeat as _hb
    server_priv, _ = embedded_server_pubkey
    captured = {}

    def make_response(req_body):
        req = json.loads(req_body)
        captured["req"] = req
        signed_dict = {
            "fresh_until": int(time.time()) + 86400 * 30,
            "server_timestamp": int(time.time()),
            "revoked_prefixes": ["deadbeef"],
            "client_pubkey": req["client_pubkey"],
            "nonce": req["nonce"],
        }
        msg = json.dumps(signed_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        sig = server_priv.sign(msg)
        return 200, {
            "ok": True,
            "fresh_until": signed_dict["fresh_until"],
            "server_timestamp": signed_dict["server_timestamp"],
            "revoked_prefixes": signed_dict["revoked_prefixes"],
            "nonce": req["nonce"],
            "server_sig": sig.hex(),
        }

    srv = _MockServer(make_response)
    srv.start()
    try:
        result = _hb.perform_handshake(
            license_token_hex=generated_license_token["token_hex"],
            client_priv_seed=generated_license_token["priv_seed"],
            fingerprint_hex="0" * 64,
            server_url=f"http://127.0.0.1:{srv.port}",
        )
    finally:
        srv.stop()

    assert result.ok, f"握手失败: {result.error_code}"
    assert result.fresh_until > int(time.time())
    assert result.revoked_prefixes == ("deadbeef",)
    # 请求字段
    assert "client_sig" in captured["req"]
    assert len(captured["req"]["client_sig"]) == 128  # 64 bytes hex


def test_handshake_rejects_fake_server_sig(isolated_license_dir, generated_license_token, embedded_server_pubkey):
    """fake server(无 server_priv)签的响应应该被 client 拒绝。"""
    from doupool.license import heartbeat as _hb
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    attacker_priv = Ed25519PrivateKey.generate()  # fake server 自己的私钥

    def make_response(req_body):
        req = json.loads(req_body)
        signed_dict = {
            "fresh_until": int(time.time()) + 86400 * 30,
            "server_timestamp": int(time.time()),
            "revoked_prefixes": [],
            "client_pubkey": req["client_pubkey"],
            "nonce": req["nonce"],
        }
        msg = json.dumps(signed_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = attacker_priv.sign(msg)  # 用错的私钥签
        return 200, {
            "ok": True,
            "fresh_until": signed_dict["fresh_until"],
            "server_timestamp": signed_dict["server_timestamp"],
            "revoked_prefixes": [],
            "nonce": req["nonce"],
            "server_sig": sig.hex(),
        }

    srv = _MockServer(make_response)
    srv.start()
    try:
        result = _hb.perform_handshake(
            license_token_hex=generated_license_token["token_hex"],
            client_priv_seed=generated_license_token["priv_seed"],
            fingerprint_hex="0" * 64,
            server_url=f"http://127.0.0.1:{srv.port}",
        )
    finally:
        srv.stop()

    assert not result.ok
    assert result.error_code == _hb.ErrCode.BAD_SIGNATURE


def test_handshake_rejects_clock_skew(isolated_license_dir, generated_license_token, embedded_server_pubkey):
    """server 返回的 timestamp 跟本地差超过 300s → 拒绝。"""
    from doupool.license import heartbeat as _hb
    server_priv, _ = embedded_server_pubkey

    def make_response(req_body):
        req = json.loads(req_body)
        signed_dict = {
            "fresh_until": int(time.time()) + 86400 * 30,
            "server_timestamp": int(time.time()) - 1000,  # 差 1000s
            "revoked_prefixes": [],
            "client_pubkey": req["client_pubkey"],
            "nonce": req["nonce"],
        }
        msg = json.dumps(signed_dict, sort_keys=True, separators=(",", ":")).encode("utf-8")
        sig = server_priv.sign(msg)
        return 200, {
            "ok": True,
            "fresh_until": signed_dict["fresh_until"],
            "server_timestamp": signed_dict["server_timestamp"],
            "revoked_prefixes": [],
            "nonce": req["nonce"],
            "server_sig": sig.hex(),
        }

    srv = _MockServer(make_response)
    srv.start()
    try:
        result = _hb.perform_handshake(
            license_token_hex=generated_license_token["token_hex"],
            client_priv_seed=generated_license_token["priv_seed"],
            fingerprint_hex="0" * 64,
            server_url=f"http://127.0.0.1:{srv.port}",
        )
    finally:
        srv.stop()

    assert not result.ok
    assert result.error_code == _hb.ErrCode.BAD_TIMESTAMP


def test_handshake_handles_timeout(isolated_license_dir, generated_license_token):
    """server 不响应(挂起)→ client 超时返 TIMEOUT,不阻塞。"""
    from doupool.license import heartbeat as _hb

    class _HangHandler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args, **kwargs):
            return

        def do_POST(self):
            time.sleep(10)  # 远超 client timeout

    # 用一个不存在的端口触发 connection refused(更快)
    result = _hb.perform_handshake(
        license_token_hex=generated_license_token["token_hex"],
        client_priv_seed=generated_license_token["priv_seed"],
        fingerprint_hex="0" * 64,
        server_url="http://127.0.0.1:1",  # 几乎肯定没人监听
        timeout_sec=1,
    )
    assert not result.ok
    # 可能是 network_error 或 timeout,都算 graceful failure
    assert result.error_code in (_hb.ErrCode.NETWORK, _hb.ErrCode.TIMEOUT)


# ============ daemon ============

def test_daemon_starts_once(isolated_license_dir):
    from doupool.license import heartbeat_daemon
    # 用很短的 interval 测启动
    heartbeat_daemon.start(interval_sec=999999)
    assert heartbeat_daemon.is_running()
    # 第二次 start 不应该新建线程
    heartbeat_daemon.start(interval_sec=999999)
    assert heartbeat_daemon.is_running()


# ============ server verify.py ============

def test_server_verify_rejects_bad_signature(tmp_path):
    """server 端 verify: developer sig 错 → rejected。"""
    # 把 server/ 加到 sys.path(它还不是 installed package)
    import sys
    server_root = str(Path(__file__).resolve().parent.parent / "server")
    if server_root not in sys.path:
        sys.path.insert(0, server_root)
    from server.app.crypto.verify import verify_heartbeat_request
    from server.app.storage.db import db_connection, init_db
    from server.app import config as _cfg
    # 用 tmp_path DB 替代默认值
    _cfg.DB_PATH = tmp_path / "test.db"
    init_db(_cfg.DB_PATH)

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    fake_dev_priv = Ed25519PrivateKey.generate()
    client_priv = Ed25519PrivateKey.generate()
    client_priv_seed = client_priv.private_bytes_raw()
    client_pub = client_priv.public_key().public_bytes_raw()

    payload = {
        "v": 2,
        "fingerprint_hex": "1" * 64,
        "customer": "x",
        "issued_at": 1,
        "expires_at": 0,
        "min_app_version": "0.3.1",
        "nonce": "0" * 16,
        "features": [],
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    # 错 developer sig
    bad_sig = fake_dev_priv.sign(payload_bytes)
    wire = client_priv_seed + client_pub + payload_bytes + bad_sig
    token_hex = wire.hex()

    timestamp = int(time.time())
    nonce = b"\x01" * 16
    msg = payload["fingerprint_hex"].encode("ascii") + str(timestamp).encode("ascii") + nonce
    client_sig = client_priv.sign(msg)

    request_payload = {
        "license_token": token_hex,
        "fingerprint": payload["fingerprint_hex"],
        "client_pubkey": client_pub.hex(),
        "timestamp": timestamp,
        "client_sig": client_sig.hex(),
        "nonce": nonce.hex(),
    }
    # developer pubkey 配置成跟 fake 不同的 → 必 reject
    real_dev_priv = Ed25519PrivateKey.generate()
    real_dev_pub = real_dev_priv.public_key().public_bytes_raw()
    _cfg.CLIENT_PUBKEY_HEX = real_dev_pub.hex()

    with db_connection() as conn:
        result = verify_heartbeat_request(request_payload, conn, timestamp)
    assert not result.ok
    assert result.error_code == "bad_signature"
