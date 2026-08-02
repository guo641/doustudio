"""
zhuceka 去水印模块测试

覆盖:
1. 必传参数校验(没 uid/key 时抛 ZhucekaConfigError)
2. 正常响应:code=200 + data.video → 返回直链
3. 异常响应:code≠200 → 抛 ZhucekaResponseError
4. 异常响应:code=200 但 data 缺 video → 兜底遍历 / 抛错
5. 网络异常:HTTP 500 → 抛 ZhucekaError
6. 非 JSON 响应 → 抛 ZhucekaResponseError
7. 重试:连续失败后抛最后一次错误(用 monkeypatch 加快)
"""

from __future__ import annotations

import pytest

from doupool.watermark import (
    ZhucekaConfigError,
    ZhucekaError,
    ZhucekaResponseError,
    resolve_clean_url,
)
from doupool.watermark.zhuceka import (
    RETRY_DELAYS_SECONDS,
    _extract_video_url,
    resolve_clean_url_once,
)


# ---------- _extract_video_url 单元测试(纯函数,不需要 mock) ----------


def test_extract_video_url_happy_path():
    payload = {"code": 200, "msg": "ok", "data": {"video": "https://cdn.example.com/x.mp4", "cover": "https://x"}}
    assert _extract_video_url(payload) == "https://cdn.example.com/x.mp4"


def test_extract_video_url_code_not_200():
    with pytest.raises(ZhucekaResponseError, match="余额不足"):
        _extract_video_url({"code": 401, "msg": "余额不足", "data": {}})


def test_extract_video_url_missing_video_falls_back_to_walk():
    """data 没 video 字段,但 data.url 是 mp4 直链,递归遍历兜底拿到"""
    payload = {"code": 200, "data": {"url": "https://cdn.example.com/abc.mp4?sign=xxx"}}
    assert _extract_video_url(payload) == "https://cdn.example.com/abc.mp4?sign=xxx"


def test_extract_video_url_returns_none_when_no_video_anywhere():
    payload = {"code": 200, "data": {"title": "hello", "cover": "https://x.png"}}
    assert _extract_video_url(payload) is None


def test_extract_video_url_non_dict():
    assert _extract_video_url([]) is None
    assert _extract_video_url(None) is None


# ---------- resolve_clean_url_once(用 respx 风格的内置 fake httpx) ----------


class _FakeResponse:
    def __init__(self, *, status_code=200, json_payload=None, text=""):
        self.status_code = status_code
        self._json = json_payload
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("not json")
        return self._json


class _FakeAsyncClient:
    """只捕获最后一次 get 的 url+params,返回预设响应"""

    def __init__(self, *, status_code=200, json_payload=None, text=""):
        self.status_code = status_code
        self.json_payload = json_payload
        self.text = text
        self.last_url = None
        self.last_params = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def get(self, url, params=None, headers=None):
        self.last_url = url
        self.last_params = params
        return _FakeResponse(
            status_code=self.status_code,
            json_payload=self.json_payload,
            text=self.text,
        )


@pytest.mark.asyncio
async def test_resolve_once_happy_path(monkeypatch):
    fake = _FakeAsyncClient(json_payload={"code": 200, "data": {"video": "https://x/y.mp4"}})

    def _factory(**kwargs):
        return fake

    monkeypatch.setattr("doupool.watermark.zhuceka.httpx.AsyncClient", _factory)
    url = await resolve_clean_url_once("https://www.doubao.com/thread/abc", uid="u1", key="k1")
    assert url == "https://x/y.mp4"
    assert fake.last_params == {"type": "dsp", "uid": "u1", "key": "k1", "url": "https://www.doubao.com/thread/abc"}


@pytest.mark.asyncio
async def test_resolve_once_missing_uid_or_key():
    with pytest.raises(ZhucekaConfigError, match="未配置"):
        await resolve_clean_url_once("https://x", uid="", key="k")
    with pytest.raises(ZhucekaConfigError):
        await resolve_clean_url_once("https://x", uid="u", key="")


@pytest.mark.asyncio
async def test_resolve_once_http_500(monkeypatch):
    fake = _FakeAsyncClient(status_code=500, text="server error")
    monkeypatch.setattr("doupool.watermark.zhuceka.httpx.AsyncClient", lambda **kw: fake)
    with pytest.raises(ZhucekaError, match="HTTP 500"):
        await resolve_clean_url_once("https://x", uid="u", key="k")


@pytest.mark.asyncio
async def test_resolve_once_non_json(monkeypatch):
    fake = _FakeAsyncClient(status_code=200, text="<html>not json</html>")
    monkeypatch.setattr("doupool.watermark.zhuceka.httpx.AsyncClient", lambda **kw: fake)
    with pytest.raises(ZhucekaResponseError, match="非 JSON"):
        await resolve_clean_url_once("https://x", uid="u", key="k")


@pytest.mark.asyncio
async def test_resolve_once_no_video_in_payload(monkeypatch):
    fake = _FakeAsyncClient(json_payload={"code": 200, "data": {"title": "无 video 字段"}})
    monkeypatch.setattr("doupool.watermark.zhuceka.httpx.AsyncClient", lambda **kw: fake)
    with pytest.raises(ZhucekaResponseError, match="缺少 video"):
        await resolve_clean_url_once("https://x", uid="u", key="k")


# ---------- resolve_clean_url 带重试 ----------


@pytest.mark.asyncio
async def test_resolve_retry_then_succeed(monkeypatch):
    """第一次 HTTP 500,第二次成功 → 最终拿到 url"""
    call_count = {"n": 0}

    class _AltFake(_FakeAsyncClient):
        async def get(self, url, params=None, headers=None):
            call_count["n"] += 1
            if call_count["n"] == 1:
                return _FakeResponse(status_code=500, text="oops")
            return _FakeResponse(json_payload={"code": 200, "data": {"video": "https://ok"}})

    monkeypatch.setattr("doupool.watermark.zhuceka.httpx.AsyncClient", lambda **kw: _AltFake())
    # 强制缩短 retry 间隔,加速测试
    monkeypatch.setattr("doupool.watermark.zhuceka.RETRY_DELAYS_SECONDS", (0, 0, 0))

    url = await resolve_clean_url("https://x", uid="u", key="k")
    assert url == "https://ok"
    assert call_count["n"] == 2


@pytest.mark.asyncio
async def test_resolve_retry_exhausted(monkeypatch):
    """连续 3 次失败 → 抛最后一次错误"""
    fake = _FakeAsyncClient(status_code=500, text="boom")
    monkeypatch.setattr("doupool.watermark.zhuceka.httpx.AsyncClient", lambda **kw: fake)
    monkeypatch.setattr("doupool.watermark.zhuceka.RETRY_DELAYS_SECONDS", (0, 0, 0))

    with pytest.raises(ZhucekaError):
        await resolve_clean_url("https://x", uid="u", key="k", retries=3)


@pytest.mark.asyncio
async def test_resolve_config_error_does_not_retry(monkeypatch):
    """没配 key 不应该重试,立即抛 ZhucekaConfigError"""
    call_count = {"n": 0}

    def _factory(**kwargs):
        call_count["n"] += 1
        return _FakeAsyncClient(json_payload={"code": 200, "data": {"video": "https://x"}})

    monkeypatch.setattr("doupool.watermark.zhuceka.httpx.AsyncClient", _factory)
    with pytest.raises(ZhucekaConfigError):
        await resolve_clean_url("https://x", uid="", key="k")
    assert call_count["n"] == 0  # 一次都没调


# ---------- 默认重试序列 ----------


def test_default_retry_delays_are_reasonable():
    """默认重试序列应在 ~1 分钟内,适合网络抖动场景"""
    assert RETRY_DELAYS_SECONDS[0] == 0
    assert max(RETRY_DELAYS_SECONDS) <= 60
    assert len(RETRY_DELAYS_SECONDS) >= 3
