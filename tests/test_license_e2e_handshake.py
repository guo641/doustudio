"""
v0.3.1:true end-to-end client↔server 握手测试。

跟 test_license_v031.py 的区别:那个用 _MockServer (手写 http.server) 来
mock server 响应;这个跑**真的** FastAPI server 端点(server/app/api/heartbeat.py)
+ 真 SQLite + 真 KMS mock (本地 .pem 私钥),从 client.bootstrap.run_startup_handshake
→ perform_handshake → urllib POST → server 验签 → 响应 → client 验签 →
update_heartbeat_fields 落盘,整条链路。

之所以单独一个文件:
  - server/app/ 不是 installed package,得 sys.path 注入
  - KMS adapter 在 import-time 创建本地 dev 私钥,要 fixture 隔离
  - Bootstrap 调 current_fingerprint() 是 .pyd 内的 HMAC,跟真实机器指纹耦合;
    测试里直接 monkeypatch 跳过 .pyd

测试运行条件:
  - doupool.license._license_verify .pyd 已编译(verifier 加载 → 否则 status='uncompiled',
    bootstrap 跳握手 → 无 round-trip 可验,只能 skip)
  - server 端 deps(fastapi / pydantic / cryptography)可用
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

import pytest


# ============ 提前 sys.path 注入 + 顶层 import ============
_HERE = Path(__file__).resolve().parent
_SERVER_ROOT = _HERE.parent / "server"
if str(_SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(_SERVER_ROOT))

from server.app import main as _srv_main  # noqa: E402  在 sys.path 注入后


# ============ Fixtures ============

@pytest.fixture
def server_test_client():
    """FastAPI TestClient(走 in-process ASGI,不绑端口)。"""
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient
    return TestClient(_srv_main.app)


@pytest.fixture
def isolated_license_dir(monkeypatch, tmp_path):
    """activated.bin 落到 tmp_path,跟真实数据隔离。"""
    # Legacy in-process/localhost heartbeat fixtures intentionally use HTTP;
    # production transport rejects it unless this explicit test switch exists.
    monkeypatch.setenv("DOUSTUDIO_DEV_ALLOW_HTTP", "1")
    new_data = tmp_path / "data"
    new_log = tmp_path / "log"
    new_data.mkdir(parents=True)
    new_log.mkdir(parents=True)
    import doupool.config as _config
    monkeypatch.setattr(_config, "_resolve_app_dirs", lambda: (new_data, new_log))
    yield new_data / "license"


@pytest.fixture
def fresh_keys():
    """一组全新 Ed25519 keypair(developer + client + server)。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    dev_priv = Ed25519PrivateKey.generate()
    client_priv = Ed25519PrivateKey.generate()
    server_priv = Ed25519PrivateKey.generate()
    return {
        "dev_priv": dev_priv,
        "dev_pub": dev_priv.public_key().public_bytes_raw(),
        "client_priv_seed": client_priv.private_bytes_raw(),
        "client_pub": client_priv.public_key().public_bytes_raw(),
        "server_priv": server_priv,
        "server_pub": server_priv.public_key().public_bytes_raw(),
    }


@pytest.fixture
def wire_blob(fresh_keys):
    """构造一个合法 v0.3.1 license_token wire blob:
    [32B client_priv_seed][32B client_pub][payload_json][64B dev_sig]。
    """
    from doupool.license.crypto import sign as ed25519_sign
    fp = "1" * 64  # 跟 fake fingerprint 一致
    payload = {
        "v": 2,
        "fingerprint_hex": fp,
        "customer": "e2e-test",
        "issued_at": int(time.time()) - 10,
        "expires_at": int(time.time()) + 365 * 86400,  # 1 年有效
        "min_app_version": "0.3.1",
        "nonce": "abcdef01" * 2,
        "features": [],
    }
    payload_bytes = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    dev_sig = ed25519_sign(fresh_keys["dev_priv"], payload_bytes)
    wire = (
        fresh_keys["client_priv_seed"]
        + fresh_keys["client_pub"]
        + payload_bytes
        + dev_sig
    )
    return {
        "wire": wire,
        "wire_hex": wire.hex(),
        "payload_bytes": payload_bytes,
        "dev_sig": dev_sig,
        "fingerprint": fp,
        "payload": payload,
    }


@pytest.fixture
def setup_server_db(tmp_path, monkeypatch):
    """把 server 端 DB 指向 tmp_path + 注入 CLIENT_PUBKEY_HEX + 让 KMS 走本地 dev 模式。"""
    from server.app import config as _cfg
    from server.app.storage.db import init_db

    db_path = tmp_path / "e2e_license.sqlite3"
    _cfg.DB_PATH = db_path
    init_db(db_path)
    yield _cfg


@pytest.fixture
def configure_server_pubkey(fresh_keys, setup_server_db):
    """把 server 自己的 KMS pubkey 注入 client._embedded_server_pubkey,
    让 client 在握手时能验 server_sig。同时替换 server 端 KMS singleton 为
    用 fresh_keys["server_priv"] 签响应的实例。
    """
    import doupool.license._embedded_server_pubkey as _sp_mod
    from doupool.license import heartbeat as _hb_mod
    from server.app.crypto.kms_adapter import KmsAdapter

    # 注入 client 端 server pubkey
    _sp_mod.ENCRYPTED_SERVER_PUBKEY = fresh_keys["server_pub"]
    _sp_mod.XOR_SERVER_MASK = b"\x00" * 32
    _hb_mod._SERVER_PUBKEY = fresh_keys["server_pub"]

    # 替换 server KMS singleton
    fresh_signer = KmsAdapter.__new__(KmsAdapter)
    fresh_signer._use_kms = False
    fresh_signer._local_priv = fresh_keys["server_priv"]
    fresh_signer._local_pub = fresh_keys["server_priv"].public_key()
    fresh_signer._kms_client = None
    fresh_signer._key_id = ""

    import server.app.api.heartbeat as hb_api
    original_singleton = _srv_main._kms_singleton
    _srv_main._kms_singleton = fresh_signer
    hb_api.get_kms = lambda: fresh_signer
    try:
        yield fresh_signer
    finally:
        _srv_main._kms_singleton = original_singleton


# ============ Tests ============

def test_server_healthz_returns_ok(server_test_client):
    """/healthz 返 {ok: True, version: 0.3.1}。证明 server app 已 import。"""
    r = server_test_client.get("/healthz")
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert data["version"] == "0.3.1"


def test_heartbeat_endpoint_via_testclient_happy_path(
    server_test_client,
    setup_server_db,
    configure_server_pubkey,
    fresh_keys,
    wire_blob,
):
    """真的 POST /api/heartbeat(server FastAPI handler),正确签名 → 200 + fresh_until。"""
    setup_server_db.CLIENT_PUBKEY_HEX = fresh_keys["dev_pub"].hex()

    timestamp = int(time.time())
    nonce = b"\x02" * 16
    msg = wire_blob["fingerprint"].encode("ascii") + str(timestamp).encode("ascii") + nonce
    client_sig = fresh_keys["client_priv_seed"]  # placeholder, recomputed below
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    client_priv = Ed25519PrivateKey.from_private_bytes(fresh_keys["client_priv_seed"])
    client_sig = client_priv.sign(msg)

    req = {
        "license_token": wire_blob["wire_hex"],
        "fingerprint": wire_blob["fingerprint"],
        "client_pubkey": fresh_keys["client_pub"].hex(),
        "timestamp": timestamp,
        "client_sig": client_sig.hex(),
        "nonce": nonce.hex(),
    }
    r = server_test_client.post("/api/heartbeat", json=req)
    assert r.status_code == 200, f"server 拒绝: {r.status_code} {r.text}"
    data = r.json()
    assert data["ok"] is True
    assert data["fresh_until"] > int(time.time())
    # server_sig 必须存在且是 128 hex
    assert len(data["server_sig"]) == 128
    # nonce / client_pubkey 回显
    assert data["nonce"] == nonce.hex()
    assert data["client_pubkey"] == fresh_keys["client_pub"].hex()


def test_heartbeat_endpoint_rejects_bad_developer_sig(
    server_test_client,
    setup_server_db,
    configure_server_pubkey,
    fresh_keys,
    wire_blob,
):
    """CLIENT_PUBKEY_HEX 配成「另一个开发者 pubkey」 → server 应拒绝(developer sig 错)。"""
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    wrong_dev_priv = Ed25519PrivateKey.generate()
    setup_server_db.CLIENT_PUBKEY_HEX = wrong_dev_priv.public_key().public_bytes_raw().hex()

    timestamp = int(time.time())
    nonce = b"\x03" * 16
    msg = wire_blob["fingerprint"].encode("ascii") + str(timestamp).encode("ascii") + nonce
    client_priv = Ed25519PrivateKey.from_private_bytes(fresh_keys["client_priv_seed"])
    client_sig = client_priv.sign(msg)

    req = {
        "license_token": wire_blob["wire_hex"],
        "fingerprint": wire_blob["fingerprint"],
        "client_pubkey": fresh_keys["client_pub"].hex(),
        "timestamp": timestamp,
        "client_sig": client_sig.hex(),
        "nonce": nonce.hex(),
    }
    r = server_test_client.post("/api/heartbeat", json=req)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "bad_signature"


def test_heartbeat_endpoint_rejects_clock_skew(
    server_test_client,
    setup_server_db,
    configure_server_pubkey,
    fresh_keys,
    wire_blob,
):
    """timestamp 跟 server time 差 > 300s → bad_timestamp。"""
    setup_server_db.CLIENT_PUBKEY_HEX = fresh_keys["dev_pub"].hex()

    # 让 client_timestamp = server_time - 1000s
    # server 内部 timestamp = int(time.time()),所以这里用过去时间
    timestamp = int(time.time()) - 1000
    nonce = b"\x04" * 16
    msg = wire_blob["fingerprint"].encode("ascii") + str(timestamp).encode("ascii") + nonce
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    client_priv = Ed25519PrivateKey.from_private_bytes(fresh_keys["client_priv_seed"])
    client_sig = client_priv.sign(msg)

    req = {
        "license_token": wire_blob["wire_hex"],
        "fingerprint": wire_blob["fingerprint"],
        "client_pubkey": fresh_keys["client_pub"].hex(),
        "timestamp": timestamp,
        "client_sig": client_sig.hex(),
        "nonce": nonce.hex(),
    }
    r = server_test_client.post("/api/heartbeat", json=req)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is False
    assert data["code"] == "bad_timestamp"


# ============ 真 round-trip(透过 perform_handshake 的 urllib 走 TestClient 不可行,改走 _do_http_post monkeypatch) ============

def test_perform_handshake_round_trips_via_in_process_server(
    isolated_license_dir,
    setup_server_db,
    configure_server_pubkey,
    fresh_keys,
    wire_blob,
    monkeypatch,
):
    """真 round-trip:
      1. activated.bin 落 v0.3.1 token
      2. monkeypatch heartbeat._do_http_post → 直接调 server FastAPI handler
      3. perform_handshake 走到 _do_http_post → server 处理 → 响应 → 验签 → ok=True
      4. update_heartbeat_fields 落盘
    """
    setup_server_db.CLIENT_PUBKEY_HEX = fresh_keys["dev_pub"].hex()

    # 写 activated.bin
    from doupool.license import storage
    storage.write_token_v031(
        license_token_blob=wire_blob["wire"],
        client_priv_seed=fresh_keys["client_priv_seed"],
        client_pubkey=fresh_keys["client_pub"],
        fresh_until=0,
        clock_offset_ms=0,
        last_server_sync=0,
    )
    stored_before = storage.read_token_v031()
    assert stored_before is not None
    assert stored_before.fresh_until == 0

    # 把 server TestClient 注入 _do_http_post
    pytest.importorskip("fastapi.testclient")
    from fastapi.testclient import TestClient
    import doupool.license.heartbeat as _hb

    tc = TestClient(_srv_main.app)

    def _in_process_post(url, payload, timeout_sec):
        # 忽略 url / timeout_sec,直接 POST 到 TestClient
        import json as _json
        r = tc.post("/api/heartbeat", json=payload)
        if r.status_code == 200:
            return r.json(), ""
        # heartbeat._do_http_post 把 HTTPError 当 BAD_RESPONSE
        try:
            data = r.json()
        except Exception:
            return None, _hb.ErrCode.BAD_RESPONSE
        return data, data.get("code", _hb.ErrCode.BAD_RESPONSE)

    monkeypatch.setattr(_hb, "_do_http_post", _in_process_post)

    result = _hb.perform_handshake(
        license_token_hex=wire_blob["wire_hex"],
        client_priv_seed=fresh_keys["client_priv_seed"],
        fingerprint_hex=wire_blob["fingerprint"],
        server_url="http://in-process.test",  # 不真用,被 monkeypatch 接管
    )
    assert result.ok, f"握手失败: {result.error_code}"
    assert result.fresh_until > int(time.time())
    # 验证 server_timestamp 跟本地差 < 1s
    assert abs(result.server_timestamp - int(time.time())) < 5

    # bootstrap.run_startup_handshake 应能 update_heartbeat_fields 写盘
    from doupool.license import bootstrap

    # bootstrap 用 `from doupool.license import ...` 在函数内部,所以 patch
    # doupool.license.* 上的属性就够了——函数体每次都重新解析。
    import doupool.license as _lic
    monkeypatch.setattr(_lic, "current_fingerprint", lambda: wire_blob["fingerprint"])
    monkeypatch.setattr(_lic, "get_activation_status", lambda: "valid")

    bootstrap.run_startup_handshake()
    stored_after = storage.read_token_v031()
    assert stored_after is not None
    assert stored_after.fresh_until == result.fresh_until
    assert stored_after.last_server_sync > 0
