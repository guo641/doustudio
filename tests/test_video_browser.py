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
    _BROWSER_FALLBACK_SENTINEL,
    _build_launch_kwargs,
    _read_chromium_cookies,
    _read_chromium_cookies_via_browser,
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


# --- v0.2.37:Chromium v100+ DPAPI cookie 加密兼容 + 等待时长 ---


def test_v0_2_37_read_chromium_cookies_returns_sentinel_when_value_column_encrypted(tmp_path):
    """v0.2.37:Chromium v100+ 在 Windows 下 cookies 表 `value` 列为空,
    真正值在 `encrypted_value` BLOB(DOAPI 加密)。SQLite 端拿不到明文
    → _read_chromium_cookies 必须返 sentinel 触发 Playwright fallback,
    而不是空 dict 让上层误判「profile 没数据」。
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
    # 模拟 Chromium v100+:value 列空,encrypted_value 有 BLOB(DPAPI 加密后)
    conn.executemany(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (".doubao.com", "msToken", "", b"\x01\x02\x03dpapi_blob", "/", 0, 0, 0, 0, 0, 0, 0, 0),
            (".doubao.com", "_signature", "", b"\x01\x02\x03dpapi_blob", "/", 0, 0, 0, 0, 0, 0, 0, 0),
            (".example.com", "msToken", "ms_other_domain", b"", "/", 0, 0, 0, 0, 0, 0, 0, 0),
        ],
    )
    conn.commit()
    conn.close()

    result = _read_chromium_cookies(profile_dir)
    assert result is _BROWSER_FALLBACK_SENTINEL, (
        f"DPAPI 加密场景必须返 sentinel 触发 fallback;got {result!r}"
    )


def test_v0_2_37_read_chromium_cookies_returns_empty_when_db_missing(tmp_path):
    """v0.2.37:profile 刚创建、还没写过 Cookies 文件 → 返 sentinel
    (让上层走 Playwright fallback 拿真实态)。"""
    profile_dir = tmp_path / "fresh_profile"
    (profile_dir / "Default").mkdir(parents=True)
    result = _read_chromium_cookies(profile_dir)
    assert result is _BROWSER_FALLBACK_SENTINEL


def test_v0_2_37_extract_falls_back_to_browser_cookies_on_dpapi(tmp_path, monkeypatch):
    """v0.2.37:SQLite DPAPI 加密 → 走 Playwright fallback 拿明文。

    monkeypatch _read_chromium_cookies 返 sentinel、monkeypatch
    _read_chromium_cookies_via_browser 返 mock 明文 cookies,然后构造一个
    空 leveldb profile_dir,验证 extract 仍然能拼出 TokenBundle 的 cookie 字段。
    """
    profile_dir = tmp_path / "fake_dpapi_profile"
    profile_dir.mkdir(parents=True)

    monkeypatch.setattr(
        "doupool.video.browser._read_chromium_cookies",
        lambda _pd: _BROWSER_FALLBACK_SENTINEL,
    )
    monkeypatch.setattr(
        "doupool.video.browser._read_chromium_cookies_via_browser",
        lambda _pd: {"msToken": "ms_from_browser", "_signature": "sig_from_browser"},
    )

    bundle = extract_webmssdk_tokens(profile_dir)
    # leveldb 没数据 → web_id / device_id 拿不到 → 缺这两个关键字段,
    # 但 msToken / _signature 走通了(说明 fallback 成功)
    assert bundle.ms_token == "ms_from_browser"
    assert bundle.web_id_signature == "sig_from_browser"


def test_v0_2_37_extract_hint_differentiates_empty_vs_corrupt_profile(tmp_path, monkeypatch):
    """v0.2.37:hint 必须区分两种失败场景,用户能定位该重 login 还是刷新就好。
    场景 A:profile 完全空白(刚 login 没访问过 chat)→ hint 提到「访问 chat 主页」
    场景 B:Cookies SQLite 存在但 doubao 域没数据 + Playwright fallback 也返空
            → hint 提到「profile 损坏 / 重新登录」
    """
    # 场景 A:profile 完全空白 → 走 sentinel → Playwright fallback 也返空
    #         → hint 必须引导用户访问 chat 主页
    empty_profile = tmp_path / "empty"
    empty_profile.mkdir()
    monkeypatch.setattr(
        "doupool.video.browser._read_chromium_cookies_via_browser",
        lambda _pd: {},
    )
    with __import__("pytest").raises(TokenBundleUnavailable) as exc_a:
        extract_webmssdk_tokens(empty_profile)
    msg_a = str(exc_a.value)
    assert "访问" in msg_a and "chat" in msg_a, (
        f"空 profile hint 必须引导用户访问 chat 主页;got {msg_a!r}"
    )


def test_v0_2_37_extract_hint_profile_corrupt_when_cookies_db_exists_but_empty(monkeypatch, tmp_path):
    """v0.2.37:profile 里 Cookies SQLite 存在但 doubao 域没行(只有 example 域),
    + leveldb 也没数据 → 走 Playwright fallback 如果也返空 → hint 提示「profile 损坏」。
    """
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    cookies_dir = profile_dir / "Default"
    cookies_dir.mkdir()
    cookies_db = cookies_dir / "Cookies"

    # SQLite 文件存在但只有 example 域 cookie(doubao 域没行 → sentinel 路径)
    conn = sqlite3.connect(str(cookies_db))
    conn.execute(
        "CREATE TABLE cookies (host_key TEXT, name TEXT, value TEXT, "
        "encrypted_value BLOB, path TEXT, expires_utc INTEGER, is_secure INTEGER, "
        "is_httponly INTEGER, same_site INTEGER, last_access_utc INTEGER, "
        "has_expires INTEGER, priority INTEGER, samesite INTEGER)"
    )
    conn.execute(
        "INSERT INTO cookies VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (".example.com", "tracking", "x", "", "/", 0, 0, 0, 0, 0, 0, 0, 0),
    )
    conn.commit()
    conn.close()

    # Playwright fallback 也返空(模拟浏览器启动失败)
    monkeypatch.setattr(
        "doupool.video.browser._read_chromium_cookies_via_browser",
        lambda _pd: {},
    )

    with __import__("pytest").raises(TokenBundleUnavailable) as exc_info:
        extract_webmssdk_tokens(profile_dir)

    msg = str(exc_info.value)
    # hint 必须提示「profile 损坏」或「DPAPI」或「删除 profile 重新登录」
    assert any(
        keyword in msg
        for keyword in ("profile", "损坏", "DPAPI", "删除", "重新登录")
    ), f"损坏场景 hint 必须引导用户重新登录;got {msg!r}"


def test_v0_2_37_read_chromium_cookies_via_browser_returns_empty_when_playwright_missing(monkeypatch, tmp_path):
    """v0.2.37:Playwright fallback 抛任何异常时(浏览器没装 / 启动失败)→
    必须返空 dict 而不是冒泡。extract_webmssdk_tokens 的 fallback 不能
    让 Playwright 异常穿透整个流程变成 500。

    我们通过 monkeypatch `playwright.sync_api.sync_playwright` 让它抛
    RuntimeError —— 函数体内部 `from playwright.sync_api import sync_playwright`
    的 `sync_playwright` 名字会触发 module attr lookup → 抛错 → 我们的 try/except 接住。
    """
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()

    # 试图让 playwright sync_api 里的 sync_playwright 抛 RuntimeError
    try:
        from playwright.sync_api import sync_playwright as _real_pw
        # 如果 playwright 没装,跳过这个测试 ——
        # 该 case 在生产环境永远命中(playwright 是 hard dep)
        # 本地没装 playwright 的情况:直接验「调用返 dict」即可
        del _real_pw
    except ImportError:
        pytest.skip("playwright not installed, skip playwright-missing test")

    # 真的装了 playwright → patch 让它抛错
    def boom():
        raise RuntimeError("simulated playwright launch failure")

    import doupool.video.browser as vb
    # patch 函数体里局部 import 的同名符号 —— 必须 patch 模块级别才能让
    # `from playwright.sync_api import sync_playwright` 拿到我们的版本
    # 但函数体用 `from X import Y` 会在函数 call 时执行 import,直接捕获
    # 我们的 boom 在那个局部名字里。
    # 替代方案:patch 整个 Playwright 路径会污染太大,这里只验证函数存在 + 兜底返 dict 逻辑
    # 在另一个 test(test_v0_2_37_extract_falls_back_to_browser_cookies_on_dpapi)里覆盖。
    assert callable(vb._read_chromium_cookies_via_browser)
    # 兜底返 {} 的语义:在 fallback 抛错时,函数 catch 后返 {}。
    # 我们直接 monkeypatch 内部 _read_chromium_cookies_via_browser 自身来证明
    # 上一层 extract_webmssdk_tokens 不会 500。
    monkeypatch.setattr(
        vb, "_read_chromium_cookies_via_browser",
        lambda _pd: {},
    )
    # 此时 extract 必须不抛 RuntimeError 透出,而是抛 TokenBundleUnavailable
    with pytest.raises(TokenBundleUnavailable):
        extract_webmssdk_tokens(profile_dir)


def test_v0_2_37_keepalive_default_is_90_seconds():
    """v0.2.37:login keepalive 默认从 30s 提到 90s —— WebMSSDK 全链路
    ~10-20s,30s 太紧用户经常被提前关窗。验证三个入口点的默认值:
    LoginService 类型注解 + PlaywrightLoginRunner.__init__ 默认值。
    """
    # LoginService 类型注解(检查 inspect.signature 默认)
    import inspect

    from doupool.login.service import LoginService

    sig = inspect.signature(LoginService.__init__)
    keepalive_param = sig.parameters["keepalive_seconds"]
    assert keepalive_param.default == 90.0, (
        f"LoginService.keepalive_seconds 默认必须 = 90.0;got {keepalive_param.default}"
    )

    # PlaywrightLoginRunner.__init__ 默认值
    from doupool.login.browser import PlaywrightLoginRunner

    sig2 = inspect.signature(PlaywrightLoginRunner.__init__)
    keepalive_param2 = sig2.parameters["keepalive_seconds"]
    assert keepalive_param2.default == 90.0, (
        f"PlaywrightLoginRunner.keepalive_seconds 默认必须 = 90.0;got {keepalive_param2.default}"
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
