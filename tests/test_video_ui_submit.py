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
- monkeypatch 提交前 aegis gate，避免单测等待真实探测窗口
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

from doupool.video import protocol as protocol_module
from doupool.video import browser as browser_module
from doupool.video.browser import (
    EDITOR_SEL,
    SEND_BTN_SEL,
    SEND_BTN_FALLBACK_SEL,
    VIDEO_TAB_SEL,
    PlaywrightVideoRunner,
    _AckWaitTimeout,
    _AegisUnresolvableInPoll,
    _ack_interceptor,
    _build_launch_kwargs,
    _build_video_prompt,
    _enter_video_generation_mode,
    _extract_local_message_ids_from_ack_payload,
    _wait_for_visible_exact_text,
    _wait_for_ack,
    clear_prose_mirror,
    submit_via_ui,
    try_click,
)


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


@pytest.fixture(autouse=True)
def _stub_video_mode_entry_for_submit_contracts(monkeypatch):
    """多数 submit 测试聚焦粘贴/发送；新入口细节在顺序用例中单独覆盖。"""
    model_button = MagicMock()
    monkeypatch.setattr(
        "doupool.video.browser._enter_video_generation_mode",
        AsyncMock(return_value=model_button),
    )
    monkeypatch.setattr(
        "doupool.video.browser._select_video_model",
        AsyncMock(),
    )


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
async def test_video_entry_waits_for_delayed_react_menu_mount(monkeypatch):
    candidate = MagicMock()
    candidate.is_enabled = AsyncMock(return_value=True)
    attempts = 0

    async def delayed_visible(_page, _selector, _text):
        nonlocal attempts
        attempts += 1
        return candidate if attempts == 3 else None

    monkeypatch.setattr(browser_module, "_first_visible_exact_text", delayed_visible)
    monkeypatch.setattr(browser_module.asyncio, "sleep", AsyncMock())

    result = await _wait_for_visible_exact_text(
        MagicMock(),
        ("strict-selector",),
        "视频生成",
        timeout_ms=1_000,
        require_enabled=True,
    )

    assert result is candidate
    assert attempts == 3


@pytest.mark.asyncio
async def test_video_entry_uses_skill_item_exact_text_fallback(monkeypatch):
    candidate = MagicMock()
    candidate.is_enabled = AsyncMock(return_value=True)
    seen_selectors: list[str] = []

    async def fallback_visible(_page, selector, _text):
        seen_selectors.append(selector)
        return candidate if selector == "skill-item-fallback" else None

    monkeypatch.setattr(browser_module, "_first_visible_exact_text", fallback_visible)

    result = await _wait_for_visible_exact_text(
        MagicMock(),
        ("strict-selector", "skill-item-fallback"),
        "视频生成",
        require_enabled=True,
    )

    assert result is candidate
    assert seen_selectors == ["strict-selector", "skill-item-fallback"]


@pytest.mark.asyncio
async def test_video_entry_keeps_new_skill_path_primary(monkeypatch):
    page = _FakePage(url="https://www.doubao.com/chat")
    new_conversation = _FakeElement()
    more = _FakeElement()
    video_generation = _FakeElement()
    model_button = MagicMock()
    ready = AsyncMock(return_value=model_button)
    legacy_tab = AsyncMock()
    candidates = iter((new_conversation, more, video_generation))

    monkeypatch.setattr(
        browser_module,
        "_wait_for_visible_exact_text",
        AsyncMock(side_effect=lambda *_args, **_kwargs: next(candidates)),
    )
    monkeypatch.setattr(browser_module, "_wait_for_video_generation_mode_ready", ready)
    monkeypatch.setattr(browser_module, "_activate_legacy_video_tab", legacy_tab)

    result = await _enter_video_generation_mode(page)

    assert result is model_button
    assert new_conversation.clicks == 1
    assert more.clicks == 1
    assert video_generation.clicks == 1
    assert page.goto_calls == []
    legacy_tab.assert_not_awaited()
    ready.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_video_entry_falls_back_to_layout_b_chip_after_menu_missing(
    monkeypatch,
):
    page = _FakePage(url="https://www.doubao.com/chat")
    new_conversation = _FakeElement()
    more = _FakeElement()
    model_button = MagicMock()
    layout_b_ready = AsyncMock(return_value=model_button)
    chip_click = AsyncMock()
    menu_checks = 0

    async def wait_exact(_page, _selectors, text, **_kwargs):
        nonlocal menu_checks
        if text == "新对话":
            return new_conversation
        if text == "更多":
            return more
        assert text == "视频生成"
        menu_checks += 1
        return None

    monkeypatch.setattr(browser_module, "_wait_for_visible_exact_text", wait_exact)
    monkeypatch.setattr(browser_module, "_wait_for_video_generation_mode_ready", layout_b_ready)
    monkeypatch.setattr(browser_module, "_click_video_mode_chip", chip_click)

    result = await _enter_video_generation_mode(page)

    assert result is model_button
    assert menu_checks == 2
    assert more.clicks == 2
    assert page.keyboard.presses == ["Escape", "Escape"]
    assert page.goto_calls == []
    chip_click.assert_awaited_once_with(page)
    layout_b_ready.assert_awaited_once_with(page)


@pytest.mark.asyncio
async def test_video_entry_missing_skill_without_layout_b_chip_fails_fast(monkeypatch):
    page = _FakePage(url="https://www.doubao.com/chat")
    new_conversation = _FakeElement()
    more = _FakeElement()

    async def wait_exact(_page, _selectors, text, **_kwargs):
        if text == "新对话":
            return new_conversation
        if text == "更多":
            return more
        return None

    monkeypatch.setattr(browser_module, "_wait_for_visible_exact_text", wait_exact)
    monkeypatch.setattr(
        browser_module,
        "_click_video_mode_chip",
        AsyncMock(side_effect=RuntimeError("该账号未开通视频生成入口")),
    )

    with pytest.raises(RuntimeError, match="未开通视频生成入口"):
        await _enter_video_generation_mode(page)

    assert page.goto_calls == []


@pytest.mark.asyncio
async def test_layout_b_chip_click_prefers_clickable_ancestor(monkeypatch):
    page = MagicMock(url="https://www.doubao.com/chat")
    chip_locator = MagicMock()
    chip = MagicMock()
    ancestor_locator = MagicMock()
    ancestor = MagicMock()
    chip_locator.selector = browser_module._VIDEO_MODE_CHIP_SEL
    page.locator.return_value = chip_locator
    chip.locator.return_value = ancestor_locator
    ancestor.click = AsyncMock()
    chip.click = AsyncMock()

    async def first_visible(locator):
        if locator is chip_locator:
            return chip
        if locator is ancestor_locator:
            return ancestor
        return None

    monkeypatch.setattr(browser_module, "_first_visible_locator", first_visible)

    await browser_module._click_video_mode_chip(page)

    chip.locator.assert_any_call(
        "xpath=ancestor-or-self::*[self::button or @role='button' "
        "or @tabindex='0'][1]"
    )
    ancestor.click.assert_awaited_once_with(timeout=3_000)
    chip.click.assert_not_awaited()


@pytest.mark.asyncio
async def test_layout_b_chip_click_falls_back_to_chip_node(monkeypatch):
    page = MagicMock(url="https://www.doubao.com/chat")
    chip_locator = MagicMock()
    chip = MagicMock()
    ancestor_locator = MagicMock()
    page.locator.return_value = chip_locator
    chip.locator.return_value = ancestor_locator
    chip.click = AsyncMock()

    async def first_visible(locator):
        if locator is chip_locator:
            return chip
        if locator is chip:
            return chip
        return None

    monkeypatch.setattr(browser_module, "_first_visible_locator", first_visible)

    await browser_module._click_video_mode_chip(page)

    chip.click.assert_awaited_once_with(timeout=3_000)


@pytest.mark.asyncio
async def test_layout_b_chip_click_without_chip_fails_fast(monkeypatch):
    page = MagicMock(url="https://www.doubao.com/chat")
    page.locator.side_effect = lambda _selector: MagicMock()

    async def no_visible(_locator):
        return None

    monkeypatch.setattr(browser_module, "_first_visible_locator", no_visible)

    with pytest.raises(RuntimeError, match="未开通视频生成入口"):
        await browser_module._click_video_mode_chip(page)


@pytest.mark.asyncio
async def test_legacy_tab_activation_does_not_open_video_options(monkeypatch):
    page = _FakePage(url=browser_module.CREATE_IMAGE_URL)
    candidate = MagicMock()
    candidate.get_attribute = AsyncMock(return_value="false")
    monkeypatch.setattr(
        browser_module,
        "_wait_for_visible_video_tab_candidate",
        AsyncMock(return_value=candidate),
    )
    monkeypatch.setattr(
        browser_module,
        "_video_tab_candidate_signature",
        AsyncMock(return_value=("视频", (1, 2, 3, 4))),
    )
    click = AsyncMock()
    validate_options = AsyncMock()
    monkeypatch.setattr(browser_module, "_click_video_tab_candidate", click)
    monkeypatch.setattr(browser_module, "_validate_video_tab_content", validate_options)

    await browser_module._activate_legacy_video_tab(page)

    click.assert_awaited_once_with(page, candidate)
    validate_options.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_tab_activation_keeps_already_selected_tab(monkeypatch):
    page = _FakePage(url=browser_module.CREATE_IMAGE_URL)
    candidate = MagicMock()

    async def selected_attribute(name):
        return "true" if name == "aria-selected" else None

    candidate.get_attribute = selected_attribute
    monkeypatch.setattr(
        browser_module,
        "_wait_for_visible_video_tab_candidate",
        AsyncMock(return_value=candidate),
    )
    monkeypatch.setattr(
        browser_module,
        "_video_tab_candidate_signature",
        AsyncMock(return_value=("视频", (1, 2, 3, 4))),
    )
    click = AsyncMock()
    monkeypatch.setattr(browser_module, "_click_video_tab_candidate", click)

    await browser_module._activate_legacy_video_tab(page)

    click.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_video_mode_ready_requires_editor_and_model(monkeypatch):
    page = _FakePage(url=browser_module.CREATE_IMAGE_URL)
    editor = MagicMock()
    model_button = MagicMock()
    model_button.inner_text = AsyncMock(return_value="模型\nSeedance 2.0 Mini")
    visible = AsyncMock(side_effect=(editor, model_button))
    monkeypatch.setattr(browser_module, "_first_visible_locator", visible)

    result = await browser_module._wait_for_legacy_video_generation_ready(page)

    assert result is model_button
    assert visible.await_count == 2


@pytest.mark.asyncio
async def test_submit_via_ui_enters_video_mode_selects_model_and_sends(monkeypatch):
    page = _FakePage(url="https://www.doubao.com/chat/create-image")
    update = MagicMock()
    options_mock = AsyncMock()
    monkeypatch.setattr(
        "doupool.video.browser._apply_video_options",
        options_mock,
    )
    monkeypatch.setattr("doupool.video.browser._pre_submit_aegis_gate", AsyncMock())
    await submit_via_ui(
        page,
        "测试一只小狗",
        model="seedance_v2.0_mini",
        ratio="16:9",
        duration=10,
        profile_dir=Path("/tmp/p"),
        update=update,
    )
    options_mock.assert_not_awaited()
    from doupool.video import browser as browser_mod
    browser_mod._enter_video_generation_mode.assert_awaited_once_with(page)
    browser_mod._select_video_model.assert_awaited_once_with(
        page,
        browser_mod._enter_video_generation_mode.return_value,
        "seedance_v2.0_mini",
    )
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
    assert any(arg == "测试一只小狗。时长10秒，比例16:9" for _expr, arg in write_calls), (
        f"prompt 必须整段传给 clipboard.writeText; 实际 evaluates={page.evaluates}"
    )
    # 2) keyboard.type 不该被调用(整段贴,不是逐字打)
    assert page.keyboard.types == [], (
        f"v0.3.2.1 起整段 prompt 走 paste; keyboard.type 应为空, 实际={page.keyboard.types}"
    )
    # 3) Ctrl+V 必须 press 一次
    assert "Control+V" in page.keyboard.presses


@pytest.mark.asyncio
async def test_submit_via_ui_uses_sidebar_entry_without_direct_navigation(monkeypatch):
    page = _FakePage(url="https://www.doubao.com/chat/123")
    monkeypatch.setattr(
        "doupool.video.browser._apply_video_options",
        AsyncMock(),
    )
    monkeypatch.setattr("doupool.video.browser._pre_submit_aegis_gate", AsyncMock())
    await submit_via_ui(
        page,
        "x",
        model="seedance_v2.0_mini",
        ratio="1:1",
        duration=10,
        profile_dir=Path("/tmp/p"),
        update=MagicMock(),
    )
    assert page.goto_calls == []


@pytest.mark.asyncio
async def test_submit_via_ui_new_entry_order_before_paste_and_send(monkeypatch):
    page = _FakePage()
    events: list[str] = []

    async def fake_gate(*_args, **_kwargs):
        events.append("aegis-gate")

    model_button = MagicMock()

    async def fake_enter(_page):
        events.extend(["new-conversation", "more", "video-generation", "mode-ready"])
        return model_button

    async def fake_model(_page, button, task_model):
        assert button is model_button
        assert task_model == "seedance_v2.0_std"
        events.extend(["model-selected", "model-readback"])

    async def fake_clear(_page):
        events.append("clear")

    async def fake_try_click(_page, selectors, **_kwargs):
        assert selectors[0] == SEND_BTN_SEL
        events.append("send")

    async def tracked_evaluate(expr, arg=None):
        if "writeText" in str(expr):
            events.append("paste")
        return None

    monkeypatch.setattr("doupool.video.browser._pre_submit_aegis_gate", fake_gate)
    monkeypatch.setattr("doupool.video.browser.try_click", fake_try_click)
    monkeypatch.setattr("doupool.video.browser._enter_video_generation_mode", fake_enter)
    monkeypatch.setattr("doupool.video.browser._select_video_model", fake_model)
    monkeypatch.setattr("doupool.video.browser.clear_prose_mirror", fake_clear)
    page.evaluate = tracked_evaluate

    await submit_via_ui(
        page,
        "prompt",
        model="seedance_v2.0_std",
        ratio="16:9",
        duration=10,
        profile_dir=Path("/tmp/p"),
        update=MagicMock(),
    )

    assert events == [
        "new-conversation",
        "more",
        "video-generation",
        "mode-ready",
        "model-selected",
        "model-readback",
        "aegis-gate",
        "clear",
        "paste",
        "aegis-gate",
        "send",
    ]


@pytest.mark.asyncio
async def test_submit_via_ui_does_not_paste_or_send_when_model_readback_fails(monkeypatch):
    page = _FakePage()
    events: list[str] = []

    async def fake_gate(*_args, **_kwargs):
        return True

    async def fake_try_click(_page, selectors, **_kwargs):
        events.append("send")

    async def failing_model(*_args, **_kwargs):
        events.append("model")
        raise RuntimeError("视频模型选中状态回读失败")

    async def tracked_evaluate(expr, arg=None):
        if "writeText" in str(expr):
            events.append("paste")
        return None

    monkeypatch.setattr("doupool.video.browser._pre_submit_aegis_gate", fake_gate)
    monkeypatch.setattr("doupool.video.browser.try_click", fake_try_click)
    monkeypatch.setattr("doupool.video.browser._select_video_model", failing_model)
    page.evaluate = tracked_evaluate

    with pytest.raises(RuntimeError, match="视频模型选中状态回读失败"):
        await submit_via_ui(
            page,
            "prompt",
            model="seedance_v2.0_mini",
            ratio="16:9",
            duration=10,
            profile_dir=Path("/tmp/p"),
            update=MagicMock(),
        )

    assert events == ["model"]


@pytest.mark.parametrize(
    ("prompt", "expected"),
    [
        ("橘猫伸懒腰", "橘猫伸懒腰。时长10秒，比例9:16"),
        ("城市夜景。", "城市夜景。时长10秒，比例16:9"),
        ("海浪!  ", "海浪!时长10秒，比例1:1"),
    ],
)
def test_build_video_prompt_appends_suffix_once(prompt, expected):
    assert _build_video_prompt(prompt, ratio=expected.rsplit("比例", 1)[1], duration=10) == expected


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
    async with _ack_interceptor(page) as state:
        assert len(page._added_handlers) == 2
        assert [event for event, _handler in page._added_handlers] == [
            "request",
            "response",
        ]
        assert state["request_seen"] is False
        request_handler = page._added_handlers[0][1]
        request_handler(MagicMock(url="https://www.doubao.com/chat/completion"))
        assert state["request_seen"] is True
        assert len(page._removed_handlers) == 0
    assert len(page._removed_handlers) == 2
    assert page._added_handlers == page._removed_handlers


@pytest.mark.asyncio
async def test_ack_interceptor_removes_listener_even_on_exception():
    page = _FakePage()
    with pytest.raises(RuntimeError):
        async with _ack_interceptor(page):
            raise RuntimeError("boom")
    assert len(page._removed_handlers) == 2


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
    with pytest.raises(_AckWaitTimeout, match="等待 /chat/completion 响应超时") as exc_info:
        await _wait_for_ack(state, timeout=0.1)
    assert exc_info.value.request_seen is False


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

    submit_kwargs = {}

    async def fake_submit_via_ui(*a, **kw):
        submit_kwargs.update(kw)

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
    monkeypatch.setattr("doupool.video.browser._pre_submit_aegis_gate", AsyncMock())

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
    assert submit_kwargs["model"] == "seedance_v2.0_mini"
    assert submit_kwargs["ratio"] == "16:9"
    assert submit_kwargs["duration"] == 10
    # update 至少调过一次 status=generating 把 ack 写进去
    update.assert_any_call(status="generating", **result)


@pytest.mark.asyncio
async def test_submit_and_poll_retries_once_only_when_request_was_not_seen(
    monkeypatch,
    tmp_path,
):
    import doupool.video.browser as browser_mod

    runner = PlaywrightVideoRunner(timeout=10, poll_interval=1)
    submits: list[dict] = []
    wait_calls = 0

    @asynccontextmanager
    async def fake_interceptor(_page):
        yield {"request_seen": False}

    async def fake_submit(_page, _prompt, **kwargs):
        submits.append(kwargs)

    async def fake_wait(state, *, timeout):
        nonlocal wait_calls
        wait_calls += 1
        if wait_calls == 1:
            raise _AckWaitTimeout(timeout, request_seen=False)
        return "ack"

    result_payload = {
        "remote_task_id": "cid",
        "vid": "vid",
        "fallback_result_url": "https://example/video.mp4",
        "cover_url": "",
    }
    page = MagicMock()
    page.evaluate = AsyncMock(return_value={"status": 200, "data": {}})
    monkeypatch.setattr(browser_mod, "_ack_interceptor", fake_interceptor)
    monkeypatch.setattr(browser_mod, "submit_via_ui", fake_submit)
    monkeypatch.setattr(browser_mod, "_wait_for_ack", fake_wait)
    monkeypatch.setattr(browser_mod, "_handle_aegis_in_poll", AsyncMock(return_value=False))
    monkeypatch.setattr(
        browser_mod,
        "parse_sse_ack",
        lambda _text: {"conversation_id": "c", "section_id": "s", "question_id": "q"},
    )
    monkeypatch.setattr(browser_mod, "parse_creation_result", MagicMock(return_value=result_payload))
    monkeypatch.setattr(
        PlaywrightVideoRunner,
        "_resolve_original_download",
        AsyncMock(return_value=result_payload),
    )

    await runner._submit_and_poll(
        page,
        "base prompt",
        "seedance_v2.0_mini",
        "9:16",
        10,
        "fp",
        MagicMock(),
        "t2v",
        [],
        MagicMock(),
        threading.Event(),
        tmp_path / "profile",
        use_real_browser=True,
    )

    assert len(submits) == 2
    assert all(call["model"] == "seedance_v2.0_mini" for call in submits)
    assert wait_calls == 2


@pytest.mark.asyncio
async def test_submit_and_poll_does_not_resend_after_ambiguous_request_timeout(
    monkeypatch,
    tmp_path,
):
    import doupool.video.browser as browser_mod

    runner = PlaywrightVideoRunner(timeout=10, poll_interval=1)
    submit = AsyncMock()

    @asynccontextmanager
    async def fake_interceptor(_page):
        yield {"request_seen": True}

    monkeypatch.setattr(browser_mod, "_ack_interceptor", fake_interceptor)
    monkeypatch.setattr(browser_mod, "submit_via_ui", submit)
    monkeypatch.setattr(
        browser_mod,
        "_wait_for_ack",
        AsyncMock(side_effect=_AckWaitTimeout(0.1, request_seen=True)),
    )
    monkeypatch.setattr(browser_mod, "_handle_aegis_in_poll", AsyncMock(return_value=False))

    with pytest.raises(_AckWaitTimeout) as exc_info:
        await runner._submit_and_poll(
            MagicMock(), "prompt", "seedance_v2.0_mini", "1:1", 10,
            "fp", MagicMock(), "t2v", [], MagicMock(), threading.Event(),
            tmp_path / "profile", use_real_browser=True,
        )
    assert exc_info.value.request_seen is True
    assert submit.await_count == 1


@pytest.mark.asyncio
async def test_submit_and_poll_aegis_after_send_timeout_never_resends(
    monkeypatch,
    tmp_path,
):
    import doupool.video.browser as browser_mod

    runner = PlaywrightVideoRunner(timeout=10, poll_interval=1)
    submit = AsyncMock()

    @asynccontextmanager
    async def fake_interceptor(_page):
        yield {"request_seen": False}

    monkeypatch.setattr(browser_mod, "_ack_interceptor", fake_interceptor)
    monkeypatch.setattr(browser_mod, "submit_via_ui", submit)
    monkeypatch.setattr(
        browser_mod,
        "_wait_for_ack",
        AsyncMock(side_effect=_AckWaitTimeout(0.1, request_seen=False)),
    )
    monkeypatch.setattr(
        browser_mod,
        "_handle_aegis_in_poll",
        AsyncMock(side_effect=_AegisUnresolvableInPoll("aegis")),
    )

    with pytest.raises(_AegisUnresolvableInPoll, match="aegis"):
        await runner._submit_and_poll(
            MagicMock(), "prompt", "seedance_v2.0_mini", "1:1", 10,
            "fp", MagicMock(), "t2v", [], MagicMock(), threading.Event(),
            tmp_path / "profile", use_real_browser=True,
        )
    assert submit.await_count == 1


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
    assert EDITOR_SEL == "[contenteditable='true'][role='textbox']"
    assert SEND_BTN_SEL == "#flow-end-msg-send"
    assert SEND_BTN_FALLBACK_SEL == ".send-btn-wrapper button"


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
        "doupool.video.browser._apply_video_options",
        AsyncMock(),
    )
    monkeypatch.setattr("doupool.video.browser._pre_submit_aegis_gate", AsyncMock())
    await submit_via_ui(
        page,
        LONG_PROMPT,
        model="seedance_v2.0_mini",
        ratio="16:9",
        duration=10,
        profile_dir=Path("/tmp/p"),
        update=MagicMock(),
    )

    # 1) 整段 prompt(>500 字)进了 clipboard,只有一次 writeText
    write_calls = [
        arg for _expr, arg in page.evaluates
        if "writeText" in str(_expr)
    ]
    assert len(write_calls) == 1, (
        f"应只调一次 writeText; 实际 {len(write_calls)} 次"
    )
    assert write_calls[0] == LONG_PROMPT + "时长10秒，比例16:9", (
        "writeText 必须完整保留原 prompt，并且只追加一次参数后缀"
    )
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
# ------------------------- v0.3.4:poll 循环 aegis fail-fast ------------------------- #
#
# 背景:用户原话「我在后台看到视频已经生成成功了,你特么还卡在生成中」。
# 真根因:poll 循环里 `_try_solve_captcha_in_video` 被 30 分钟 cooldown 短路,
# 用户关掉弹窗或凭证没配时,aegis 持续挡 chain → poll 盲飞到超时。
#
# v0.3.4 修复:
#   1. 加 `_handle_aegis_in_poll`(廉价探测 + fail-fast)
#   2. poll 循环 wait_for_timeout 拆 1s 段,每段先 `aegis_popup_present`
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
        raise _AegisUnresolvableInPoll("当前任务已停止，请在账号管理中打开该账号浏览器完成验证后重提")

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

    验证:poll_interval=3 时,1s 段循环应跑 3 次 `aegis_popup_present`,
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
        "doupool.video.browser.aegis_popup_present", probe_mock,
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

    # v0.3.4 关键契约:`aegis_popup_present` 必被 poll 循环多次调
    # (timeout=2, poll_interval=3,但 timeout 先到 → 至少 1 轮 → probe 至少 3 次)
    assert probe_mock.await_count >= 1, (
        f"aegis_popup_present 必须至少调一次(每 1s 段都探); "
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
        "doupool.video.browser.aegis_popup_present", probe_mock,
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
