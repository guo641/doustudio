"""v0.2.9:callbackUrl 异步回执调度器单元测试。

覆盖 callbacks.build_payload / dispatch 的关键分支:
  - payload shape 跟 yaonieyo 对齐(snake_case 字段)
  - 空 callback_url → 标 disabled,不发请求
  - 非法 scheme (file://) → 标 failed,不发请求
  - 第 1 次 POST 成功 → 标 succeeded,attempts=1
  - 第 1 次失败,第 2 次成功 → 标 succeeded,attempts=2
  - 全部 3 次失败 → 标 failed,attempts=3
  - 任意未捕获异常吞掉,不抛
  - update() 在每次尝试都调一次(attempts / last_error 实时写库)

不依赖 peewee 模型,直接构造一个 SimpleNamespace 充当 VideoTask,
让 callbacks.dispatch 完全黑盒测。
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from doupool import callbacks


# ---- 工具 ----

def _make_task(callback_url: str | None, callback_status=None, **overrides):
    """构造一个回调测试用的简易 task 对象。字段够 callbacks 读即可。"""
    fields = dict(
        id="task-abc",
        status="succeeded",
        result_url="https://result.example.com/a.mp4",
        backup_result_url="https://backup.example.com/a.mp4",
        cover_url="https://cover.example.com/a.jpg",
        error_message=None,
        conversation_id="conv-1",
        remote_task_id="rt-1",
        callback_url=callback_url,
        callback_status=callback_status,
        callback_attempts=0,
        callback_last_error=None,
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


class _RecordingUpdate:
    """记录每次 update() 调用,kwargs 暴露给测试断言。"""
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)


class _FakeClient:
    """httpx.AsyncClient 替身:可按 URL 路径配置多次响应。"""
    def __init__(self, responses: list[Exception] | None = None):
        self.responses = responses or []
        self.calls: list[dict] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, url, content=None, headers=None):
        self.calls.append({"url": url, "content": content, "headers": headers})
        if not self.responses:
            return SimpleNamespace(status_code=200, raise_for_status=lambda: None)
        # 每次取下一条响应(异常对象)
        next_resp = self.responses.pop(0)
        if isinstance(next_resp, Exception):
            raise next_resp
        return next_resp


def _factory_for(client: _FakeClient):
    return lambda: client


# ---- payload shape ----

def test_build_payload_snake_case_keys():
    """build_payload 必须是 snake_case 字段名,跟 yaonieyo 兼容。"""
    task = _make_task("https://x", callback_status="sending")
    payload = callbacks.build_payload(task)
    assert payload == {
        "task_id": "task-abc",
        "status": "succeeded",
        "result_url": "https://result.example.com/a.mp4",
        "backup_result_url": "https://backup.example.com/a.mp4",
        "cover_url": "https://cover.example.com/a.jpg",
        "error_message": None,
        "conversation_id": "conv-1",
        "remote_task_id": "rt-1",
    }


def test_build_payload_with_failed_status_keeps_error_message():
    task = _make_task("https://x", status="failed", error_message="upstream timeout")
    payload = callbacks.build_payload(task)
    assert payload["status"] == "failed"
    assert payload["error_message"] == "upstream timeout"


# ---- 空 callback_url → disabled ----

@pytest.mark.asyncio
async def test_dispatch_empty_url_marks_disabled_and_skips_post():
    update = _RecordingUpdate()
    task = _make_task("")
    factory = lambda: pytest.fail("没设 callback_url 不该构造 httpx 客户端")

    outcome = await callbacks.dispatch(task, update, client_factory=factory)

    assert outcome.delivered is False
    assert outcome.attempts == 0
    assert outcome.last_error is None
    # 唯一一次 update:callback_status=disabled
    assert update.calls == [{"callback_status": "disabled", "callback_last_error": None}]


@pytest.mark.asyncio
async def test_dispatch_empty_url_already_disabled_no_update():
    """callback_status 已经 'disabled' 就不要再写一次(update() 会顺带改 updated_at)"""
    update = _RecordingUpdate()
    task = _make_task("", callback_status="disabled")

    await callbacks.dispatch(task, update, client_factory=lambda: pytest.fail())

    assert update.calls == []


@pytest.mark.asyncio
async def test_dispatch_whitespace_only_url_treated_as_empty():
    update = _RecordingUpdate()
    task = _make_task("   ")
    await callbacks.dispatch(task, update, client_factory=lambda: pytest.fail())
    assert update.calls[0]["callback_status"] == "disabled"


# ---- 非法 scheme ----

@pytest.mark.asyncio
async def test_dispatch_unsupported_scheme_marks_failed_without_post():
    update = _RecordingUpdate()
    task = _make_task("file:///etc/passwd")
    factory = lambda: pytest.fail("非法 scheme 不该构造客户端")

    outcome = await callbacks.dispatch(task, update, client_factory=factory)

    assert outcome.delivered is False
    assert outcome.attempts == 0
    assert outcome.last_error == "unsupported scheme"
    assert update.calls[0]["callback_status"] == "failed"
    assert "unsupported scheme" in update.calls[0]["callback_last_error"]


@pytest.mark.asyncio
async def test_dispatch_gopher_scheme_rejected():
    update = _RecordingUpdate()
    task = _make_task("gopher://attacker.example/")
    await callbacks.dispatch(task, update, client_factory=lambda: pytest.fail())
    assert update.calls[0]["callback_status"] == "failed"


# ---- 成功投递 ----

@pytest.mark.asyncio
async def test_dispatch_success_first_attempt_marks_succeeded_attempts1():
    update = _RecordingUpdate()
    task = _make_task("https://hook.example.com/cb")
    client = _FakeClient(responses=[])

    outcome = await callbacks.dispatch(task, update, client_factory=_factory_for(client))

    assert outcome.delivered is True
    assert outcome.attempts == 1
    assert outcome.last_error is None
    # 至少:1 次 'sending' + 1 次 'succeeded'
    statuses = [c.get("callback_status") for c in update.calls]
    assert "sending" in statuses
    assert statuses[-1] == "succeeded"
    # attempts 在最后一条 update 里 = 1
    assert update.calls[-1]["callback_attempts"] == 1
    assert update.calls[-1]["callback_last_error"] is None
    # 只发了 1 次 POST
    assert len(client.calls) == 1
    sent_payload = json.loads(client.calls[0]["content"])
    assert sent_payload["task_id"] == "task-abc"
    assert client.calls[0]["headers"]["Content-Type"] == "application/json"


@pytest.mark.asyncio
async def test_dispatch_uses_snake_case_payload_yaonieyo_compatible():
    update = _RecordingUpdate()
    task = _make_task("https://hook.example.com/cb")
    client = _FakeClient(responses=[])

    await callbacks.dispatch(task, update, client_factory=_factory_for(client))

    payload = json.loads(client.calls[0]["content"])
    # 必须 snake_case,不能驼峰
    assert "taskId" not in payload
    assert "remoteTaskId" not in payload
    assert set(payload.keys()) == {
        "task_id", "status", "result_url", "backup_result_url",
        "cover_url", "error_message", "conversation_id", "remote_task_id",
    }


# ---- 重试 ----

@pytest.mark.asyncio
async def test_dispatch_retry_after_first_http_error(monkeypatch):
    """第 1 次 httpx.HTTPError → 等 5s 重试 → 第 2 次成功。
    为避免测试等 5s,monkeypatch asyncio.sleep 直接 pass。
    """
    update = _RecordingUpdate()
    task = _make_task("https://hook.example.com/cb")
    # 第一次抛 ConnectError,第二次(默认)成功
    client = _FakeClient(responses=[httpx.ConnectError("connect failed")])

    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(callbacks.asyncio, "sleep", _noop_sleep)

    outcome = await callbacks.dispatch(task, update, client_factory=_factory_for(client))

    assert outcome.delivered is True
    assert outcome.attempts == 2
    assert len(client.calls) == 2
    # 第 1 次失败时 attempts=1 + last_error 非空(写库了)
    # 跳过 'sending' 那条初始化 update(callback_attempts=0),从重试记录里取
    retry_updates = [
        c for c in update.calls
        if c.get("callback_attempts", 0) > 0 or c.get("callback_last_error")
    ]
    assert retry_updates[0]["callback_attempts"] == 1
    assert "ConnectError" in retry_updates[0]["callback_last_error"]
    # 第 2 次成功后 status=succeeded + attempts=2 + last_error=None
    assert update.calls[-1]["callback_status"] == "succeeded"
    assert update.calls[-1]["callback_attempts"] == 2
    assert update.calls[-1]["callback_last_error"] is None


@pytest.mark.asyncio
async def test_dispatch_exhausted_retries_marks_failed_attempts3(monkeypatch):
    """3 次都失败 → status=failed, attempts=3, last_error 保留最后一次。"""
    update = _RecordingUpdate()
    task = _make_task("https://hook.example.com/cb")
    client = _FakeClient(responses=[
        httpx.ConnectError("c1"),
        httpx.ReadTimeout("c2"),
        httpx.ConnectError("c3"),
    ])

    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(callbacks.asyncio, "sleep", _noop_sleep)

    outcome = await callbacks.dispatch(task, update, client_factory=_factory_for(client))

    assert outcome.delivered is False
    assert outcome.attempts == 3
    assert outcome.last_error is not None
    assert "ConnectError" in outcome.last_error
    assert len(client.calls) == 3
    # 最后一条 update:status=failed + last_error 是第 3 次的错误
    assert update.calls[-1]["callback_status"] == "failed"
    assert "c3" in update.calls[-1]["callback_last_error"]


@pytest.mark.asyncio
async def test_dispatch_unexpected_exception_swallowed(monkeypatch):
    """_post_once 抛非 httpx 异常也吞掉,不让主任务受牵连。"""
    update = _RecordingUpdate()
    task = _make_task("https://hook.example.com/cb")
    # 全部 3 次抛 ValueError,模拟 SDK 内部 bug
    client = _FakeClient(responses=[ValueError("boom")] * 3)

    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(callbacks.asyncio, "sleep", _noop_sleep)

    outcome = await callbacks.dispatch(task, update, client_factory=_factory_for(client))

    assert outcome.delivered is False
    assert outcome.attempts == 3
    assert "unexpected" in (outcome.last_error or "")
    assert update.calls[-1]["callback_status"] == "failed"


# ---- 真实 httpx.AsyncClient 集成(快速 + 模拟 transport) ----

@pytest.mark.asyncio
async def test_dispatch_with_mock_transport_returns_500_then_200(monkeypatch):
    """用 httpx.MockTransport 端到端验证 status_code >= 400 也算失败。"""
    update = _RecordingUpdate()
    task = _make_task("https://hook.example.com/cb")

    call_count = {"n": 0}

    def _handler(request: httpx.Request) -> httpx.Response:
        call_count["n"] += 1
        if call_count["n"] == 1:
            return httpx.Response(500, text="upstream broken")
        return httpx.Response(204)

    transport = httpx.MockTransport(_handler)

    def _factory():
        return httpx.AsyncClient(transport=transport, timeout=8.0, follow_redirects=False)

    async def _noop_sleep(_seconds):
        return None

    monkeypatch.setattr(callbacks.asyncio, "sleep", _noop_sleep)

    outcome = await callbacks.dispatch(task, update, client_factory=_factory)

    assert outcome.delivered is True
    assert outcome.attempts == 2
    assert call_count["n"] == 2
    assert update.calls[-1]["callback_status"] == "succeeded"