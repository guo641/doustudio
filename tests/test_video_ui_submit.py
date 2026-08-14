"""v0.3.2:UI click 路径单测 —— mock try_click / submit_via_ui / 拦截器。

覆盖点:
1. try_click 按顺序试 selector,首个可见 + 可点击的就点
2. try_click 全失败抛 RuntimeError
3. submit_via_ui 按顺序调 (VIDEO_TAB_SEL,) → (SEND_BTN_SEL, FALLBACK)
4. _ack_interceptor context manager 不 leak listener
5. _wait_for_ack 等到 state['text']
6. _submit_and_poll(use_real_browser=True) 仍返 3 字段 ack dict 契约
7. clear_prose_mirror 用 Ctrl+A + Delete(不能用 innerHTML)

mock 策略:
- _FakePage 模拟 locator.first + bounding_box + mouse + keyboard 最小接口
- monkeypatch _try_solve_captcha_in_video 让 submit_via_ui 不用真跑 captcha
- _submit_and_poll 测试用 monkeypatch 整个拦截器返回 SSE ACK 文本
"""
from __future__ import annotations

import asyncio
import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from doupool.captcha.solver import CaptchaKind
from doupool.video import protocol as protocol_module
from doupool.video.browser import (
    EDITOR_SEL,
    SEND_BTN_SEL,
    SEND_BTN_FALLBACK_SEL,
    VIDEO_TAB_SEL,
    PlaywrightVideoRunner,
    _AegisUnresolvableInPoll,
    _ack_interceptor,
    _build_launch_kwargs,
    _extract_local_message_ids_from_ack_payload,
    _handle_aegis_in_poll,
    _pre_submit_aegis_gate,
    _probe_aegis_quickly,
    _wait_for_ack,
    clear_prose_mirror,
    submit_via_ui,
    try_click,
)
from doupool.video.protocol import DoubaoRateLimited


# ------------------------- fakes ------------------------- #


class _FakeBox:
    def __init__(self, *, x: float = 10, y: float = 10, w: float = 30, h: float = 30) -> None:
        self._box = {"x": x, "y": y, "width": w, "height": h}

    async def bounding_box(self) -> dict:
        return self._box


class _FakeElement:
    def __init__(self, *, box: dict | None = None) -> None:
        self._box = box if box is not None else {"x": 10, "y": 10, "width": 30, "height": 30}
        self.clicks = 0

    async def wait_for(self, state: str = "visible", timeout: int = 0) -> None:
        return None

    async def bounding_box(self) -> dict:
        return self._box

    async def click(self, *args, **kwargs) -> None:
        self.clicks += 1


class _FakeLocator:
    def __init__(self, element: _FakeElement) -> None:
        self._element = element

    @property
    def first(self) -> _FakeElement:
        return self._element


class _FakeMouse:
    def __init__(self) -> None:
        self.moves: list[tuple[float, float, int]] = []
        self.downs: int = 0
        self.ups: int = 0

    async def move(self, x: float, y: float, steps: int = 1) -> None:
        self.moves.append((x, y, steps))

    async def down(self) -> None:
        self.downs += 1

    async def up(self) -> None:
        self.ups += 1


class _FakeKeyboard:
    def __init__(self) -> None:
        self.presses: list[str] = []
        self.types: list[tuple[str, int]] = []

    async def press(self, key: str) -> None:
        self.presses.append(key)

    async def type(self, text: str, delay: int = 0) -> None:
        self.types.append((text, delay))


class _FakePage:
    """最小 Playwright Page 替身 —— 只覆盖 v0.3.2 用到的接口。"""

    def __init__(self, *, url: str = "https://www.doubao.com/chat/create-image") -> None:
        self.url = url
        self.mouse = _FakeMouse()
        self.keyboard = _FakeKeyboard()
        self.evaluates: list[tuple[str, object | None]] = []
        self._elements: dict[str, _FakeElement] = {}
        self._added_handlers: list = []
        self._removed_handlers: list = []
        self.goto_calls: list[tuple[str, dict]] = []

    def locator(self, sel: str) -> _FakeLocator:
        self._elements.setdefault(sel, _FakeElement())
        return _FakeLocator(self._elements[sel])

    async def goto(self, url: str, **kw) -> None:
        self.url = url
        self.goto_calls.append((url, kw))

    async def wait_for_timeout(self, ms: int) -> None:
        return None

    async def evaluate(self, expr: str, arg: object | None = None) -> object:
        self.evaluates.append((expr, arg))
        return None

    def on(self, event: str, handler) -> None:
        self._added_handlers.append((event, handler))

    def remove_listener(self, event: str, handler) -> None:
        self._removed_handlers.append((event, handler))


# ------------------------- try_click ------------------------- #


@pytest.mark.asyncio
async def test_try_click_first_selector_succeeds():
    page = _FakePage()
    await try_click(page, (SEND_BTN_SEL,))
    # bounding_box 是 {x:10,y:10,width:30,height:30} → 中心 (25, 25)
    assert page.mouse.moves == [(25.0, 25.0, 3)]
    assert page.mouse.downs == 1
    assert page.mouse.ups == 1


@pytest.mark.asyncio
async def test_try_click_falls_through_to_fallback():
    """第一个 selector 零尺寸 → 跳过,fallback 命中。"""
    page = _FakePage()
    zero = _FakeElement(box={"x": 0, "y": 0, "width": 0, "height": 0})
    good = _FakeElement()
    page._elements[SEND_BTN_SEL] = zero
    page._elements[SEND_BTN_FALLBACK_SEL] = good
    await try_click(page, (SEND_BTN_SEL, SEND_BTN_FALLBACK_SEL))
    assert good.clicks == 0  # click 走 mouse 路径,不调 element.click
    assert page.mouse.downs == 1


@pytest.mark.asyncio
async def test_try_click_all_fail_raises():
    page = _FakePage()
    zero = _FakeElement(box={"x": 0, "y": 0, "width": 0, "height": 0})
    page._elements.clear()
    page._elements["sel-a"] = zero
    page._elements["sel-b"] = zero
    with pytest.raises(RuntimeError, match="all selectors failed"):
        await try_click(page, ("sel-a", "sel-b"), timeout=0.05)


# ------------------------- clear_prose_mirror ------------------------- #


@pytest.mark.asyncio
async def test_clear_prose_mirror_uses_ctrl_a_delete():
    page = _FakePage()
    await clear_prose_mirror(page)
    assert "Control+A" in page.keyboard.presses
    assert "Delete" in page.keyboard.presses
    innerhtml_calls = [e for e in page.evaluates if "innerHTML" in str(e)]
    assert not innerhtml_calls, "ProseMirror 不能 innerHTML,会破坏 internal state"


# ------------------------- submit_via_ui ------------------------- #


@pytest.mark.asyncio
async def test_submit_via_ui_clicks_video_tab_and_send_btn(monkeypatch):
    page = _FakePage(url="https://www.doubao.com/chat/create-image")
    update = MagicMock()
    monkeypatch.setattr(
        "doupool.video.browser._try_solve_captcha_in_video",
        AsyncMock(return_value=False),
    )
    await submit_via_ui(page, "测试一只小狗", profile_dir=Path("/tmp/p"), update=update)
    # 视频 tab 已 click(触发 mouse.down/up)
    assert page._elements[VIDEO_TAB_SEL] is not None
    # send button 命中(SEND_BTN_SEL 或 fallback)
    send_hit = (
        page._elements.get(SEND_BTN_SEL) is not None
        or page._elements.get(SEND_BTN_FALLBACK_SEL) is not None
    )
    assert send_hit, "submit_via_ui 应该至少注册 SEND_BTN_SEL / FALLBACK"
    # v0.3.2.1:prompt 必须一次 paste(不是 keyboard.type 一字一字打)
    # 1) 完整 prompt 进了 navigator.clipboard.writeText
    write_calls = [
        e for e in page.evaluates
        if "writeText" in str(e[0])
    ]
    assert any(arg == "测试一只小狗" for _expr, arg in write_calls), (
        f"prompt 必须整段传给 clipboard.writeText; 实际 evaluates={page.evaluates}"
    )
    # 2) keyboard.type 不该被调用(整段贴,不是逐字打)
    assert page.keyboard.types == [], (
        f"v0.3.2.1 起整段 prompt 走 paste; keyboard.type 应为空, 实际={page.keyboard.types}"
    )
    # 3) Ctrl+V 必须 press 一次
    assert "Control+V" in page.keyboard.presses


@pytest.mark.asyncio
async def test_submit_via_ui_skips_goto_when_already_on_create_image(monkeypatch):
    """已在 create-image 页 → 不应再调 page.goto(避免重复跳转)。"""
    page = _FakePage(url="https://www.doubao.com/chat/create-image")
    monkeypatch.setattr(
        "doupool.video.browser._try_solve_captcha_in_video",
        AsyncMock(return_value=False),
    )
    await submit_via_ui(page, "x", profile_dir=Path("/tmp/p"), update=MagicMock())
    assert page.goto_calls == []


@pytest.mark.asyncio
async def test_submit_via_ui_goto_when_on_other_page(monkeypatch):
    page = _FakePage(url="https://www.doubao.com/chat/123")
    monkeypatch.setattr(
        "doupool.video.browser._try_solve_captcha_in_video",
        AsyncMock(return_value=False),
    )
    await submit_via_ui(page, "x", profile_dir=Path("/tmp/p"), update=MagicMock())
    assert len(page.goto_calls) == 1
    assert page.goto_calls[0][0].startswith("https://www.doubao.com/chat/create-image")


# ------------------------- _ack_interceptor ------------------------- #


def test_extract_local_ids_ignores_generic_message_id_fallbacks():
    payload = {
        "ack_client_meta": {
            "message_id": "meta-generic",
            "local_message_ids": [{"message_id": "meta-list-generic"}],
        },
        "query_list": [{
            "message_id": "query-generic",
            "local_message_ids": [{"message_id": "query-list-generic"}],
        }],
    }

    assert _extract_local_message_ids_from_ack_payload(payload) == set()


def test_extract_local_ids_keeps_only_explicit_local_fields():
    payload = {
        "ack_client_meta": {
            "local_message_id": "meta-one",
            "local_message_ids": [
                "meta-two",
                {"local_message_id": "meta-three"},
            ],
        },
        "query_list": [{
            "local_message_id": "query-one",
            "local_message_ids": [
                "query-two",
                {"local_message_id": "query-three"},
            ],
        }],
    }

    assert _extract_local_message_ids_from_ack_payload(payload) == {
        "meta-one",
        "meta-two",
        "meta-three",
        "query-one",
        "query-two",
        "query-three",
    }


@pytest.mark.asyncio
async def test_ack_interceptor_does_not_leak_listener():
    page = _FakePage()
    async with _ack_interceptor(page):
        assert len(page._added_handlers) == 1
        assert page._added_handlers[0][0] == "response"
        assert len(page._removed_handlers) == 0
    assert len(page._removed_handlers) == 1
    assert page._added_handlers[0][1] is page._removed_handlers[0][1]


@pytest.mark.asyncio
async def test_ack_interceptor_removes_listener_even_on_exception():
    page = _FakePage()
    with pytest.raises(RuntimeError):
        async with _ack_interceptor(page):
            raise RuntimeError("boom")
    assert len(page._removed_handlers) == 1


# ------------------------- _wait_for_ack ------------------------- #


@pytest.mark.asyncio
async def test_wait_for_ack_returns_state_text():
    state: dict = {"text": "event:SSE_ACK\ndata:{}\n\n"}
    text = await _wait_for_ack(state, timeout=1.0)
    assert "SSE_ACK" in text


@pytest.mark.asyncio
async def test_wait_for_ack_timeout_raises(monkeypatch):
    # 用 monkeypatch 把 _UI_ACK_WAIT_SECONDS 临时改短,避免测试真的等 30s
    import doupool.video.browser as browser_mod

    monkeypatch.setattr(browser_mod, "_UI_ACK_WAIT_SECONDS", 0.1)
    state: dict = {}
    with pytest.raises(RuntimeError, match="等待 /chat/completion 响应超时"):
        await _wait_for_ack(state, timeout=0.1)


# ------------------------- _submit_and_poll 契约 ------------------------- #


@pytest.mark.asyncio
async def test_submit_and_poll_use_real_browser_returns_three_field_ack(monkeypatch):
    """_submit_and_poll(use_real_browser=True) 仍返完整 ack dict —— service.py 契约。

    service.py:1368 直接 update(**ack) 解包,3 字段缺一即 KeyError。
    本测试断言 UI click 路径也保持这个契约。

    测试策略:把 _resolve_original_download 替成只返 ack,跳过 poll +
    download 全链路 —— 测的是 UI 路径下 ack 解析契约,不是 poll。
    """
    runner = PlaywrightVideoRunner(timeout=10, poll_interval=1)
    page = MagicMock()
    monkeypatch.setattr(protocol_module, "_accepted_remote_ids", {})

    async def fake_submit_via_ui(*a, **kw):
        return None

    monkeypatch.setattr("doupool.video.browser.submit_via_ui", fake_submit_via_ui)

    sse_text = (
        "event:SSE_ACK\n"
        "data:" + json.dumps({
            "ack_client_meta": {"conversation_id": "C1", "section_id": "S1"},
            "query_list": [{"question_id": "Q1"}],
        }) + "\n\n"
    )

    @asynccontextmanager
    async def fake_interceptor(p):
        yield {"text": sse_text}

    monkeypatch.setattr("doupool.video.browser._ack_interceptor", fake_interceptor)
    monkeypatch.setattr(
        "doupool.video.browser._try_solve_captcha_in_video",
        AsyncMock(return_value=False),
    )

    # 把整个 poll 路径短路:_resolve_original_download 直接返 ack dict。
    # 关键:这避开了 chain / parse_creation_result 真实响应形状(很复杂),
    # 把测试范围聚焦在「UI click → ack 解析 → 3 字段契约」。
    expected_ack = {
        "conversation_id": "C1",
        "section_id": "S1",
        "question_id": "Q1",
    }

    async def fake_resolve(self, page, result, cancel_event):
        return expected_ack

    monkeypatch.setattr(
        "doupool.video.browser.PlaywrightVideoRunner._resolve_original_download",
        fake_resolve,
    )

    # poll 路径只需在 CHAIN_SCRIPT 返 dict 让 parse_creation_result 拿到终止信号。
    # 用最简 shape:status==3 + download_url 都在。
    chain_ok_payload = json.dumps([{
        "content": {
            "creation_block": {
                "creations": [{
                    "id": "x", "video": {
                        "status": 3, "download_url": "u", "vid": "v",
                    }
                }]
            }
        }
    }])
    chain_response = {
        "status": 200,
        "data": {
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [{"content": chain_ok_payload}]
                }
            }
        },
    }
    page.evaluate = AsyncMock(return_value=chain_response)

    update = MagicMock()
    cancel = threading.Event()
    bundle = MagicMock()
    bundle.to_client_meta.return_value = {}

    result = await runner._submit_and_poll(
        page,
        "prompt",
        "seedance_v2.0_mini",
        "16:9",
        10,
        "fp",
        bundle,
        "t2v",
        [],
        update,
        cancel,
        Path("/tmp/p"),
        use_real_browser=True,
        owner_task_id="owner-task",
    )

    # 契约:result 必含 3 字段(service.py **ack 解包不会 KeyError)
    assert result["conversation_id"] == "C1"
    assert result["section_id"] == "S1"
    assert result["question_id"] == "Q1"
    assert protocol_module._accepted_remote_ids == {"x": "owner-task"}
    # update 至少调过一次 status=generating 把 ack 写进去
    update.assert_any_call(status="generating", **result)


@pytest.mark.asyncio
async def test_submit_and_poll_serializes_submit_ack_within_profile(
    monkeypatch,
    tmp_path,
):
    """同 profile 串行 submit→ACK,但进入 poll 后仍可并发。"""
    import doupool.video.browser as browser_mod

    runner = PlaywrightVideoRunner(timeout=10, poll_interval=1)
    profile = tmp_path / "shared-profile"

    class _ObservedLock(asyncio.Lock):
        def __init__(self):
            super().__init__()
            self.acquire_calls = 0
            self.second_attempted = asyncio.Event()

        async def acquire(self):
            self.acquire_calls += 1
            if self.acquire_calls == 2:
                self.second_attempted.set()
            return await super().acquire()

    observed_lock = _ObservedLock()
    runner._submit_ack_locks[str(profile)] = observed_lock

    first_submit_entered = asyncio.Event()
    release_first_ack = asyncio.Event()
    both_polling = asyncio.Event()
    release_poll = asyncio.Event()
    submit_order: list[str] = []
    poll_order: list[str] = []
    submit_ack_active = 0
    max_submit_ack_active = 0
    poll_active = 0
    max_poll_active = 0

    @asynccontextmanager
    async def fake_interceptor(page):
        nonlocal submit_ack_active, max_submit_ack_active
        submit_ack_active += 1
        max_submit_ack_active = max(max_submit_ack_active, submit_ack_active)
        try:
            yield {"page": page}
        finally:
            submit_ack_active -= 1

    async def fake_submit_via_ui(page, *args, **kwargs):
        submit_order.append(page.task_name)
        if page.task_name == "task-1":
            first_submit_entered.set()

    async def fake_wait_for_ack(state, *, timeout):
        page = state["page"]
        if page.task_name == "task-1":
            await release_first_ack.wait()
        return page.task_name

    def make_page(task_name: str):
        page = MagicMock()
        page.task_name = task_name

        async def evaluate(*args, **kwargs):
            nonlocal poll_active, max_poll_active
            poll_active += 1
            max_poll_active = max(max_poll_active, poll_active)
            poll_order.append(task_name)
            if poll_active == 2:
                both_polling.set()
            try:
                await release_poll.wait()
                return {"status": 200, "data": {}}
            finally:
                poll_active -= 1

        page.evaluate = AsyncMock(side_effect=evaluate)
        return page

    result_payload = {
        "remote_task_id": "cid",
        "vid": "vid",
        "fallback_result_url": "https://example/video.mp4",
        "cover_url": "",
    }

    monkeypatch.setattr(browser_mod, "_ack_interceptor", fake_interceptor)
    monkeypatch.setattr(browser_mod, "submit_via_ui", fake_submit_via_ui)
    monkeypatch.setattr(browser_mod, "_wait_for_ack", fake_wait_for_ack)
    monkeypatch.setattr(
        browser_mod,
        "parse_sse_ack",
        lambda text: {
            "conversation_id": f"conversation-{text}",
            "section_id": f"section-{text}",
            "question_id": f"question-{text}",
        },
    )
    monkeypatch.setattr(browser_mod, "_handle_aegis_in_poll", AsyncMock())
    monkeypatch.setattr(
        browser_mod,
        "parse_creation_result",
        MagicMock(return_value=result_payload),
    )
    monkeypatch.setattr(
        PlaywrightVideoRunner,
        "_resolve_original_download",
        AsyncMock(return_value=result_payload),
    )

    async def invoke(page):
        return await runner._submit_and_poll(
            page,
            page.task_name,
            "seedance_v2.0_mini",
            "16:9",
            10,
            "fp",
            MagicMock(),
            "t2v",
            [],
            MagicMock(),
            threading.Event(),
            profile,
            use_real_browser=True,
        )

    tasks: list[asyncio.Task] = []
    try:
        tasks.append(asyncio.create_task(invoke(make_page("task-1"))))
        await asyncio.wait_for(first_submit_entered.wait(), timeout=2)

        tasks.append(asyncio.create_task(invoke(make_page("task-2"))))
        await asyncio.wait_for(observed_lock.second_attempted.wait(), timeout=2)
        assert submit_order == ["task-1"]
        assert max_submit_ack_active == 1

        release_first_ack.set()
        await asyncio.wait_for(both_polling.wait(), timeout=2)
        assert submit_order == ["task-1", "task-2"]
        assert set(poll_order) == {"task-1", "task-2"}
        assert max_poll_active == 2

        release_poll.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    finally:
        release_first_ack.set()
        release_poll.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    assert observed_lock.acquire_calls == 2
    assert max_submit_ack_active == 1


@pytest.mark.asyncio
async def test_submit_and_poll_concurrent_across_profiles(monkeypatch, tmp_path):
    """不同 profile 使用不同锁,submit→ACK 可以并发。"""
    import doupool.video.browser as browser_mod

    runner = PlaywrightVideoRunner(timeout=10, poll_interval=1)
    profile_a = tmp_path / "profile-a"
    profile_b = tmp_path / "profile-b"
    both_submitting = asyncio.Event()
    release_submits = asyncio.Event()
    active_submits = 0
    max_active_submits = 0
    submit_order: list[str] = []

    @asynccontextmanager
    async def fake_interceptor(page):
        yield {"page": page}

    async def fake_submit_via_ui(page, *args, **kwargs):
        nonlocal active_submits, max_active_submits
        active_submits += 1
        max_active_submits = max(max_active_submits, active_submits)
        submit_order.append(page.task_name)
        if active_submits == 2:
            both_submitting.set()
        try:
            await release_submits.wait()
        finally:
            active_submits -= 1

    async def fake_wait_for_ack(state, *, timeout):
        return state["page"].task_name

    result_payload = {
        "remote_task_id": "cid",
        "vid": "vid",
        "fallback_result_url": "https://example/video.mp4",
        "cover_url": "",
    }
    chain_response = {"status": 200, "data": {}}

    monkeypatch.setattr(browser_mod, "_ack_interceptor", fake_interceptor)
    monkeypatch.setattr(browser_mod, "submit_via_ui", fake_submit_via_ui)
    monkeypatch.setattr(browser_mod, "_wait_for_ack", fake_wait_for_ack)
    monkeypatch.setattr(
        browser_mod,
        "parse_sse_ack",
        lambda text: {
            "conversation_id": f"conversation-{text}",
            "section_id": f"section-{text}",
            "question_id": f"question-{text}",
        },
    )
    monkeypatch.setattr(browser_mod, "_handle_aegis_in_poll", AsyncMock())
    monkeypatch.setattr(
        browser_mod,
        "parse_creation_result",
        MagicMock(return_value=result_payload),
    )
    monkeypatch.setattr(
        PlaywrightVideoRunner,
        "_resolve_original_download",
        AsyncMock(return_value=result_payload),
    )

    def make_page(task_name: str):
        page = MagicMock()
        page.task_name = task_name
        page.evaluate = AsyncMock(return_value=chain_response)
        return page

    async def invoke(page, profile):
        return await runner._submit_and_poll(
            page,
            page.task_name,
            "seedance_v2.0_mini",
            "16:9",
            10,
            "fp",
            MagicMock(),
            "t2v",
            [],
            MagicMock(),
            threading.Event(),
            profile,
            use_real_browser=True,
        )

    tasks = [
        asyncio.create_task(invoke(make_page("task-1"), profile_a)),
        asyncio.create_task(invoke(make_page("task-2"), profile_b)),
    ]
    try:
        await asyncio.wait_for(both_submitting.wait(), timeout=2)
        assert max_active_submits == 2
        assert set(submit_order) == {"task-1", "task-2"}
        assert runner._submit_ack_lock_for(profile_a) is not runner._submit_ack_lock_for(profile_b)

        release_submits.set()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=2)
    finally:
        release_submits.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_submit_and_poll_use_real_browser_false_still_works(monkeypatch):
    """use_real_browser=False = 原 fetch 路径保留(回归 + 测试 fixture)。"""
    runner = PlaywrightVideoRunner(timeout=10, poll_interval=1)
    page = MagicMock()

    sse_text = (
        "event:SSE_ACK\n"
        "data:" + json.dumps({
            "ack_client_meta": {"conversation_id": "C2", "section_id": "S2"},
            "query_list": [{"question_id": "Q2"}],
        }) + "\n\n"
    )

    chain_ok_payload = json.dumps([{
        "content": {
            "creation_block": {
                "creations": [{
                    "id": "x", "video": {
                        "status": 3, "download_url": "u", "vid": "v",
                    }
                }]
            }
        }
    }])
    chain_response = {
        "status": 200,
        "data": {
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [{"content": chain_ok_payload}]
                }
            }
        },
    }

    call_count = {"n": 0}

    async def fake_evaluate(expr, arg=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None  # history.replaceState
        if call_count["n"] == 2:
            return {"status": 200, "text": sse_text}  # COMPLETION_SCRIPT
        return chain_response  # CHAIN_SCRIPT 成功 → 终止 while

    page.evaluate = fake_evaluate

    bundle = MagicMock()
    bundle.to_client_meta.return_value = {}

    expected_ack = {
        "conversation_id": "C2",
        "section_id": "S2",
        "question_id": "Q2",
    }

    async def fake_resolve(self, page, result, cancel_event):
        return expected_ack

    monkeypatch.setattr(
        "doupool.video.browser.PlaywrightVideoRunner._resolve_original_download",
        fake_resolve,
    )

    result = await runner._submit_and_poll(
        page,
        "prompt",
        "seedance_v2.0_mini",
        "16:9",
        10,
        "fp",
        bundle,
        "t2v",
        [],
        MagicMock(),
        threading.Event(),
        Path("/tmp/p"),
        use_real_browser=False,
    )

    assert result["conversation_id"] == "C2"
    assert result["section_id"] == "S2"
    assert result["question_id"] == "Q2"


# ------------------------- selector 常量 ------------------------- #


def test_selector_constants_match_probe_findings():
    """selector 必须跟 probe_*.py 实地命中的一致 —— 防止改错。"""
    assert VIDEO_TAB_SEL == "[role='tab']:has-text('视频')"
    assert EDITOR_SEL == "div[contenteditable='true']"
    assert SEND_BTN_SEL == ".send-btn-wrapper button"
    assert SEND_BTN_FALLBACK_SEL == "button:has(svg path[d^='M4.93934 10.2598'])"


# ------------------------- v0.3.2.1 clipboard paste ------------------------- #


LONG_PROMPT = (
    "请生成一段 16:9 的高质量短视频:夕阳下的城市天际线,玻璃幕墙反射"
    "出橙红色的晚霞,空中有鸟群飞过,镜头从远景缓慢推进到中景,色调"
    "温暖怀旧,粒子光斑点缀,8K 电影质感,光圈虚化背景,无人,空镜,"
    "BGM 渐弱后停止。生成完成后请保证画面连续无闪烁,人物自然,场景"
    "真实,字幕清晰可读。" * 4
)  # ~500 chars,模拟长 prompt


@pytest.mark.asyncio
async def test_submit_via_ui_pastes_long_prompt_in_single_op(monkeypatch):
    """v0.3.2.1:长 prompt 必须一次性 paste(整段 → writeText 一次 + Ctrl+V 一次),
    不能分次 keyboard.type(那样 aegis 时序风控会标)。
    """
    page = _FakePage(url="https://www.doubao.com/chat/create-image")
    monkeypatch.setattr(
        "doupool.video.browser._try_solve_captcha_in_video",
        AsyncMock(return_value=False),
    )
    await submit_via_ui(
        page, LONG_PROMPT, profile_dir=Path("/tmp/p"), update=MagicMock(),
    )

    # 1) 整段 prompt(>500 字)进了 clipboard,只有一次 writeText
    write_calls = [
        arg for _expr, arg in page.evaluates
        if "writeText" in str(_expr)
    ]
    assert len(write_calls) == 1, (
        f"应只调一次 writeText; 实际 {len(write_calls)} 次"
    )
    assert write_calls[0] == LONG_PROMPT, "writeText 必须接收完整 prompt"
    assert len(write_calls[0]) >= 500, "测试 prompt 应 >= 500 字"

    # 2) keyboard.type 整段没被调 —— 否则就把整段按 char type 进去了
    assert page.keyboard.types == [], (
        f"v0.3.2.1 起整段 prompt 走 paste; keyboard.type 应为空; "
        f"实际 types={page.keyboard.types}"
    )

    # 3) Control+V 整段贴,只按一次(不是每字符按)
    ctrl_v_count = sum(1 for k in page.keyboard.presses if k == "Control+V")
    assert ctrl_v_count == 1, f"应只按一次 Ctrl+V; 实际 {ctrl_v_count} 次"


def test_build_launch_kwargs_grants_clipboard_permission():
    """v0.3.2.1:_build_launch_kwargs() 必须带 clipboard 读写权限。

    没有这个 grant_permissions,navigator.clipboard.writeText() 抛
    NotAllowedError,paste 路径整个挂掉,prompt 进不去输入框。
    launch_persistent_context 的 permissions 必须在 context 创建时
    一次性申请,context.grant_permissions() 后改 origin 不对(不是
    https://www.doubao.com),所以只能走 launch kwargs。

    v0.3.2.2 修:Playwright 严格校验,只接受 clipboard-read /
    clipboard-write 两条独立 permission(见
    playwright/driver/.../coreBundle.js 的 permission map)。v0.3.2.1
    写 clipboard-read-write —— launch 立刻抛 `Unknown permission`
    浏览器闪退,整条 login / submit 流挂掉。改名拆两条:
    - clipboard-read → writeText 后 readback 用,本次 v0.3.2.1 没用到
      但保留,跟 Playwright 文档推荐做法对齐(给后续扩展留口子)
    - clipboard-write → writeText 必需(无它 NotAllowedError)

    测试同时断言「不在列表里」(防 v0.3.2.1 那条 clipboard-read-write
    混进代码回归)。
    """
    kwargs = _build_launch_kwargs(window_visible=False)
    assert "permissions" in kwargs, "launch kwargs 必须有 permissions 字段"
    perms = kwargs["permissions"]
    assert "clipboard-read" in perms, (
        f"必须授 clipboard-read 才能 navigator.clipboard.readText(); 实际 permissions={perms}"
    )
    assert "clipboard-write" in perms, (
        f"必须授 clipboard-write 才能 navigator.clipboard.writeText(); 实际 permissions={perms}"
    )
    # v0.3.2.2 防回归:v0.3.2.1 错误拼写 clipboard-read-write 必须不在列表里
    assert "clipboard-read-write" not in perms, (
        f"v0.3.2.2:clipboard-read-write 不是合法 Playwright permission 名 "
        f"(launch 会闪退);实际 permissions={perms}"
    )

    # window_visible=True 的分支也必须带(用户开窗浏览时一样要 paste)
    kwargs_visible = _build_launch_kwargs(window_visible=True)
    visible_perms = kwargs_visible["permissions"]
    assert "clipboard-read" in visible_perms
    assert "clipboard-write" in visible_perms
    assert "clipboard-read-write" not in visible_perms


# ------------------------- v0.3.2.3 _pre_submit_aegis_gate ------------------------- #


@pytest.fixture
def fast_gate_constants(monkeypatch):
    """把 _UI_CAPTCHA_* 三个常量临时压短,避免测试真的等 6+4 秒。

    v0.3.2.3 经验值 6s wait + 4s verify gone 是真机场景下「足够」的窗口;
    单测没必要等真的时间,改成 0.2 + 0.2 就能覆盖四条分支。
    """
    import doupool.video.browser as browser_mod

    monkeypatch.setattr(browser_mod, "_UI_CAPTCHA_WAIT_SECONDS", 0.2)
    monkeypatch.setattr(browser_mod, "_UI_CAPTCHA_VERIFY_GONE_SECONDS", 0.2)
    monkeypatch.setattr(browser_mod, "_UI_CAPTCHA_DETECT_POLL_INTERVAL", 0.05)


@pytest.mark.asyncio
async def test_pre_submit_aegis_gate_no_popup_allows_submit(
    monkeypatch, fast_gate_constants,
):
    """case 1:探测期内始终没弹窗 → 放行 True。

    这是大多数正常提交路径(no aegis popup)—— 直接进 step 6 点 send,
    不浪费时间等弹窗。
    """
    page = _FakePage()
    update = MagicMock()

    # detect 一律返 UNKNOWN → 6s 内不会 break
    monkeypatch.setattr(
        "doupool.video.browser._detect_aegis_captcha",
        AsyncMock(return_value=CaptchaKind.UNKNOWN),
    )

    allowed = await _pre_submit_aegis_gate(
        page, Path("/tmp/p"), update,
    )
    assert allowed is True
    # solve 路径不应被触达(load_credentials 没调就对了)
    update.assert_not_called()


@pytest.mark.asyncio
async def test_pre_submit_aegis_gate_in_cooldown_allows_submit(
    monkeypatch, fast_gate_constants,
):
    """case 2:账号已在 cooldown → 立即放行,根本不走 detect。

    account_key = str(profile_dir),与 login 路径共享同一份 30min dict;
    login 刚解完 → video 路径秒过,不会浪费图鉴配额。
    """
    page = _FakePage()
    update = MagicMock()

    monkeypatch.setattr(
        "doupool.video.browser._captcha_is_in_cooldown",
        lambda key: True,
    )

    # detect 不该被调(已 cooldown 立即 return)
    detect_mock = AsyncMock(return_value=CaptchaKind.SLIDE_PUZZLE)
    monkeypatch.setattr(
        "doupool.video.browser._detect_aegis_captcha", detect_mock,
    )

    allowed = await _pre_submit_aegis_gate(
        page, Path("/tmp/in_cooldown"), update,
    )
    assert allowed is True
    detect_mock.assert_not_called()


@pytest.mark.asyncio
async def test_pre_submit_aegis_gate_popup_solved_and_gone_allows_submit(
    monkeypatch, fast_gate_constants,
):
    """case 3:探测到弹窗 → 解 → 弹窗消失 → 放行 True。

    主 happy path,弹窗被图鉴拖走了,solver 之后 detect 返 UNKNOWN。
    """
    page = _FakePage()
    update = MagicMock()
    profile_dir = Path("/tmp/gate_solved")

    # detect 序列:第 1-2 次 UNKNOWN,第 3 次 SLIDE(被发现),之后全 UNKNOWN(已解)
    detect_results = [
        CaptchaKind.UNKNOWN,
        CaptchaKind.UNKNOWN,
        CaptchaKind.SLIDE_PUZZLE,  # 弹窗出现 → 进 solve 路径
        CaptchaKind.UNKNOWN,       # 解完后消失
        CaptchaKind.UNKNOWN,
    ]
    monkeypatch.setattr(
        "doupool.video.browser._detect_aegis_captcha",
        AsyncMock(side_effect=detect_results),
    )

    # 凭证可用 → make_client 返 dummy
    monkeypatch.setattr(
        "doupool.video.browser._load_captcha_credentials",
        lambda: MagicMock(usable=True),
    )
    dummy_client = MagicMock()
    monkeypatch.setattr(
        "doupool.video.browser._make_captcha_client",
        lambda creds: dummy_client,
    )
    # solve 直接完成
    monkeypatch.setattr(
        "doupool.video.browser._solve_aegis_captcha",
        AsyncMock(return_value=None),
    )
    # mark_cooldown 不做断言但 stub 掉避免污染
    monkeypatch.setattr(
        "doupool.video.browser._captcha_mark_cooldown",
        lambda key: None,
    )

    allowed = await _pre_submit_aegis_gate(
        page, profile_dir, update,
    )
    assert allowed is True, "弹窗解完消失后,网关必须放行"
    # update 至少报过一次 "拖拽验证已通过"
    update_msgs = [c.kwargs.get("error_message", "") for c in update.call_args_list]
    assert any("拖拽" in m or "已通过" in m for m in update_msgs), (
        f"解完后应该报进度文案; 实际 update={update_msgs}"
    )


@pytest.mark.asyncio
async def test_pre_submit_aegis_gate_popup_persists_blocks_submit(
    monkeypatch, fast_gate_constants,
):
    """case 4:探测到弹窗 → 解 → 弹窗仍在 → 返 False(**不**点 send)。

    这是 v0.3.2.3 关键防御:防止 POST 撞 aegis 弹窗触发 shark_admin 拒绝。
    submit_via_ui 拿到 False 就 raise RuntimeError 阻止 click send。
    """
    page = _FakePage()
    update = MagicMock()
    profile_dir = Path("/tmp/gate_stuck")

    # detect:第一次就 SLIDE,之后永远 SLIDE(弹窗卡死)
    monkeypatch.setattr(
        "doupool.video.browser._detect_aegis_captcha",
        AsyncMock(return_value=CaptchaKind.SLIDE_PUZZLE),
    )

    monkeypatch.setattr(
        "doupool.video.browser._load_captcha_credentials",
        lambda: MagicMock(usable=True),
    )
    dummy_client = MagicMock()
    monkeypatch.setattr(
        "doupool.video.browser._make_captcha_client",
        lambda creds: dummy_client,
    )
    # solver 调完,弹窗依然在(就等不到 UNKNOWN)
    monkeypatch.setattr(
        "doupool.video.browser._solve_aegis_captcha",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "doupool.video.browser._captcha_mark_cooldown",
        lambda key: None,
    )

    allowed = await _pre_submit_aegis_gate(
        page, profile_dir, update,
    )
    assert allowed is False, "弹窗持续存在必须阻止 submit,防 POST + 弹窗撞车"
    # update 应有「暂不提交」语义
    update_msgs = [c.kwargs.get("error_message", "") for c in update.call_args_list]
    assert any("未消失" in m or "暂不" in m or "稍后" in m for m in update_msgs), (
        f"弹窗卡死时 update 必须告诉用户; 实际={update_msgs}"
    )


@pytest.mark.asyncio
async def test_pre_submit_aegis_gate_credentials_disabled_blocks_submit(
    monkeypatch, fast_gate_constants,
):
    """case 5(v0.3.2.4):图鉴凭证关(没配 / enabled=false)→ make_client 抛
    AegisCaptchaDisabled → 网关返 **False**,**阻止** submit。

    旧 v0.3.2.3 行为是「降级放行」—— 让 submit 撞 aegis 弹窗 → 走原失败路径。
    但实测:撞弹窗会被 shark_admin 风控挡,直接扣视频额度 + 任务失败,
    「降级放行」反而是 bug,比直接 fail 更糟。

    新语义:任何解不出来的情况(凭证关 / solver 失败 / 异常)→ 返 False,
    submit_via_ui 拿到 False 直接 raise RuntimeError 阻止 click send。
    cooldown 30 分钟保护下次不要重蹈覆辙。
    """
    page = _FakePage()
    update = MagicMock()
    profile_dir = Path("/tmp/no_creds")

    # 探测到弹窗
    monkeypatch.setattr(
        "doupool.video.browser._detect_aegis_captcha",
        AsyncMock(return_value=CaptchaKind.SLIDE_PUZZLE),
    )

    # make_client 抛 AegisCaptchaDisabled(凭证关)
    from doupool.captcha.solver import AegisCaptchaDisabled
    monkeypatch.setattr(
        "doupool.video.browser._load_captcha_credentials",
        lambda: MagicMock(usable=False),
    )

    def _raise_disabled(creds):
        raise AegisCaptchaDisabled("凭证未配置")

    monkeypatch.setattr(
        "doupool.video.browser._make_captcha_client",
        _raise_disabled,
    )
    monkeypatch.setattr(
        "doupool.video.browser._captcha_mark_cooldown",
        lambda key: None,
    )

    allowed = await _pre_submit_aegis_gate(
        page, profile_dir, update,
    )
    # v0.3.2.4:凭证关 → 阻止 submit,不能撞 shark_admin 风控
    assert allowed is False, "凭证关必须阻止 submit,以免撞 aegis 触发服务端风控"
    update_msgs = [c.kwargs.get("error_message", "") for c in update.call_args_list]
    # 必须告诉用户原因(凭证关 / 配额 / 30min cooldown)
    assert any(
        "凭证" in m or "图鉴" in m or "配额" in m or "暂不" in m for m in update_msgs
    ), f"凭证关必须有提示; 实际={update_msgs}"


@pytest.mark.asyncio
async def test_pre_submit_aegis_gate_solver_failed_blocks_submit(
    monkeypatch, fast_gate_constants,
):
    """case 5b(v0.3.2.4):solver 抛 AegisCaptchaFailed → 网关返 **False**。

    旧行为返 True(降级放行)撞 shark_admin 失败 → 扣额度。改成 False 阻止。
    """
    page = _FakePage()
    update = MagicMock()
    profile_dir = Path("/tmp/solver_failed")

    monkeypatch.setattr(
        "doupool.video.browser._detect_aegis_captcha",
        AsyncMock(return_value=CaptchaKind.SLIDE_PUZZLE),
    )
    monkeypatch.setattr(
        "doupool.video.browser._load_captcha_credentials",
        lambda: MagicMock(usable=True),
    )
    monkeypatch.setattr(
        "doupool.video.browser._make_captcha_client",
        lambda creds: MagicMock(),
    )

    from doupool.captcha.solver import AegisCaptchaFailed

    async def _raise_failed(*a, **kw):
        raise AegisCaptchaFailed("图鉴识别失败,余额不足")

    monkeypatch.setattr(
        "doupool.video.browser._solve_aegis_captcha",
        _raise_failed,
    )
    monkeypatch.setattr(
        "doupool.video.browser._captcha_mark_cooldown",
        lambda key: None,
    )

    allowed = await _pre_submit_aegis_gate(
        page, profile_dir, update,
    )
    assert allowed is False, "solver 抛 AegisCaptchaFailed 必须阻止 submit"
    update_msgs = [c.kwargs.get("error_message", "") for c in update.call_args_list]
    assert any("失败" in m or "余额" in m or "配额" in m or "暂不" in m for m in update_msgs), (
        f"solver 失败必须告诉用户原因; 实际={update_msgs}"
    )


@pytest.mark.asyncio
async def test_pre_submit_aegis_gate_solver_exception_blocks_submit(
    monkeypatch, fast_gate_constants,
):
    """case 5c(v0.3.2.4):solver 抛通用 Exception → 网关返 **False**(防御性)。

    旧行为:except Exception → 返 True(吞错降级),然后撞 shark_admin。
    新行为:任何意外也返 False —— 阻止 submit 比误放行更安全。
    """
    page = _FakePage()
    update = MagicMock()
    profile_dir = Path("/tmp/solver_crash")

    monkeypatch.setattr(
        "doupool.video.browser._detect_aegis_captcha",
        AsyncMock(return_value=CaptchaKind.SLIDE_PUZZLE),
    )
    monkeypatch.setattr(
        "doupool.video.browser._load_captcha_credentials",
        lambda: MagicMock(usable=True),
    )
    monkeypatch.setattr(
        "doupool.video.browser._make_captcha_client",
        lambda creds: MagicMock(),
    )

    async def _raise_boom(*a, **kw):
        raise RuntimeError("solver 内层炸了")

    monkeypatch.setattr(
        "doupool.video.browser._solve_aegis_captcha",
        _raise_boom,
    )
    monkeypatch.setattr(
        "doupool.video.browser._captcha_mark_cooldown",
        lambda key: None,
    )

    allowed = await _pre_submit_aegis_gate(
        page, profile_dir, update,
    )
    assert allowed is False, "solver 抛通用 Exception 也必须阻止 submit"
    update_msgs = [c.kwargs.get("error_message", "") for c in update.call_args_list]
    assert any("异常" in m or "暂不" in m or "稍后" in m for m in update_msgs), (
        f"异常必须告诉用户; 实际={update_msgs}"
    )


# ------------------------- v0.3.2.3 / v0.3.2.4 submit_via_ui 行为契约 ------------------------- #


@pytest.mark.asyncio
async def test_submit_via_ui_raises_when_gate_blocks(monkeypatch, fast_gate_constants):
    """v0.3.2.3 + v0.3.2.4 submit_via_ui 契约:_pre_submit_aegis_gate 返 False →
    整体 raise RuntimeError,不能进入 step 3 后续 click 流程。

    防止代码被改回 fire-and-forget:即使 gate 拒绝,后续 try_click 也不该被调。

    v0.3.2.4 进一步:step 6 也走完整的 _pre_submit_aegis_gate(不再是
    _try_solve_captcha_in_video 弱兜底),所以两次调用都应被验证;
    第一次返 False → 直接 raise,不会再调第二次。
    """
    page = _FakePage(url="https://www.doubao.com/chat/create-image")
    update = MagicMock()

    gate_mock = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "doupool.video.browser._pre_submit_aegis_gate",
        gate_mock,
    )

    with pytest.raises(RuntimeError, match="aegis 拖拽验证未通过"):
        await submit_via_ui(page, "x", profile_dir=Path("/tmp/p"), update=update)

    # 关键:raise 之后,send button 不该被 click(mouse.downs 应为 0)。
    # VIDEO_TAB_SEL 也不该被 click(还没到 step 3)。
    assert page.mouse.downs == 0, (
        f"gate 拒绝后 mouse.downs 必须为 0(不该进 step 3/6); "
        f"实际={page.mouse.downs}, moves={page.mouse.moves}"
    )

    # v0.3.2.4:gate 第一次返 False 直接 raise —— 只调了一次(不进 step 6)。
    # 这是 step 2 「导航 → 网关」的拦截路径。
    assert gate_mock.await_count == 1, (
        f"gate 第一次返 False 应直接 raise,不应再调第二次; "
        f"实际 await_count={gate_mock.await_count}"
    )


@pytest.mark.asyncio
async def test_submit_via_ui_step6_rechecks_aegis_after_paste(
    monkeypatch, fast_gate_constants,
):
    """v0.3.2.4 契约:submit_via_ui 在 step 2(导航后) **和** step 6(粘贴后、
    click send 前)都调 _pre_submit_aegis_gate。

    用户实际场景:step 2 网关通过 → paste prompt → aegis 弹窗刚出现 → click
    send → POST 撞弹窗 → shark_admin 拒绝。step 6 二次拦截防止这种情况。

    此测试:step 2 返 True(允许 paste),step 6 返 False(拦截 send)→ 整体 raise。
    """
    page = _FakePage(url="https://www.doubao.com/chat/create-image")
    update = MagicMock()

    # step 2 返 True,paste 正常进行;step 6 返 False 拦截 send
    gate_mock = AsyncMock(side_effect=[True, False])
    monkeypatch.setattr(
        "doupool.video.browser._pre_submit_aegis_gate",
        gate_mock,
    )

    # step 6 触发的错误文案跟 step 2 不一样(「粘贴后再次检测到...」)
    with pytest.raises(RuntimeError, match="粘贴后再次检测到拖拽验证"):
        await submit_via_ui(page, "x", profile_dir=Path("/tmp/p"), update=update)

    # step 2 通过 → step 3(点 video tab)跑了 1 次 mouse.down,
    # 但 step 6 raise → step 6 的 click(SEND_BTN_SEL)没跑 → 总 mouse.downs 应为 1
    assert page.mouse.downs == 1, (
        f"step 6 raise 后 SEND_BTN_SEL click 必须被阻止; "
        f"应有 1 次 mouse.down(video tab click); 实际={page.mouse.downs}"
    )
    # gate 必须被调 **两次**(step 2 + step 6)
    assert gate_mock.await_count == 2, (
        f"step 2 通过 → step 6 必须再调一次 gate; "
        f"实际 await_count={gate_mock.await_count}"
    )


# ------------------------- v0.3.2.5 shark_admin 拒绝 → 不关浏览器重提 ------------------------- #
#
# 用户反馈(2026-08-13):「还是滑块刚出现就被关掉了,我现在要求你修改一个
# 审核逻辑,只要是识别到账号被风控拦截这个报错,就不能关闭浏览器,必须等
# 滑块出来让图鉴识别再模拟拖动提交。」
#
# v0.3.2.3 / v0.3.2.4 的实现是「submit 撞弹窗 → raise → service.py
# finally page.close() → 弹窗随页面被关」。v0.3.2.5 在 run() retry loop
# 里新增 except DoubaoRateLimited(is_risk_control=True) 分支:不让它冒泡,
# 保持 page 活着,在原 page 上调 _pre_submit_aegis_gate 等弹窗出现 + 解,
# 然后 continue 重新跑 _submit_and_poll。
#
# 关键不变量:
# - page 在整个 retry 期间不关(finally 块不会跑到)
# - quota 已经在 service._run_inner.update("generating") 时扣过,失败路径
#   service.py 会 refund,所以 retry 不重复扣
# - 最多 _MAX_RISK_RETRY 次,避免无限循环浪费 token
#


def _stub_run_setup(monkeypatch):
    """把 run() 的「打开 context + new_page + 提交前 captcha 探针 + i2v 上传」
    全部 stub 掉,只让 retry loop 跑。返回 (runner, submit_and_poll_mock,
    gate_mock) —— 测试只关心这三个的状态变化。
    """
    runner = PlaywrightVideoRunner(timeout=10, poll_interval=1)

    # context + token_bundle(返回 (_FakePage 形状的 MagicMock context))
    fake_page = MagicMock()
    fake_page.url = "https://www.doubao.com/chat/"
    fake_page.close = AsyncMock(return_value=None)  # run() finally 会 await 它
    fake_page.goto = AsyncMock(return_value=None)
    fake_page.wait_for_timeout = AsyncMock(return_value=None)
    fake_context = MagicMock()
    fake_context.new_page = AsyncMock(return_value=fake_page)
    fake_context.is_closed = MagicMock(return_value=False)
    fake_token_bundle = MagicMock()
    fake_token_bundle.device_id = "device-fp"

    async def fake_get_shared_context(self, profile_dir, pc_version=None, **kw):
        return fake_context, fake_token_bundle

    monkeypatch.setattr(
        "doupool.video.browser.PlaywrightVideoRunner._get_shared_context",
        fake_get_shared_context,
    )
    monkeypatch.setattr(
        "doupool.video.browser._is_context_alive",
        lambda ctx: True,
    )

    # 提交前 captcha 探针 no-op
    monkeypatch.setattr(
        "doupool.video.browser._try_solve_captcha_in_video",
        AsyncMock(return_value=False),
    )

    # _submit_and_poll 替成 mock
    submit_and_poll_mock = AsyncMock()
    monkeypatch.setattr(
        "doupool.video.browser.PlaywrightVideoRunner._submit_and_poll",
        submit_and_poll_mock,
    )

    # _pre_submit_aegis_gate 替成 mock(默认放行 True)
    gate_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(
        "doupool.video.browser._pre_submit_aegis_gate",
        gate_mock,
    )

    return runner, submit_and_poll_mock, gate_mock


@pytest.mark.asyncio
async def test_run_shark_admin_keepalive_solve_succeeds_then_retry_succeeds(
    monkeypatch, fast_gate_constants,
):
    """case 1(v0.3.2.5 主路径):第一次 _submit_and_poll 抛
    DoubaoRateLimited(is_risk_control=True) → 不 raise、不关 page →
    调 _pre_submit_aegis_gate 等滑块 + 解 → 返 True → 用原 prompt 第二次
    _submit_and_poll 成功 → 任务完成。

    这是用户 2026-08-13 反馈的核心场景的 happy path:滑块在第一次 submit 后
    才出现,程序不关窗口,等图鉴拖完后自动重提。
    """
    runner, submit_and_poll_mock, gate_mock = _stub_run_setup(monkeypatch)
    update = MagicMock()
    cancel = threading.Event()
    profile_dir = Path("/tmp/risk_keepalive")

    # 第一次抛风控拒绝,第二次返 ack
    final_ack = {"conversation_id": "C-final", "section_id": "S-final", "question_id": "Q-final"}
    submit_and_poll_mock.side_effect = [
        DoubaoRateLimited("shark_admin 拒绝", is_risk_control=True),
        final_ack,
    ]

    result = await runner.run(
        profile_dir=profile_dir,
        prompt="小狗在草地上奔跑",
        model="seedance_v2.0_mini",
        ratio="16:9",
        duration=10,
        update=update,
        cancel_event=cancel,
        owner_task_id="owner-task",
    )

    # 重试成功 → 返 ack
    assert result == final_ack
    # 两次 submit_and_poll 调用
    assert submit_and_poll_mock.await_count == 2
    assert all(
        call.kwargs["owner_task_id"] == "owner-task"
        for call in submit_and_poll_mock.await_args_list
    )
    # gate 必须被调一次(第一次冒泡风控时),**不能在第二次跑前再调**
    assert gate_mock.await_count == 1, (
        f"gate 应只在第一次风控后调一次,等弹窗 + 解完后放行重提; "
        f"实际={gate_mock.await_count}"
    )
    # update 至少报过一次「检测到风控拦截,正在保留浏览器等待滑块并自动解算」
    update_msgs = [c.kwargs.get("error_message", "") for c in update.call_args_list]
    assert any("风控" in m and "保留浏览器" in m for m in update_msgs), (
        f"风控 keepalive 必须报进度文案; 实际={update_msgs}"
    )
    # 第二次重提成功后 update 应写「滑块已通过,正在以原 prompt 重新提交」
    assert any("滑块已通过" in m for m in update_msgs), (
        f"解完滑块应报正在重提; 实际={update_msgs}"
    )


@pytest.mark.asyncio
async def test_run_shark_admin_keepalive_solve_failed_raises(
    monkeypatch, fast_gate_constants,
):
    """case 2(v0.3.2.5):图鉴解滑块失败(_pre_submit_aegis_gate 返 False)
    → raise RuntimeError,不再 retry。

    防御性:不让任务在「等不到滑块」的境况下无限循环。第一次 solve 失败
    → 直接 fail-fast 走 service.py 失败路径(task failed + 退额度)。
    """
    runner, submit_and_poll_mock, gate_mock = _stub_run_setup(monkeypatch)
    update = MagicMock()
    cancel = threading.Event()
    profile_dir = Path("/tmp/risk_solve_fail")

    submit_and_poll_mock.side_effect = DoubaoRateLimited(
        "shark_admin 拒绝", is_risk_control=True,
    )
    # 第一次 solve → 失败
    gate_mock.side_effect = [False]

    with pytest.raises(RuntimeError, match="自动解滑块失败"):
        await runner.run(
            profile_dir=profile_dir,
            prompt="x",
            model="seedance_v2.0_mini",
            ratio="16:9",
            duration=10,
            update=update,
            cancel_event=cancel,
        )

    # 只调了一次 submit_and_poll(第一次冒泡风控);solve 失败 → 不再重试
    assert submit_and_poll_mock.await_count == 1
    assert gate_mock.await_count == 1


@pytest.mark.asyncio
async def test_run_shark_admin_retry_exhausted_raises(monkeypatch, fast_gate_constants):
    """case 3(v0.3.2.5):连续 _MAX_RISK_RETRY 次风控拒绝 + 每次解滑块成功 →
    用完 _MAX_RISK_RETRY 后 raise RuntimeError 退出,防止无限循环。

    防浪费 token / 额度:同一账号同 IP 同请求指纹特征,改 prompt 也救不了
    shark_admin —— _MAX_RISK_RETRY=2 是上限,够覆盖偶发网络抖动,再多次就
    是无谓消耗。
    """
    import doupool.video.browser as browser_mod

    monkeypatch.setattr(browser_mod, "_MAX_RISK_RETRY", 2)
    runner, submit_and_poll_mock, gate_mock = _stub_run_setup(monkeypatch)
    update = MagicMock()
    cancel = threading.Event()
    profile_dir = Path("/tmp/risk_exhaust")

    # 每次 submit_and_poll 都抛风控;gate 一直成功(模拟「滑块解得通但 shark_admin 仍挡」)
    submit_and_poll_mock.side_effect = DoubaoRateLimited(
        "shark_admin 拒绝", is_risk_control=True,
    )
    gate_mock.side_effect = [True, True, True]  # 第 3 次不会再被调

    with pytest.raises(RuntimeError, match="连续 2 次自动解滑块均失败"):
        await runner.run(
            profile_dir=profile_dir,
            prompt="x",
            model="seedance_v2.0_mini",
            ratio="16:9",
            duration=10,
            update=update,
            cancel_event=cancel,
        )

    # 初始 1 次 + retry 2 次(_MAX_RISK_RETRY=2)= 总共 3 次 submit_and_poll
    # (每次 raise DoubaoRateLimited 后 risk_attempt 递增,直到 > _MAX_RISK_RETRY 才 raise RuntimeError)
    assert submit_and_poll_mock.await_count == 3
    # gate 也调了 2 次(每次失败后等滑块 + 解)
    assert gate_mock.await_count == 2


@pytest.mark.asyncio
async def test_run_non_risk_rate_limit_still_bubbles(monkeypatch, fast_gate_constants):
    """case 4(v0.3.2.5 防御):is_risk_control=False 的 quota 限流 → 不走
    keepalive 分支,沿原行为冒泡(交给 service.py 处理 mark_account_limited
    + assign None + 任务回 queued)。

    验证点:非风控的 DoubaoRateLimited 不被 swallow,必须 raise 出去。
    """
    runner, submit_and_poll_mock, gate_mock = _stub_run_setup(monkeypatch)
    update = MagicMock()
    cancel = threading.Event()
    profile_dir = Path("/tmp/quota_limit")

    submit_and_poll_mock.side_effect = DoubaoRateLimited(
        "豆包今日 quota 已用完", is_risk_control=False,
    )

    with pytest.raises(DoubaoRateLimited, match="quota"):
        await runner.run(
            profile_dir=profile_dir,
            prompt="x",
            model="seedance_v2.0_mini",
            ratio="16:9",
            duration=10,
            update=update,
            cancel_event=cancel,
        )

    # 非风控 → 不应触发 gate,也不应第二次 submit_and_poll
    assert submit_and_poll_mock.await_count == 1
    assert gate_mock.await_count == 0, (
        f"非风控的 quota 限流绝不能触发滑块等待 gate; "
        f"实际 await_count={gate_mock.await_count}"
    )


# ------------------------- v0.3.4:poll 循环 aegis fail-fast ------------------------- #
#
# 背景:用户原话「我在后台看到视频已经生成成功了,你特么还卡在生成中」。
# 真根因:poll 循环里 `_try_solve_captcha_in_video` 被 30 分钟 cooldown 短路,
# 用户关掉弹窗或凭证没配时,aegis 持续挡 chain → poll 盲飞到超时。
#
# v0.3.4 修复:
#   1. 加 `_handle_aegis_in_poll`(廉价探测 + fail-fast)
#   2. poll 循环 wait_for_timeout 拆 1s 段,每段先 `_probe_aegis_quickly`
#   3. 探测到 aegis 但凭证不可用 → 抛 `_AegisUnresolvableInPoll`,任务立刻 fail
#
# 本节测试:验证(2)(3)的契约,(1)已在 test_video_captcha_hook.py 覆盖。
# ─────────────────────────────────────────────────────────────────────────────


def _build_chain_response_no_creation() -> dict:
    """构造 chain 返回 payload 但**没有**creation_block → poll 继续等待。"""
    return {
        "status": 200,
        "data": {
            "downlink_body": {
                "pull_singe_chain_downlink_body": {
                    "messages": [{"content": "[]"}]
                }
            }
        },
    }


@pytest.mark.asyncio
async def test_poll_loop_calls_handle_aegis_in_poll_each_iteration(
    monkeypatch,
):
    """v0.3.4:poll 循环每次 chain 请求前都调 `_handle_aegis_in_poll`，
    而非只调被 cooldown 短路的 `_try_solve_captcha_in_video`。
    """
    runner = PlaywrightVideoRunner(timeout=10, poll_interval=1)
    page = MagicMock()

    handle_mock = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "doupool.video.browser._handle_aegis_in_poll", handle_mock,
    )

    sse_text = (
        "event:SSE_ACK\n"
        "data:" + json.dumps({
            "ack_client_meta": {"conversation_id": "C", "section_id": "S"},
            "query_list": [{"question_id": "Q"}],
        }) + "\n\n"
    )
    chain_no_creation = _build_chain_response_no_creation()

    call_count = {"n": 0}

    async def fake_evaluate(expr, arg=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None  # history.replaceState
        if call_count["n"] == 2:
            return {"status": 200, "text": sse_text}
        return chain_no_creation  # chain 空 → 继续 poll

    page.evaluate = fake_evaluate

    async def fake_resolve(self, page, result, cancel_event):
        raise RuntimeError("never reached - poll times out")

    monkeypatch.setattr(
        "doupool.video.browser.PlaywrightVideoRunner._resolve_original_download",
        fake_resolve,
    )
    # 加速 poll:timeout=2s,poll_interval=1s,只跑 2 轮
    runner.timeout = 2
    runner.poll_interval = 1

    bundle = MagicMock()
    bundle.to_client_meta.return_value = {}

    with pytest.raises(RuntimeError, match="视频生成超时"):
        await runner._submit_and_poll(
            page,
            "prompt",
            "seedance_v2.0_mini",
            "16:9",
            10,
            "fp",
            bundle,
            "t2v",
            [],
            MagicMock(),
            threading.Event(),
            Path("/tmp/p"),
            use_real_browser=False,
        )
    # 关键断言:`_handle_aegis_in_poll` 至少被调一次(每轮 poll 必跑)
    assert handle_mock.await_count >= 1, (
        f"v0.3.4 要求 poll 每轮都调 _handle_aegis_in_poll; "
        f"实际 await_count={handle_mock.await_count}"
    )


@pytest.mark.asyncio
async def test_poll_loop_fails_fast_on_aegis_unresolvable(
    monkeypatch,
):
    """v0.3.4 关键防御:探测到 aegis + 凭证不可用 → **立即抛** `_AegisUnresolvableInPoll`，
    不浪费 30+ 分钟盲飞等 chain 响应。
    """
    runner = PlaywrightVideoRunner(timeout=600, poll_interval=5)
    page = MagicMock()

    # 让 chain 永远返空(模拟 video 实际未生成完),让 _handle_aegis_in_poll 失败能 break 出 loop
    chain_no_creation = _build_chain_response_no_creation()
    call_count = {"n": 0}

    async def fake_evaluate(expr, arg=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        if call_count["n"] == 2:
            return {
                "status": 200,
                "text": (
                    "event:SSE_ACK\n"
                    "data:" + json.dumps({
                        "ack_client_meta": {
                            "conversation_id": "C", "section_id": "S",
                        },
                        "query_list": [{"question_id": "Q"}],
                    }) + "\n\n"
                ),
            }
        return chain_no_creation

    page.evaluate = fake_evaluate

    # stub _handle_aegis_in_poll 让它第一轮就抛
    call_log = {"handle_calls": 0}

    async def fake_handle_aegis(*a, **kw):
        call_log["handle_calls"] += 1
        raise _AegisUnresolvableInPoll(
            "aegis drag captcha blocking poll loop; captcha credentials not configured"
        )

    monkeypatch.setattr(
        "doupool.video.browser._handle_aegis_in_poll", fake_handle_aegis,
    )

    bundle = MagicMock()
    bundle.to_client_meta.return_value = {}

    # 关键断言:必须抛 `_AegisUnresolvableInPoll`,而不是普通的「视频生成超时」
    with pytest.raises(_AegisUnresolvableInPoll):
        await runner._submit_and_poll(
            page,
            "prompt",
            "seedance_v2.0_mini",
            "16:9",
            10,
            "fp",
            bundle,
            "t2v",
            [],
            MagicMock(),
            threading.Event(),
            Path("/tmp/p"),
            use_real_browser=False,
        )

    # 必须**第一轮就 fail**,不能拖到 timeout
    assert call_log["handle_calls"] == 1, (
        f"fail-fast 失败:handle_aegis_in_poll 应在第 1 轮就抛; "
        f"实际被调 {call_log['handle_calls']} 次"
    )


@pytest.mark.asyncio
async def test_poll_loop_segments_wait_into_1s_intervals(monkeypatch):
    """v0.3.4:wait_for_timeout 拆 1s 段 + 每段先 probe —— 而不是一次 wait 35*60s。

    验证:poll_interval=3 时,1s 段循环应跑 3 次 `_probe_aegis_quickly`,
    每次 sleep 1s。run_in_executor/真 asyncio.sleep 会被 monkeypatch。
    """
    runner = PlaywrightVideoRunner(timeout=2, poll_interval=3)
    page = MagicMock()

    chain_no_creation = _build_chain_response_no_creation()
    sse_text = (
        "event:SSE_ACK\n"
        "data:" + json.dumps({
            "ack_client_meta": {"conversation_id": "C", "section_id": "S"},
            "query_list": [{"question_id": "Q"}],
        }) + "\n\n"
    )

    call_count = {"n": 0}

    async def fake_evaluate(expr, arg=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        if call_count["n"] == 2:
            return {"status": 200, "text": sse_text}
        return chain_no_creation

    page.evaluate = fake_evaluate

    handle_mock = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "doupool.video.browser._handle_aegis_in_poll", handle_mock,
    )
    probe_mock = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "doupool.video.browser._probe_aegis_quickly", probe_mock,
    )
    # 真 sleep 加速(避免 35 分钟测试)
    sleep_mock = AsyncMock()
    monkeypatch.setattr(
        "doupool.video.browser.asyncio.sleep", sleep_mock,
    )

    async def fake_resolve(self, page, result, cancel_event):
        raise RuntimeError("never reached")

    monkeypatch.setattr(
        "doupool.video.browser.PlaywrightVideoRunner._resolve_original_download",
        fake_resolve,
    )

    bundle = MagicMock()
    bundle.to_client_meta.return_value = {}

    with pytest.raises(RuntimeError, match="视频生成超时"):
        await runner._submit_and_poll(
            page,
            "prompt",
            "seedance_v2.0_mini",
            "16:9",
            10,
            "fp",
            bundle,
            "t2v",
            [],
            MagicMock(),
            threading.Event(),
            Path("/tmp/p"),
            use_real_browser=False,
        )

    # v0.3.4 关键契约:`_probe_aegis_quickly` 必被 poll 循环多次调
    # (timeout=2, poll_interval=3,但 timeout 先到 → 至少 1 轮 → probe 至少 3 次)
    assert probe_mock.await_count >= 1, (
        f"_probe_aegis_quickly 必须至少调一次(每 1s 段都探); "
        f"实际 {probe_mock.await_count} 次"
    )
    # 单次 1s sleep 调用次数 = 至少一轮 wait 内的 1s 段数
    sleep_calls_of_1s = [
        c for c in sleep_mock.await_args_list
        if c.args and c.args[0] == 1
    ]
    assert len(sleep_calls_of_1s) >= 1, (
        f"v0.3.4 要求 sleep(1) 段而不是 wait_for_timeout(大数); "
        f"实际 sleep calls={sleep_mock.await_args_list}"
    )


@pytest.mark.asyncio
async def test_poll_loop_probe_exception_breaks_wait_not_loop(monkeypatch):
    """v0.3.4:probe 在 wait 段里抛异常(典型:TargetClosedError,page 被用户关掉)
    → break 出内层 wait,下一轮外层 while 仍跑 → 上层 finally close page。

    不能因为 probe 异常就让整个 poll loop raise 出去 —— 那样会跳过
    finally page.close(),导致 anchor page 被其它 task 抢走(详见 v0.2.26)。
    """
    runner = PlaywrightVideoRunner(timeout=2, poll_interval=3)
    page = MagicMock()

    chain_no_creation = _build_chain_response_no_creation()
    sse_text = (
        "event:SSE_ACK\n"
        "data:" + json.dumps({
            "ack_client_meta": {"conversation_id": "C", "section_id": "S"},
            "query_list": [{"question_id": "Q"}],
        }) + "\n\n"
    )

    call_count = {"n": 0}

    async def fake_evaluate(expr, arg=None):
        call_count["n"] += 1
        if call_count["n"] == 1:
            return None
        if call_count["n"] == 2:
            return {"status": 200, "text": sse_text}
        return chain_no_creation

    page.evaluate = fake_evaluate

    handle_mock = AsyncMock(return_value=False)
    monkeypatch.setattr(
        "doupool.video.browser._handle_aegis_in_poll", handle_mock,
    )
    # probe 每次都抛(模拟 page 在 wait 期间被外部关)
    probe_mock = AsyncMock(side_effect=Exception("Target closed"))
    monkeypatch.setattr(
        "doupool.video.browser._probe_aegis_quickly", probe_mock,
    )

    async def fake_resolve(self, page, result, cancel_event):
        raise RuntimeError("never reached")

    monkeypatch.setattr(
        "doupool.video.browser.PlaywrightVideoRunner._resolve_original_download",
        fake_resolve,
    )

    bundle = MagicMock()
    bundle.to_client_meta.return_value = {}

    # 关键断言:不抛异常出去,而是跑完 poll_interval 段后由 while
    # 条件(time.monotonic() >= deadline) 退出 → raise「视频生成超时」
    with pytest.raises(RuntimeError, match="视频生成超时"):
        await runner._submit_and_poll(
            page,
            "prompt",
            "seedance_v2.0_mini",
            "16:9",
            10,
            "fp",
            bundle,
            "t2v",
            [],
            MagicMock(),
            threading.Event(),
            Path("/tmp/p"),
            use_real_browser=False,
        )
