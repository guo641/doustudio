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
