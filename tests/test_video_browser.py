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
        (".example.com", "msToken", "ms_other_domain", "", "/", 0, 0, 0, 0, 0, 0, 0, 0),
    ]
    conn.executemany(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows
    )
    conn.commit()
    conn.close()

    # leveldb 路径不存在 → storage 空 → web_id / device_id 拿不到
    bundle = extract_webmssdk_tokens(profile_dir)
    assert bundle.ms_token == "ms_from_sqlite"
    assert bundle.web_id_signature == "sig_from_sqlite"
    assert bundle.device_id == "fp_from_sqlite"  # 兜底 s_v_web_id


def test_extract_webmssdk_tokens_raises_when_profile_dir_missing(tmp_path):
    """v0.2.17:profile 目录里啥都没有 → 抛 TokenBundleUnavailable。"""
    with __import__("pytest").raises(TokenBundleUnavailable, match="web_id"):
        extract_webmssdk_tokens(tmp_path / "empty_profile")


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
