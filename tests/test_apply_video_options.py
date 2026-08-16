from __future__ import annotations

import asyncio
import logging
import re

import pytest

from doupool.video.browser import _apply_video_options, _close_video_options


class _FakeMouse:
    def __init__(self):
        self.moves: list[tuple] = []
        self.downs = 0
        self.ups = 0

    async def move(self, *args, **kwargs):
        self.moves.append((*args, kwargs))

    async def down(self):
        self.downs += 1

    async def up(self):
        self.ups += 1


class _FakeKeyboard:
    def __init__(self, page):
        self.page = page
        self.presses: list[str] = []

    async def press(self, key: str):
        self.presses.append(key)
        if key == "Escape" and self.page.escape_closes:
            if self.page.escape_close_delay_ms > 0 and self.page.menu_open:
                self.page.pending_escape_close_ms = self.page.escape_close_delay_ms
            else:
                self.page.menu_open = False


class _FakeElement:
    def __init__(self, page, kind: str, value: str | None = None):
        self.page = page
        self.kind = kind
        self.value = value

    def _text(self) -> str:
        if self.kind == "trigger":
            text = (
                f"{self.page.display_ratio} · {self.page.display_duration}s"
            )
            return (
                f"{self.page.trigger_prefix}{text}{self.page.trigger_suffix}"
            )
        return self.value or ""

    async def is_visible(self) -> bool:
        if self.kind == "trigger":
            return self.page.has_trigger
        if self.kind in {"ratio", "range", "aria"}:
            return self.page.menu_open and self.page.render_delay_ms <= 0
        return True

    async def click(self):
        if self.kind == "trigger":
            self.page.trigger_clicks += 1
            if self.page.menu_opens:
                if self.page.menu_open:
                    self.page.menu_open = False
                else:
                    self.page.menu_open = True
                    self.page.render_delay_ms = (
                        self.page.open_delays.pop(0)
                        if self.page.open_delays
                        else 0
                    )
        elif self.kind == "ratio":
            self.page.ratio_clicks.append(self.value)
            self.page.ratio = str(self.value)
            self.page.schedule_trigger_update()
            if self.page.close_menu_on_ratio_click:
                self.page.menu_open = False

    async def fill(self, value: str):
        if self.kind != "range":
            raise RuntimeError("not a range input")
        self.page.range_fills.append(value)
        self.page.duration = int(value)
        self.page.schedule_trigger_update()

    async def evaluate(self, _expression, value):
        self.page.duration = int(value)
        self.page.schedule_trigger_update()

    async def input_value(self) -> str:
        return str(self.page.duration)

    async def get_attribute(self, name: str) -> str | None:
        if self.kind != "aria":
            return None
        if name == "aria-valuemin":
            return "4"
        if name == "aria-valuemax":
            return "15"
        if name == "aria-valuenow":
            return str(self.page.duration)
        return None

    async def focus(self):
        self.page.slider_focused = True

    async def press(self, key: str):
        if self.kind != "aria":
            raise RuntimeError("not an aria slider")
        self.page.slider_presses.append(key)
        if key == "Home":
            self.page.duration = 4
        elif key == "ArrowRight":
            self.page.duration = min(15, self.page.duration + 1)
        self.page.schedule_trigger_update()

    async def inner_text(self) -> str:
        if self.kind == "trigger":
            self.page.trigger_inner_text_calls += 1
            if self.page.trigger_inner_text_calls <= self.page.readback_stale_calls:
                stale = self.page.readback_stale_duration
                if stale is not None:
                    return (
                        f"{self.page.trigger_prefix}{self.page.display_ratio} · "
                        f"{stale}s{self.page.trigger_suffix}"
                    )
        return self._text()

    async def bounding_box(self):
        if self.kind == "track":
            return {"x": 100, "y": 100, "width": 220, "height": 20}
        return {"x": 200, "y": 100, "width": 16, "height": 16}

    def locator(self, selector: str):
        assert selector == "xpath=.."
        return _FakeElement(self.page, "track")


class _FakeLocator:
    def __init__(self, page, selector: str, text_filter=None):
        self.page = page
        self.selector = selector
        self.text_filter = text_filter

    def _items(self) -> list[_FakeElement]:
        if self.selector == "button":
            items: list[_FakeElement] = []
            if self.page.has_trigger and self.page.trigger_tag == "button":
                items.append(self.page.trigger)
            items.extend(self.page.ratio_elements)
        elif self.selector == "role=button":
            items = []
            if self.page.has_trigger and self.page.trigger_role:
                items.append(self.page.trigger)
        elif self.selector in {"[role='button'], button", "button, [role='button']"}:
            items = []
            if self.page.has_trigger and (
                self.page.trigger_tag == "button" or self.page.trigger_role
            ):
                items.append(self.page.trigger)
            items.extend(self.page.ratio_elements)
            items.extend(self.page.extra_buttons)
        elif self.selector == "input[type='range']":
            items = (
                [self.page.range_element]
                if self.page.slider_kind in {"range", "both"}
                else []
            )
        elif self.selector == "[role='slider']":
            items = (
                [self.page.aria_element]
                if self.page.slider_kind in {"aria", "both"}
                else []
            )
        else:
            items = []

        if self.text_filter is None:
            return items
        if isinstance(self.text_filter, re.Pattern):
            return [item for item in items if self.text_filter.search(item._text())]
        return [item for item in items if str(self.text_filter) in item._text()]

    def filter(self, *, has_text):
        return _FakeLocator(self.page, self.selector, has_text)

    async def count(self) -> int:
        return len(self._items())

    def nth(self, index: int):
        return self._items()[index]


class _FakePage:
    def __init__(
        self,
        *,
        ratio_options: list[str] | None = None,
        slider_kind: str = "range",
        has_trigger: bool = True,
        menu_opens: bool = True,
        escape_closes: bool = True,
        escape_close_delay_ms: int = 0,
        open_delays: list[int] | None = None,
        trigger_updates: bool = True,
        trigger_update_delay_ms: int = 0,
        close_menu_on_ratio_click: bool = False,
        ratio: str = "自动",
        duration: int = 5,
        trigger_tag: str = "button",
        trigger_role: bool = True,
        trigger_prefix: str = "",
        trigger_suffix: str = "",
        readback_stale_calls: int = 0,
        readback_stale_duration: int | None = None,
        extra_buttons: list[str] | None = None,
    ):
        self.url = "https://www.doubao.com/chat/create-image"
        self.has_trigger = has_trigger
        self.menu_opens = menu_opens
        self.escape_closes = escape_closes
        self.escape_close_delay_ms = escape_close_delay_ms
        self.pending_escape_close_ms: int | None = None
        self.open_delays = list(open_delays or [])
        self.render_delay_ms = 0
        self.menu_open = False
        self.ratio = ratio
        self.duration = duration
        self.display_ratio = ratio
        self.display_duration = duration
        self.trigger_tag = trigger_tag
        self.trigger_role = trigger_role
        self.trigger_prefix = trigger_prefix
        self.trigger_suffix = trigger_suffix
        self.readback_stale_calls = readback_stale_calls
        self.readback_stale_duration = readback_stale_duration
        self.trigger_inner_text_calls = 0
        self.trigger_updates = trigger_updates
        self.trigger_update_delay_ms = trigger_update_delay_ms
        self.pending_trigger_update_ms: int | None = None
        self.close_menu_on_ratio_click = close_menu_on_ratio_click
        self.slider_kind = slider_kind
        self.trigger_clicks = 0
        self.ratio_clicks: list[str | None] = []
        self.range_fills: list[str] = []
        self.slider_presses: list[str] = []
        self.slider_focused = False
        self.trigger = _FakeElement(self, "trigger")
        self.extra_buttons = [
            _FakeElement(self, "other", value)
            for value in (extra_buttons or [])
        ]
        self.ratio_elements = [
            _FakeElement(self, "ratio", value)
            for value in (
                ratio_options
                if ratio_options is not None
                else ["自动", "3:4", "4:3", "9:16", "16:9", "1:1", "21:9"]
            )
        ]
        self.range_element = _FakeElement(self, "range")
        self.aria_element = _FakeElement(self, "aria")
        self.keyboard = _FakeKeyboard(self)
        self.mouse = _FakeMouse()

    def locator(self, selector: str):
        return _FakeLocator(self, selector)

    def get_by_role(self, role: str):
        assert role == "button"
        return _FakeLocator(self, "role=button")

    def schedule_trigger_update(self):
        if not self.trigger_updates:
            return
        self.pending_trigger_update_ms = self.trigger_update_delay_ms
        if self.pending_trigger_update_ms == 0:
            self.display_ratio = self.ratio
            self.display_duration = self.duration
            self.pending_trigger_update_ms = None

    async def wait_for_timeout(self, milliseconds: int):
        if self.pending_escape_close_ms is not None:
            real_wait_ms = min(milliseconds, self.pending_escape_close_ms)
            await asyncio.sleep(real_wait_ms / 1_000)
            self.pending_escape_close_ms -= real_wait_ms
            if self.pending_escape_close_ms == 0:
                self.menu_open = False
                self.pending_escape_close_ms = None
        else:
            await asyncio.sleep(0)
        self.render_delay_ms = max(0, self.render_delay_ms - milliseconds)
        if self.pending_trigger_update_ms is not None:
            self.pending_trigger_update_ms = max(
                0,
                self.pending_trigger_update_ms - milliseconds,
            )
            if self.pending_trigger_update_ms == 0:
                self.display_ratio = self.ratio
                self.display_duration = self.duration
                self.pending_trigger_update_ms = None


@pytest.mark.asyncio
async def test_apply_video_options_selects_ratio_and_native_range_duration():
    page = _FakePage(
        ratio_options=["3:4", "4:3", "9:16", "16:9"],
        slider_kind="range",
        duration=5,
    )

    await _apply_video_options(page, ratio="16:9", duration=10)

    assert page.ratio == "16:9"
    assert page.duration == 10
    assert page.ratio_clicks == ["16:9"]
    assert page.range_fills == ["10"]
    assert page.keyboard.presses[-1] == "Escape"
    assert page.menu_open is False


@pytest.mark.asyncio
async def test_apply_video_options_finds_role_button_trigger_with_nested_text():
    """The trigger may be a div[role=button] rather than a native button."""
    page = _FakePage(
        trigger_tag="div",
        trigger_role=True,
        trigger_prefix="视频生成 · ",
        trigger_suffix=" · 默认",
        ratio="自动",
        duration=9,
    )

    await _apply_video_options(page, ratio="1:1", duration=5)

    assert page.trigger_tag == "div"
    assert page.trigger_role is True
    assert page.ratio == "1:1"
    assert page.duration == 5
    assert page.trigger_clicks == 1


@pytest.mark.asyncio
async def test_apply_video_options_retries_readback_for_prefixed_trigger(caplog):
    """A stale first readback must enter the second independent readback phase."""
    # The first phase polls 30 times at 50 ms. Keep the old duration for that
    # entire phase, then expose the updated value on the retry phase.
    page = _FakePage(
        ratio="自动",
        duration=9,
        trigger_prefix="视频生成 · ",
        trigger_suffix=" · 默认",
        readback_stale_calls=30,
        readback_stale_duration=9,
    )

    with caplog.at_level(logging.WARNING, logger="doupool.video.browser"):
        await _apply_video_options(page, ratio="1:1", duration=5)

    assert page.ratio == "1:1"
    assert page.duration == 5
    assert page.trigger_inner_text_calls >= 31
    assert page.trigger._text() == "视频生成 · 1:1 · 5s · 默认"
    assert "event=video_options_readback_retry" in caplog.text
    assert "actual='视频生成 · 1:1 · 9s · 默认'" in caplog.text


@pytest.mark.asyncio
async def test_apply_video_options_missing_trigger_logs_url_and_visible_buttons(caplog):
    page = _FakePage(
        has_trigger=False,
        extra_buttons=["模型 · 默认", "发送"],
    )

    with caplog.at_level(logging.WARNING, logger="doupool.video.browser"):
        with pytest.raises(RuntimeError, match="视频参数按钮未找到"):
            await _apply_video_options(page, ratio="1:1", duration=10)

    assert "event=video_options_trigger_not_found" in caplog.text
    assert page.url in caplog.text
    assert "模型 · 默认" in caplog.text


@pytest.mark.asyncio
async def test_apply_video_options_keeps_native_button_exact_text_path():
    page = _FakePage(
        trigger_tag="button",
        trigger_role=True,
        trigger_prefix="",
        trigger_suffix="",
        ratio="1:1",
        duration=9,
    )

    await _apply_video_options(page, ratio="1:1", duration=5)

    assert page.ratio == "1:1"
    assert page.duration == 5
    assert page.trigger_clicks == 1


@pytest.mark.asyncio
async def test_apply_video_options_uses_aria_slider_fallback():
    page = _FakePage(slider_kind="aria", duration=10)

    await _apply_video_options(page, ratio="1:1", duration=5)

    assert page.ratio == "1:1"
    assert page.duration == 5
    assert page.slider_focused is True
    assert page.slider_presses == ["Home", "ArrowRight"]
    assert page.mouse.downs == 0


@pytest.mark.asyncio
async def test_apply_video_options_rejects_missing_ratio_option():
    page = _FakePage(ratio_options=["3:4", "4:3", "9:16", "21:9"])

    with pytest.raises(RuntimeError, match=r"视频比例选项不存在.*实际可见"):
        await _apply_video_options(page, ratio="16:9", duration=10)


@pytest.mark.asyncio
@pytest.mark.parametrize("duration", [3, 16])
async def test_apply_video_options_rejects_duration_outside_ui_range(duration):
    page = _FakePage()

    with pytest.raises(ValueError, match="4 到 15"):
        await _apply_video_options(page, ratio="1:1", duration=duration)
    assert page.trigger_clicks == 0


@pytest.mark.asyncio
async def test_apply_video_options_rejects_unknown_ratio():
    page = _FakePage()

    with pytest.raises(RuntimeError, match="视频比例选项不存在"):
        await _apply_video_options(page, ratio="2:1", duration=10)
    assert page.trigger_clicks == 0


@pytest.mark.asyncio
async def test_apply_video_options_fails_when_trigger_is_missing():
    page = _FakePage(has_trigger=False)

    with pytest.raises(RuntimeError, match="视频参数按钮未找到"):
        await _apply_video_options(page, ratio="1:1", duration=10)


@pytest.mark.asyncio
async def test_apply_video_options_retries_then_fails_when_menu_does_not_open():
    page = _FakePage(menu_opens=False)

    with pytest.raises(RuntimeError, match="视频参数菜单未展开"):
        await _apply_video_options(page, ratio="1:1", duration=10)
    assert page.trigger_clicks == 2


@pytest.mark.asyncio
async def test_apply_video_options_normalizes_toggle_before_open_retry():
    page = _FakePage(open_delays=[1_100, 0])

    await _apply_video_options(page, ratio="16:9", duration=10)

    assert page.trigger_clicks == 2
    assert page.ratio == "16:9"
    assert page.duration == 10


@pytest.mark.asyncio
async def test_apply_video_options_clicks_trigger_when_escape_does_not_close_menu():
    page = _FakePage(escape_closes=False)

    await _apply_video_options(page, ratio="1:1", duration=10)

    assert page.trigger_clicks == 2
    assert page.menu_open is False


@pytest.mark.asyncio
async def test_apply_video_options_rejects_ambiguous_visible_sliders():
    page = _FakePage(slider_kind="both")

    with pytest.raises(RuntimeError, match="视频时长滑块定位不唯一"):
        await _apply_video_options(page, ratio="1:1", duration=10)


@pytest.mark.asyncio
async def test_apply_video_options_fails_when_trigger_readback_stays_stale():
    page = _FakePage(trigger_updates=False)

    with pytest.raises(RuntimeError, match="视频参数设置后校验失败"):
        await _apply_video_options(page, ratio="16:9", duration=10)


@pytest.mark.asyncio
async def test_apply_video_options_waits_for_delayed_trigger_readback():
    page = _FakePage(trigger_update_delay_ms=200)

    await _apply_video_options(page, ratio="16:9", duration=10)

    assert page.display_ratio == "16:9"
    assert page.display_duration == 10


@pytest.mark.asyncio
async def test_apply_video_options_reopens_menu_when_ratio_click_closes_it():
    page = _FakePage(close_menu_on_ratio_click=True)

    await _apply_video_options(page, ratio="16:9", duration=10)

    assert page.trigger_clicks == 2
    assert page.duration == 10


@pytest.mark.asyncio
async def test_apply_video_options_waits_for_delayed_escape_close_without_toggle():
    page = _FakePage(
        ratio_options=["3:4", "4:3", "9:16", "16:9", "1:1"],
        escape_close_delay_ms=800,
        duration=10,
    )
    started = asyncio.get_running_loop().time()

    await _apply_video_options(page, ratio="1:1", duration=10)

    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed >= 0.8
    assert page.keyboard.presses == ["Escape"]
    assert page.trigger_clicks == 1
    assert page.menu_open is False


@pytest.mark.asyncio
async def test_close_video_options_logs_when_escape_and_toggle_both_fail(caplog):
    page = _FakePage(escape_closes=False, menu_opens=False)
    page.menu_open = True
    expected_visible = ["3:4", "4:3", "9:16", "16:9", "1:1", "21:9"]

    with caplog.at_level(logging.WARNING, logger="doupool.video.browser"):
        with pytest.raises(RuntimeError) as exc_info:
            await _close_video_options(page, page.trigger)

    message = str(exc_info.value)
    assert "trigger_text='自动 · 5s'" in message
    assert f"visible={expected_visible!r}" in message
    assert page.keyboard.presses == ["Escape"]
    assert page.trigger_clicks == 1
    assert "event=video_options_close_failed" in caplog.text
    assert page.url in caplog.text
    assert "trigger_text='自动 · 5s'" in caplog.text
    assert f"visible_after_escape={expected_visible!r}" in caplog.text
    assert f"visible_after_toggle={expected_visible!r}" in caplog.text


@pytest.mark.asyncio
async def test_video_options_readback_failure_logs_expected_and_actual(caplog):
    page = _FakePage(trigger_updates=False, ratio="自动", duration=10)

    with caplog.at_level(logging.WARNING, logger="doupool.video.browser"):
        with pytest.raises(RuntimeError, match="视频参数设置后校验失败"):
            await _apply_video_options(page, ratio="1:1", duration=10)

    assert "event=video_options_readback_failed" in caplog.text
    assert "actual='自动 · 10s'" in caplog.text
    assert "expected='1:1 · 10s'" in caplog.text
    assert page.url in caplog.text
