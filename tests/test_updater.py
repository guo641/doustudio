"""updater 模块单元测试"""

from __future__ import annotations

import pytest

from doupool.updater import (
    UpdateInfo,
    check_for_update,
    detect_platform,
    parse_version,
    schedule_background_check,
    _platform_from_asset_name,
)


# ---------- parse_version ----------


class TestParseVersion:
    def test_simple(self):
        assert parse_version("v0.2.0") == (0, 2, 0)

    def test_uppercase(self):
        assert parse_version("V1.10.5") == (1, 10, 5)

    def test_two_part(self):
        assert parse_version("v2.0") == (2, 0)

    def test_with_suffix(self):
        assert parse_version("v0.2.0-beta") == (0, 2, 0)

    def test_empty(self):
        assert parse_version("") == (0,)

    def test_garbage(self):
        assert parse_version("xxx") == (0,)


# ---------- 比较 ----------


class TestCompareVersion:
    def test_newer(self):
        assert parse_version("v0.3.0") > parse_version("v0.2.0")

    def test_older(self):
        assert parse_version("v0.2.0") < parse_version("v0.3.0")

    def test_equal(self):
        assert parse_version("v0.2.0") == parse_version("v0.2.0")

    def test_minor_only(self):
        # 0.2.10 > 0.2.9 因为是元组比较
        assert parse_version("v0.2.10") > parse_version("v0.2.9")


# ---------- _platform_from_asset_name ----------


class TestPlatformFromAsset:
    def test_windows(self):
        assert _platform_from_asset_name("DouStudio-v0.2.0-windows-x86_64.zip") == "windows-x86_64"

    def test_linux(self):
        assert _platform_from_asset_name("DouStudio-v0.2.0-linux-x86_64.zip") == "linux-x86_64"

    def test_macos_arm(self):
        assert _platform_from_asset_name("DouStudio-v0.2.0-macos-arm64.zip") == "macos-arm64"

    def test_macos_intel(self):
        assert _platform_from_asset_name("DouStudio-v0.2.0-macos-x86_64.zip") == "macos-x86_64"

    def test_non_zip(self):
        assert _platform_from_asset_name("DouStudio-v0.2.0.tar.gz") is None

    def test_wrong_prefix(self):
        assert _platform_from_asset_name("OtherApp-v0.2.0-windows-x86_64.zip") is None


# ---------- detect_platform(返回值形态) ----------


class TestDetectPlatform:
    def test_returns_string(self):
        plat = detect_platform()
        assert isinstance(plat, str)
        assert "-" in plat
        # 形如 "linux-x86_64" / "windows-x86_64" / "darwin-arm64"
        system, _, arch = plat.partition("-")
        assert system in {"linux", "windows", "darwin"}
        assert arch


# ---------- check_for_update(用 monkeypatch httpx) ----------


class _FakeAsyncClient:
    def __init__(self, *, status=200, payload=None, exc=None):
        self.status = status
        self.payload = payload
        self.exc = exc

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, *args, **kwargs):
        if self.exc is not None:
            raise self.exc
        # 模拟 httpx.Response
        class _Resp:
            def __init__(self, status, payload):
                self.status_code = status
                self._payload = payload

            def json(self):
                if self._payload is None:
                    raise ValueError("not json")
                return self._payload

        return _Resp(self.status, self.payload)


@pytest.mark.asyncio
async def test_check_update_finds_newer_version(monkeypatch):
    payload = {
        "tag_name": "v0.3.0",
        "html_url": "https://github.com/guo641/doustudio/releases/tag/v0.3.0",
        "body": "新版本发布",
        "assets": [
            {"name": "DouStudio-v0.3.0-windows-x86_64.zip", "browser_download_url": "https://x/y.zip"}
        ],
    }
    monkeypatch.setattr("doupool.updater.httpx.AsyncClient", lambda **kw: _FakeAsyncClient(status=200, payload=payload))
    info = await check_for_update("0.2.0")
    assert info.has_update is True
    assert info.latest_version == "v0.3.0"
    assert info.release_notes == "新版本发布"
    assert "windows-x86_64" in info.asset_urls


@pytest.mark.asyncio
async def test_check_update_no_update(monkeypatch):
    payload = {
        "tag_name": "v0.2.0",
        "html_url": "https://x",
        "body": "",
        "assets": [],
    }
    monkeypatch.setattr("doupool.updater.httpx.AsyncClient", lambda **kw: _FakeAsyncClient(status=200, payload=payload))
    info = await check_for_update("0.2.0")
    assert info.has_update is False
    assert info.latest_version == "v0.2.0"
    assert info.asset_urls == {}


@pytest.mark.asyncio
async def test_check_update_rate_limited(monkeypatch):
    monkeypatch.setattr("doupool.updater.httpx.AsyncClient", lambda **kw: _FakeAsyncClient(status=403))
    info = await check_for_update("0.2.0")
    assert info.has_update is False
    assert info.latest_version == "0.2.0"  # fallback to current


@pytest.mark.asyncio
async def test_check_update_network_error(monkeypatch):
    monkeypatch.setattr(
        "doupool.updater.httpx.AsyncClient",
        lambda **kw: _FakeAsyncClient(exc=Exception("connection reset")),
    )
    info = await check_for_update("0.2.0")
    assert info.has_update is False
    assert info.latest_version == "0.2.0"


@pytest.mark.asyncio
async def test_check_update_server_error(monkeypatch):
    monkeypatch.setattr("doupool.updater.httpx.AsyncClient", lambda **kw: _FakeAsyncClient(status=500))
    info = await check_for_update("0.2.0")
    assert info.has_update is False


@pytest.mark.asyncio
async def test_check_update_non_json(monkeypatch):
    monkeypatch.setattr("doupool.updater.httpx.AsyncClient", lambda **kw: _FakeAsyncClient(status=200, payload=None))
    info = await check_for_update("0.2.0")
    assert info.has_update is False


# ---------- schedule_background_check(确保不抛) ----------


def test_schedule_background_check_runs_callback():
    captured: list[UpdateInfo] = []
    schedule_background_check("0.1.0", lambda info: captured.append(info))
    # 等几秒让后台线程跑完(本地网络请求可能失败,但 callback 必须被调)
    import time
    for _ in range(50):
        if captured:
            break
        time.sleep(0.1)
    assert len(captured) == 1
    assert captured[0].current_version == "0.1.0"
