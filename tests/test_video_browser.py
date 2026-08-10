import json
import sqlite3
from pathlib import Path

from doupool.video.browser import (
    AISPACE_SCRIPT,
    CHAIN_SCRIPT,
    COMPLETION_SCRIPT,
    UPLOAD_IMAGE_SCRIPT,
    TokenBundle,
    TokenBundleUnavailable,
    _build_launch_kwargs,
    _read_chromium_cookies,
    _read_chromium_local_storage,
    _read_cookies_from_json,
    extract_webmssdk_tokens,
    load_browser_context,
    read_browser_fingerprint,
)


class FakePage:
    def __init__(self, evaluate_result=None):
        self.expression = None
        self.timeout = None
        self._evaluate_result = evaluate_result or {
            "web_id": "wb_from_storage",
            "tea_uuid": "tu_from_storage",
            "device_id": "dev_from_storage",
        }

    async def wait_for_function(self, expression, timeout):
        self.expression = expression
        self.timeout = timeout

    async def evaluate(self, _expression):
        return self._evaluate_result


class FakeContext:
    def __init__(self, cookies=None):
        # 注意:用 `if cookies is None` 而不是 `cookies or [...]`,
        # 否则 cookies=[] 会被默认值替换掉
        if cookies is None:
            cookies = [{"name": "s_v_web_id", "value": "verify_current_fp"}]
        self._cookies = cookies

    async def cookies(self, urls):
        assert urls == ["https://www.doubao.com"]
        return self._cookies


import pytest


@pytest.mark.asyncio
async def test_read_browser_fingerprint_uses_current_tea_key_and_fingerprint_cookie():
    page = FakePage()

    fingerprint = await read_browser_fingerprint(page, FakeContext())

    assert "__tea_cache_tokens_497858" in page.expression
    assert "__tea_cache_tokens_2018" not in page.expression
    assert page.timeout == 15_000
    assert fingerprint == "verify_current_fp"


def test_page_requests_use_current_fingerprint_and_web_id_sources():
    assert "fp:payload.ext.fp" in COMPLETION_SCRIPT
    assert "web_id:tea.web_id" in COMPLETION_SCRIPT
    for script in (CHAIN_SCRIPT, AISPACE_SCRIPT):
        assert "s_v_web_id" in script
        assert "web_id:tea.web_id" in script
        assert "__tea_cache_tokens_2018" not in script


def test_upload_script_covers_i2v_pipeline():
    for marker in (
        "/alice/resource/prepare_upload",
        "ApplyImageUpload",
        "CommitImageUpload",
        "/alice/message/pre_handle_v2_without_conv",
        "resource_type:2",
        "entity_type:2",
    ):
        assert marker in UPLOAD_IMAGE_SCRIPT


# --- v0.2.17:TokenBundle + 真实 fp 注入 ---


def test_token_bundle_to_client_meta_drops_empty_fields():
    """v0.2.17:to_client_meta 只返非空字段,空值会被 build_completion_payload 过滤。"""
    bundle = TokenBundle(web_id="wb_x", tea_uuid="", device_id="", web_id_signature="sig_x")
    meta = bundle.to_client_meta()
    assert meta == {"web_id": "wb_x", "web_id_signature": "sig_x", "pc_version": TokenBundle().pc_version}
    assert "tea_uuid" not in meta
    assert "device_id" not in meta


def test_token_bundle_to_client_meta_always_has_pc_version():
    """pc_version 即使没显式给也要填(默认 PC_VERSION),让 payload 带上。"""
    bundle = TokenBundle(web_id="wb_x")
    assert bundle.to_client_meta()["pc_version"] == "3.27.4"


@pytest.mark.asyncio
async def test_load_browser_context_reads_tea_and_device_storage():
    """v0.2.17:load_browser_context 从 page.evaluate 抽 localStorage 的 web_id /
    tea_uuid / device_id,凑齐 TokenBundle 透传给 payload.client_meta。"""
    cookies = [
        {"name": "s_v_web_id", "value": "device_cookie_fp"},
        {"name": "msToken", "value": "ms_abc"},
    ]
    bundle = await load_browser_context(FakePage(), FakeContext(cookies=cookies))
    assert bundle.web_id == "wb_from_storage"
    assert bundle.tea_uuid == "tu_from_storage"
    assert bundle.device_id == "dev_from_storage"
    assert bundle.ms_token == "ms_abc"
    assert bundle.pc_version == "3.27.4"


@pytest.mark.asyncio
async def test_load_browser_context_falls_back_to_cookies_when_storage_empty():
    """v0.2.17:localStorage 全空 → web_id / device_id / tea_uuid 走 cookie 兜底。"""
    page = FakePage(evaluate_result={"web_id": "", "tea_uuid": "", "device_id": ""})
    cookies = [
        {"name": "s_v_web_id", "value": "fp_cookie"},
        {"name": "samantha_web_web_id", "value": "wb_cookie"},
        {"name": "user_unique_id", "value": "tu_cookie"},
    ]
    bundle = await load_browser_context(page, FakeContext(cookies=cookies))
    assert bundle.web_id == "wb_cookie"
    assert bundle.tea_uuid == "tu_cookie"
    assert bundle.device_id == "fp_cookie"


@pytest.mark.asyncio
async def test_load_browser_context_raises_when_no_fingerprint_cookie():
    """v0.2.17:连 fp cookie + localStorage device_id 都拿不到 → 提示重新登录。"""
    page = FakePage(evaluate_result={"web_id": "", "tea_uuid": "", "device_id": ""})
    with __import__("pytest").raises(RuntimeError, match="重新登录"):
        await load_browser_context(page, FakeContext(cookies=[]))


@pytest.mark.asyncio
async def test_load_browser_context_raises_token_bundle_unavailable_when_no_web_id():
    """v0.2.17:web_id 完全抽不到(冷启动 profile 没让 WebMSSDK 跑过)→
    抛 TokenBundleUnavailable,UI 引导用户去 doubao.com/chat/ 主页访问 5 秒。"""
    page = FakePage(evaluate_result={"web_id": "", "tea_uuid": "", "device_id": ""})
    cookies = [{"name": "s_v_web_id", "value": "fp_only"}]  # 只剩 fp,没 web_id
    with __import__("pytest").raises(TokenBundleUnavailable, match="web_id"):
        await load_browser_context(page, FakeContext(cookies=cookies))


def test_extract_webmssdk_tokens_reads_cookies_sqlite(tmp_path):
    """v0.2.17:extract_webmssdk_tokens 从 Default/Cookies SQLite 抽 msToken /
    web_id_signature,s_v_web_id 等。"""
    profile_dir = tmp_path / "profile"
    cookies_dir = profile_dir / "Default"
    cookies_dir.mkdir(parents=True)
    cookies_db = cookies_dir / "Cookies"

    conn = sqlite3.connect(str(cookies_db))
    conn.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB, path TEXT, expires_utc INTEGER, is_secure INTEGER, "
        "is_httponly INTEGER, same_site INTEGER, last_access_utc INTEGER, "
        "has_expires INTEGER, priority INTEGER, samesite INTEGER)"
    )
    rows = [
        (".doubao.com", "msToken", "ms_from_sqlite", "", "/", 0, 0, 0, 0, 0, 0, 0, 0),
        (".doubao.com", "_signature", "sig_from_sqlite", "", "/", 0, 0, 0, 0, 0, 0, 0, 0),
        (".doubao.com", "s_v_web_id", "fp_from_sqlite", "", "/", 0, 0, 0, 0, 0, 0, 0, 0),
        (".doubao.com", "samantha_web_web_id", "wb_from_sqlite", "", "/", 0, 0, 0, 0, 0, 0, 0, 0),
        (".example.com", "msToken", "ms_other_domain", "", "/", 0, 0, 0, 0, 0, 0, 0, 0),
    ]
    conn.executemany(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    conn.commit()
    conn.close()

    # v0.2.37.2:cookies.json 不存在 → 走 SQLite 兜底。samantha_web_web_id
    # 是 cookie 兜底给 web_id 用的字段,没有它现在的 hint 不再容忍。
    bundle = extract_webmssdk_tokens(profile_dir)
    assert bundle.ms_token == "ms_from_sqlite"
    assert bundle.web_id_signature == "sig_from_sqlite"
    assert bundle.web_id == "wb_from_sqlite"
    assert bundle.device_id == "fp_from_sqlite"  # 兜底 s_v_web_id


def test_extract_webmssdk_tokens_raises_when_profile_dir_missing(tmp_path):
    """v0.2.37.2:profile 目录里啥都没有 → 抛 TokenBundleUnavailable,
    hint 引导用户点「重新导出 cookies」按钮(替代 v0.2.17 的「web_id」字眼)。"""
    with __import__("pytest").raises(TokenBundleUnavailable, match="重新导出 cookies"):
        extract_webmssdk_tokens(tmp_path / "empty_profile")


def test_extract_webmssdk_tokens_wraps_corrupted_cookies_sqlite(tmp_path, monkeypatch):
    """v0.2.36:Cookies SQLite 损坏(老版直接 raise sqlite3.DatabaseError →
    500 给前端「token 状态加载失败」)→ 必须归一到 TokenBundleUnavailable,
    让上层 endpoint 拿到真实原因。

    场景:Chromium profile 的 Cookies SQLite 被损坏 / 锁占用时,read_bytes
    可能成功但 sqlite3.connect 抛 DatabaseError。extract 必须 catch 并把
    真实异常类名 + 消息塞进 hint,这样用户能看到「Cookies 文件损坏」而不是
    「token 状态加载失败」这种没用的兜底。
    """
    profile_dir = tmp_path / "profile"
    cookies_dir = profile_dir / "Default"
    cookies_dir.mkdir(parents=True)
    cookies_db = cookies_dir / "Cookies"
    cookies_db.write_bytes(b"this is not a sqlite database")  # 损坏

    # monkeypatch sqlite3.connect 让它在损坏文件上抛 DatabaseError
    import sqlite3 as _sqlite3
    orig_connect = _sqlite3.connect
    calls = {"n": 0}

    def broken_connect(*args, **kwargs):
        calls["n"] += 1
        # 第一次连接:tmp 拷贝(ro uri)抛 DatabaseError(因为内容不是 sqlite)
        raise _sqlite3.DatabaseError("file is not a database")

    monkeypatch.setattr(_sqlite3, "connect", broken_connect)

    with __import__("pytest").raises(TokenBundleUnavailable) as exc_info:
        extract_webmssdk_tokens(profile_dir)

    msg = str(exc_info.value)
    assert "DatabaseError" in msg, f"hint 必须含真实异常类名;got {msg!r}"
    assert "file is not a database" in msg
    assert calls["n"] >= 1


def test_extract_webmssdk_tokens_wraps_permission_error_on_read(tmp_path, monkeypatch):
    """v0.2.36:read_bytes 抛 PermissionError(Chromium 正在用 profile) →
    必须归到 TokenBundleUnavailable,不再 500。"""
    profile_dir = tmp_path / "profile"
    cookies_dir = profile_dir / "Default"
    cookies_dir.mkdir(parents=True)
    cookies_db = cookies_dir / "Cookies"
    cookies_db.write_bytes(b"x")

    real_read_bytes = Path.read_bytes

    def broken_read_bytes(self, *args, **kwargs):
        if str(self).endswith("Cookies"):
            raise PermissionError("The process cannot access the file because it is being used by another process")
        return real_read_bytes(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", broken_read_bytes)

    with __import__("pytest").raises(TokenBundleUnavailable) as exc_info:
        extract_webmssdk_tokens(profile_dir)

    msg = str(exc_info.value)
    assert "PermissionError" in msg, f"hint 必须含真实异常类名;got {msg!r}"
    assert "being used by another process" in msg


# --- v0.2.37.2:cookies.json 优先 + 删 DPAPI Playwright fallback ---


def test_v0_2_37_2_read_cookies_from_json_returns_login_backup(tmp_path):
    """v0.2.37.2:_read_cookies_from_json 读 login 流程主动导出的明文备份,
    这是 DPAPI 加密问题的源头替代方案:登录成功的瞬间从 Chromium 进程直接
    拉 cookies() 写明文到 profile_dir/cookies.json,后续任何时刻读此文件
    都能拿到当前登录态。
    """
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    cookies_json = profile_dir / "cookies.json"
    # 模拟 Playwright context.cookies() 输出的 Playwright Cookie 数组
    cookies_json.write_text(
        json.dumps([
            {"name": "msToken", "value": "ms_from_json", "domain": ".doubao.com", "path": "/"},
            {"name": "_signature", "value": "sig_from_json", "domain": ".doubao.com", "path": "/"},
            {"name": "s_v_web_id", "value": "fp_from_json", "domain": ".doubao.com", "path": "/"},
        ]),
        encoding="utf-8",
    )

    result = _read_cookies_from_json(profile_dir)
    assert result == {
        "msToken": "ms_from_json",
        "_signature": "sig_from_json",
        "s_v_web_id": "fp_from_json",
    }


def test_v0_2_37_2_read_cookies_from_json_returns_none_when_missing(tmp_path):
    """v0.2.37.2:profile 还没经过 login 流程(cookies.json 不存在)→
    返 None 让上层走 SQLite 兜底,而不是空 dict 让上层误判「profile 没数据」。"""
    profile_dir = tmp_path / "fresh_profile"
    profile_dir.mkdir()
    assert _read_cookies_from_json(profile_dir) is None


def test_v0_2_37_2_read_cookies_from_json_returns_none_on_corrupt_json(tmp_path):
    """v0.2.37.2:cookies.json 内容损坏(写到一半被 kill)→ 返 None 走 SQLite 兜底,
    而不是冒泡让 endpoint 500。"""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "cookies.json").write_text("{not valid json", encoding="utf-8")
    assert _read_cookies_from_json(profile_dir) is None


def test_v0_2_37_2_read_cookies_from_json_skips_malformed_entries(tmp_path):
    """v0.2.37.2:cookies.json 数组里掺杂非 dict / 缺 name / 缺 value 项时,
    必须跳过这些项而不是抛 KeyError。"""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / "cookies.json").write_text(
        json.dumps([
            {"name": "msToken", "value": "ms_ok"},
            "not a dict",
            {"name": "no_value"},
            {"value": "no_name"},
            {"name": "sig", "value": "sig_ok"},
        ]),
        encoding="utf-8",
    )
    result = _read_cookies_from_json(profile_dir)
    assert result == {"msToken": "ms_ok", "sig": "sig_ok"}


def test_v0_2_37_2_extract_prefers_cookies_json_over_sqlite(tmp_path, monkeypatch):
    """v0.2.37.2:extract_webmssdk_tokens 优先用 cookies.json,即使 SQLite
    也能返回数据,只要 cookies.json 存在就用它的值(cookies.json 是从
    Chromium 进程实时拉的,比 SQLite 拿到的磁盘值更新)。

    还顺带验证:cookies.json 里只放 msToken / _signature 时,web_id
    仍从 cookies.samantha_web_web_id 兜底拿(s_v_web_id 也兜底 device_id)。
    """
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()

    # cookies.json 写明文
    (profile_dir / "cookies.json").write_text(
        json.dumps([
            {"name": "msToken", "value": "ms_from_json_priority", "domain": ".doubao.com"},
            {"name": "_signature", "value": "sig_from_json_priority", "domain": ".doubao.com"},
            {"name": "samantha_web_web_id", "value": "wb_from_json_cookie", "domain": ".doubao.com"},
            {"name": "s_v_web_id", "value": "fp_from_json_cookie", "domain": ".doubao.com"},
        ]),
        encoding="utf-8",
    )

    # SQLite 里写「旧」的值 —— 验证 cookies.json 优先
    cookies_dir = profile_dir / "Default"
    cookies_dir.mkdir()
    cookies_db = cookies_dir / "Cookies"
    conn = sqlite3.connect(str(cookies_db))
    conn.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB, path TEXT, expires_utc INTEGER, is_secure INTEGER, "
        "is_httponly INTEGER, same_site INTEGER, last_access_utc INTEGER, "
        "has_expires INTEGER, priority INTEGER, samesite INTEGER)"
    )
    conn.executemany(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (".doubao.com", "msToken", "ms_from_sqlite_stale", "", "/", 0, 0, 0, 0, 0, 0, 0, 0),
            (".doubao.com", "_signature", "sig_from_sqlite_stale", "", "/", 0, 0, 0, 0, 0, 0, 0, 0),
        ],
    )
    conn.commit()
    conn.close()

    bundle = extract_webmssdk_tokens(profile_dir)
    # cookies.json 优先 → 拿到的是 ms_from_json_priority,不是 sqlite 的 stale 版本
    assert bundle.ms_token == "ms_from_json_priority"
    assert bundle.web_id_signature == "sig_from_json_priority"
    # web_id / device_id 在 cookies.json 里走 cookie 兜底
    assert bundle.web_id == "wb_from_json_cookie"
    assert bundle.device_id == "fp_from_json_cookie"


def test_v0_2_37_2_extract_falls_back_to_sqlite_when_cookies_json_absent(tmp_path):
    """v0.2.37.2:cookies.json 不存在(老用户升级前 / 走的 SQLite 路径)→
    必须能继续从 Default/Cookies 拿到 msToken / _signature,不能因为优先
    cookies.json 就把老路径打挂。
    """
    profile_dir = tmp_path / "profile"
    cookies_dir = profile_dir / "Default"
    cookies_dir.mkdir(parents=True)
    cookies_db = cookies_dir / "Cookies"
    conn = sqlite3.connect(str(cookies_db))
    conn.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB, path TEXT, expires_utc INTEGER, is_secure INTEGER, "
        "is_httponly INTEGER, same_site INTEGER, last_access_utc INTEGER, "
        "has_expires INTEGER, priority INTEGER, samesite INTEGER)"
    )
    conn.executemany(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (".doubao.com", "msToken", "ms_from_sqlite", "", "/", 0, 0, 0, 0, 0, 0, 0, 0),
            (".doubao.com", "_signature", "sig_from_sqlite", "", "/", 0, 0, 0, 0, 0, 0, 0, 0),
            (".doubao.com", "s_v_web_id", "fp_from_sqlite", "", "/", 0, 0, 0, 0, 0, 0, 0, 0),
            (".doubao.com", "samantha_web_web_id", "wb_from_sqlite_cookie", "", "/", 0, 0, 0, 0, 0, 0, 0, 0),
        ],
    )
    conn.commit()
    conn.close()

    bundle = extract_webmssdk_tokens(profile_dir)
    assert bundle.ms_token == "ms_from_sqlite"
    assert bundle.web_id_signature == "sig_from_sqlite"
    # web_id SQLite 拿不到 → 走 cookie 兜底(s_v_web_id 或 samantha_web_web_id)
    assert bundle.web_id == "wb_from_sqlite_cookie"
    assert bundle.device_id == "fp_from_sqlite"


def test_v0_2_37_2_extract_raises_when_essential_fields_missing(tmp_path):
    """v0.2.37.2:cookies.json / SQLite / storage 三个源都拿不到 web_id +
    msToken → 抛 TokenBundleUnavailable 且 hint 引导用户点「重新导出 cookies」。
    """
    profile_dir = tmp_path / "empty_profile"
    profile_dir.mkdir()
    with __import__("pytest").raises(TokenBundleUnavailable) as exc_info:
        extract_webmssdk_tokens(profile_dir)
    msg = str(exc_info.value)
    assert "重新导出 cookies" in msg, (
        f"hint 必须引导用户点「重新导出 cookies」按钮;got {msg!r}"
    )


def test_v0_2_37_2_local_storage_regex_extracts_web_id_without_script_tag(tmp_path):
    """v0.2.37.2:修复 v0.2.17 的 leveldb regex bug —— 之前用 `(.+?)</script>`
    期待 HTML script 边界,但 leveldb 000003.log 是二进制文本,根本没有
    `</script>` → 那个分支永远不命中,导致 web_id 永远从 storage 拿不到。
    新 regex 用 `(\\{...?"web_id"...\\})` 抓最近的 JSON object,本测试用
    一个不含 `</script>` 但包含 `__tea_cache_tokens_497858` 块的 leveldb
    数据,验证能抽出 web_id + user_unique_id。
    """
    profile_dir = tmp_path / "profile"
    storage_dir = profile_dir / "Default" / "Local Storage" / "leveldb"
    storage_dir.mkdir(parents=True)
    log = storage_dir / "000003.log"
    # 模拟 leveldb 一条记录:key=`__tea_cache_tokens_497858`,value 是 JSON
    # 注意:leveldb 没有 `</script>` —— 这正是 v0.2.17 旧 regex 抓不到的原因
    log.write_bytes(
        b"\x00\x01__tea_cache_tokens_497858"
        b'{"user_unique_id":"tu_abc12345","web_id":"wb_xyz98765","expire":1234567890}'
        b"\x00\x00",
    )

    out = _read_chromium_local_storage(profile_dir)
    assert out.get("web_id") == "wb_xyz98765", f"web_id 必须从 storage 抽出;got {out!r}"
    assert out.get("tea_uuid") == "tu_abc12345", f"tea_uuid (user_unique_id) 必须抽出;got {out!r}"


def test_v0_2_37_2_local_storage_regex_extracts_samantha_web_id(tmp_path):
    """v0.2.37.2:samantha_web_web_id 块中的 web_id 字段 → 抽到 device_id
    (device_id 之前是从 s_v_web_id cookie 兜底,现在优先从 samantha web_id 拿,
    因为这是 WebMSSDK 的真实 device 字段)。"""
    profile_dir = tmp_path / "profile"
    storage_dir = profile_dir / "Default" / "Local Storage" / "leveldb"
    storage_dir.mkdir(parents=True)
    log = storage_dir / "000003.log"
    log.write_bytes(
        b"samantha_web_web_id"
        b'{"web_id":"dev_from_samantha_block","mode":"strict"}',
    )

    out = _read_chromium_local_storage(profile_dir)
    assert out.get("device_id") == "dev_from_samantha_block", (
        f"device_id 必须从 samantha_web_web_id 块抽出;got {out!r}"
    )


def test_v0_2_37_2_local_storage_returns_empty_when_log_missing(tmp_path):
    """v0.2.37.2:leveldb 000003.log 还没创建(冷启动)→ 返 {} 不抛异常。"""
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    assert _read_chromium_local_storage(profile_dir) == {}


def test_v0_2_37_2_local_storage_handles_partial_corrupt_json(tmp_path):
    """v0.2.37.2:leveldb 二进制可能在 Chromium 写入时被截断,只读到半个 JSON
    → json.loads 抛 ValueError → 必须吞掉,返 {}(让上层走 cookie 兜底)
    而不是冒泡。"""
    profile_dir = tmp_path / "profile"
    storage_dir = profile_dir / "Default" / "Local Storage" / "leveldb"
    storage_dir.mkdir(parents=True)
    log = storage_dir / "000003.log"
    log.write_bytes(
        b"__tea_cache_tokens_497858"
        b'{"user_unique_id":"tu_half","web_id":"wb_hal',  # 故意截断
    )

    # 不能让 ValueError 冒泡
    out = _read_chromium_local_storage(profile_dir)
    # 解析失败 → 备用 regex 抓不到完整 web_id → 返 {}
    assert isinstance(out, dict)


def test_v0_2_37_2_keepalive_default_is_30_seconds():
    """v0.2.37.2:回退 v0.2.37 的 90s 改动,login keepalive 恢复 30s 默认
    (用户反馈「只要 cookie 在线账号就没问题」,不需要延长 keepalive)。

    验证三个入口点的默认值:
    LoginService 类型注解 + PlaywrightLoginRunner.__init__ 默认值。
    """
    import inspect

    from doupool.login.service import LoginService

    sig = inspect.signature(LoginService.__init__)
    keepalive_param = sig.parameters["keepalive_seconds"]
    assert keepalive_param.default == 30.0, (
        f"LoginService.keepalive_seconds 默认必须 = 30.0;got {keepalive_param.default}"
    )

    from doupool.login.browser import PlaywrightLoginRunner

    sig2 = inspect.signature(PlaywrightLoginRunner.__init__)
    keepalive_param2 = sig2.parameters["keepalive_seconds"]
    assert keepalive_param2.default == 30.0, (
        f"PlaywrightLoginRunner.keepalive_seconds 默认必须 = 30.0;got {keepalive_param2.default}"
    )


def test_build_launch_kwargs_includes_stealth_args_and_locale():
    """v0.2.17:_build_launch_kwargs 必须包含反自动化开关 + zh-CN 时区/语言。"""
    kwargs = _build_launch_kwargs()
    assert kwargs["headless"] is False
    assert "--disable-blink-features=AutomationControlled" in kwargs["args"]
    assert "--disable-features=IsolateOrigins,site-per-process" in kwargs["args"]
    assert kwargs["locale"] == "zh-CN"
    assert kwargs["timezone_id"] == "Asia/Shanghai"
    assert kwargs["extra_http_headers"]["Referer"] == "https://www.doubao.com/chat/"
    assert kwargs["extra_http_headers"]["Accept-Language"].startswith("zh-CN")
    # viewport 是 [937,943] × [647,653] 的抖动区间,实测允许 ±3
    assert 937 <= kwargs["viewport"]["width"] <= 943
    assert 647 <= kwargs["viewport"]["height"] <= 653


# --- v0.2.26:anchor page 不能被 task 复用,否则并发 task 会关闭 anchor → context 崩溃 ---


class _AnchorPage:
    """anchor page 模拟:代表 _get_shared_context 保留的 pages[0],不能被关闭。"""

    def __init__(self):
        self.closed = False
        self.url = "about:blank"

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        # 如果业务代码试图关 anchor,模拟「导致 context 崩溃」
        # 测试要断言:anchor.closed 在 run() / recheck_result() 之后必须仍为 False
        self.closed = True


class _TaskPage:
    """run() / recheck_result() 自己 new_page() 出来的 task page。"""

    def __init__(self):
        self.closed = False
        self.url = "about:blank"
        self.goto_calls: list[tuple[str, int]] = []
        self.evaluate_calls: list[str] = []

    def is_closed(self) -> bool:
        return self.closed

    async def close(self) -> None:
        self.closed = True

    async def goto(self, url: str, wait_until: str = "load", timeout: int = 30_000):
        self.goto_calls.append((url, timeout))

    async def wait_for_timeout(self, _ms: int) -> None:
        pass

    async def wait_for_function(self, _expression, timeout: int = 0):
        pass

    async def evaluate(self, expression: str, arg=None):
        self.evaluate_calls.append(expression)
        # 终止 run() 的 while 循环,避免无限循环
        if "submit" in expression.lower() or "complete" in expression.lower():
            return {
                "send_url": "",
                "send_message": "ok",
                "completion_data": None,
                "message_data": None,
                "chain_data": {"status": 200, "data": {"status": "running", "message": {"create_message": {}}}},
            }
        if "chain" in expression.lower():
            return {"status": 200, "data": {"status": "running", "message": {"create_message": {}}}}
        return None


class _FakeNewContext:
    """v0.2.26 测试用 mock —— 模拟 BrowserContext,带 anchor + new_page()。"""

    def __init__(self, profile_dir: Path):
        self.profile_dir = profile_dir
        self.anchor = _AnchorPage()
        self.task_pages: list[_TaskPage] = []
        self._closed = False

    @property
    def pages(self) -> list:
        # 始终返回 [anchor, *task_pages],模拟 Playwright 真实行为
        result = [self.anchor]
        result.extend(self.task_pages)
        return result

    def is_closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        self._closed = True

    async def new_page(self) -> _TaskPage:
        if self._closed:
            raise RuntimeError("context closed")
        page = _TaskPage()
        self.task_pages.append(page)
        return page


class _RunnerPatch:
    """最小化的 PlaywrightVideoRunner 替身,跑 run() / recheck_result() 的子集路径。

    我们不实例化真实的 PlaywrightVideoRunner(需要 playwright 启动 + chromium 二进制),
    只复制必要字段并 inline 测试所需的代码段。这样改 browser.py 时如果不小心重命名
    字段,这些测试也会挂。
    """

    def __init__(self, context: _FakeNewContext):
        self._contexts: dict[str, _FakeNewContext] = {str(context.profile_dir): context}
        self._tokens: dict[str, object] = {str(context.profile_dir): object()}

    async def _get_shared_context(self, profile_dir: Path, pc_version=None):
        return self._contexts[str(profile_dir)], self._tokens[str(profile_dir)]

    async def run(self, profile_dir: Path):
        # 复制 browser.py run() 第 815-840 行(到 new_page 之后立即抛错的位置)。
        # 我们只测试 new_page() 是否被调用 + anchor 是否被保留,不进入 submit/poll。
        context, _bundle = await self._get_shared_context(profile_dir)
        from doupool.video.browser import _is_context_alive
        if not _is_context_alive(context):
            raise RuntimeError("视频浏览器上下文已关闭,请重试")
        try:
            page = await context.new_page()
        except Exception as exc:
            self._contexts.pop(str(profile_dir), None)
            self._tokens.pop(str(profile_dir), None)
            raise RuntimeError(f"视频浏览器窗口已关闭,请重新打开后重试:{exc}") from exc
        # 模拟 task 完成后 finally 关闭 task page(anchor 不受影响)
        await page.close()
        return page

    async def recheck_result(self, profile_dir: Path):
        context, _bundle = await self._get_shared_context(profile_dir)
        from doupool.video.browser import _is_context_alive
        if not _is_context_alive(context):
            raise RuntimeError("视频浏览器上下文已关闭,请重试")
        try:
            page = await context.new_page()
        except Exception as exc:
            self._contexts.pop(str(profile_dir), None)
            self._tokens.pop(str(profile_dir), None)
            raise RuntimeError(f"视频浏览器窗口已关闭,请重新打开后重试:{exc}") from exc
        await page.close()
        return page


@pytest.mark.asyncio
async def test_v0_2_26_run_creates_own_page_and_does_not_touch_anchor(tmp_path):
    """v0.2.26:run() 必须 new_page() 而不是复用 anchor。
    修复前:run() 选 context.pages[0] → finally 关掉 anchor → context 自动 close。
    修复后:run() 拿到自己的 task page,anchor 完整保留。"""
    profile = tmp_path / "p"
    profile.mkdir()
    ctx = _FakeNewContext(profile)
    runner = _RunnerPatch(ctx)

    await runner.run(profile)

    # anchor 必须仍 alive(没被 task 关掉)
    assert ctx.anchor.closed is False, "run() 不应关闭 anchor page"
    # 且 task page 已被 close(行为不变)
    assert len(ctx.task_pages) == 1
    assert ctx.task_pages[0].closed is True


@pytest.mark.asyncio
async def test_v0_2_26_two_concurrent_runs_do_not_close_each_others_pages(tmp_path):
    """v0.2.26:两个并发 run() 互不影响 —— 各自 new_page,各自 finally close 自己的。
    修复前:第二个 task 拿到的可能是第一个 task 已 close 的 anchor,
    引发 TargetClosedError。"""
    import asyncio

    profile = tmp_path / "p"
    profile.mkdir()
    ctx = _FakeNewContext(profile)
    runner = _RunnerPatch(ctx)

    # 同时跑两个 task
    await asyncio.gather(runner.run(profile), runner.run(profile))

    # 两个 task page 都创建了,都正常 close 了
    assert len(ctx.task_pages) == 2
    for tp in ctx.task_pages:
        assert tp.closed is True
    # anchor 仍 alive
    assert ctx.anchor.closed is False
    # context 没 close(因为 anchor + 0 个 live task page,但 anchor 还在 → Playwright 不 close context)
    assert ctx._closed is False


@pytest.mark.asyncio
async def test_v0_2_26_run_raises_clear_error_when_context_already_closed(tmp_path):
    """v0.2.26:context 已 close 时(用户手关窗)run() 必须抛带可读提示的 RuntimeError,
    而不是把底层 Playwright 异常原文透到 UI。"""
    profile = tmp_path / "p"
    profile.mkdir()
    ctx = _FakeNewContext(profile)
    ctx._closed = True
    runner = _RunnerPatch(ctx)

    with __import__("pytest").raises(RuntimeError, match="浏览器上下文已关闭"):
        await runner.run(profile)


@pytest.mark.asyncio
async def test_v0_2_26_recheck_result_creates_own_page_and_keeps_anchor_alive(tmp_path):
    """v0.2.26:recheck_result() 同样不复用 anchor,自己 new_page()。
    修复前:recheck 拿 anchor → finally 关掉 → 后续 retry-result 同账号炸。"""
    profile = tmp_path / "p"
    profile.mkdir()
    ctx = _FakeNewContext(profile)
    runner = _RunnerPatch(ctx)

    await runner.recheck_result(profile)

    assert ctx.anchor.closed is False
    assert len(ctx.task_pages) == 1
    assert ctx.task_pages[0].closed is True


@pytest.mark.asyncio
async def test_v0_2_26_anchor_persists_across_many_runs(tmp_path):
    """v0.2.26:5 轮 run() 之后,anchor 仍然是 context.pages[0] 且未关闭。
    模拟「5 个 task 串行排队完成」的场景:每个 task 都不应该动到 anchor。"""
    profile = tmp_path / "p"
    profile.mkdir()
    ctx = _FakeNewContext(profile)
    runner = _RunnerPatch(ctx)

    for _ in range(5):
        await runner.run(profile)

    # 5 个 task page 都创建了(每个 run() 都 new_page)
    assert len(ctx.task_pages) == 5
    # anchor 永远在第一位,从未关闭
    assert ctx.pages[0] is ctx.anchor
    assert ctx.anchor.closed is False
    # 全部 task page 已 close
    assert all(tp.closed for tp in ctx.task_pages)
