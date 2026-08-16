from __future__ import annotations

import asyncio
import logging
import re

import pytest

from doupool.video.browser import (
    _apply_video_options,
    _click_video_tab,
    _close_video_options,
    _diagnose_three_dot_candidates,
    _find_video_options_trigger,
    _open_video_options,
    _validate_video_tab_content,
)


_UNSET = object()


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
                self.page.outer_menu_open = False
                self.page.pending_more_close_ms = None
                self.page.closing_more = None


class _FakeElement:
    def __init__(
        self,
        page,
        kind: str,
        value: str | None = None,
        *,
        tag: str = "button",
        role: str | None = None,
        aria_label: str | None = None,
        title: str | None = None,
        class_name: str = "",
        svg_aria_label: str | None = None,
        has_svg: bool = False,
        svg_circle_count: int = 0,
        svg_rect_count: int = 0,
        svg_path_count: int = 0,
        svg_use_href: str | None = None,
        svg_class_name: str = "",
        svg_view_box: str | None = None,
        data_testid: str | None = None,
        has_send_path: bool = False,
        inside_send_wrapper: bool = False,
        inside_tab: bool = False,
        adjacent_to_model: bool = False,
        box: dict[str, float] | None = None,
        action: str = "none",
        open_delays: list[int] | None = None,
        close_delay_ms: int = 0,
        click_error: str | None = None,
    ):
        self.page = page
        self.kind = kind
        self.value = value
        self.tag = tag
        self.role = role
        self.aria_label = aria_label
        self.title = title
        self.class_name = class_name
        self.svg_aria_label = svg_aria_label
        self.svg_circle_count = svg_circle_count
        self.svg_rect_count = svg_rect_count
        self.svg_path_count = svg_path_count
        self.svg_use_href = svg_use_href
        self.svg_class_name = svg_class_name
        self.svg_view_box = svg_view_box
        self.data_testid = data_testid
        self.has_send_path = has_send_path
        self.has_svg = bool(
            has_svg
            or svg_aria_label
            or svg_circle_count
            or svg_rect_count
            or svg_path_count
            or svg_use_href
            or svg_class_name
        )
        self.inside_send_wrapper = inside_send_wrapper
        self.inside_tab = inside_tab
        self.adjacent_to_model = adjacent_to_model
        self.box = box
        self.action = action
        self.open_delays = list(open_delays or [])
        self.close_delay_ms = close_delay_ms
        self.click_error = click_error
        self.click_attempts = 0

    def _text(self) -> str:
        if self.kind == "trigger":
            text = (
                f"{self.page.display_ratio} · {self.page.display_duration}s"
            )
            return (
                f"{self.page.trigger_prefix}{text}{self.page.trigger_suffix}"
            )
        if self.kind == "summary":
            return f"{self.page.display_ratio} · {self.page.display_duration}s >"
        return self.value or ""

    async def is_visible(self) -> bool:
        if self.kind == "trigger":
            return self.page.has_trigger
        if self.kind in {"ratio", "range", "aria"}:
            return self.page.menu_open and self.page.render_delay_ms <= 0
        if self.kind == "summary":
            return self.page.outer_menu_open
        if self.kind == "more" and self.page.closing_more is self:
            return False
        return True

    async def click(self):
        self.click_attempts += 1
        if self.click_error is not None:
            raise RuntimeError(self.click_error)
        if self.kind == "trigger":
            self.page.trigger_clicks += 1
            if self.page.summary_trigger_opens:
                if self.page.menu_open:
                    self.page.menu_open = False
                else:
                    self.page.menu_open = True
                    self.page.render_delay_ms = (
                        self.page.open_delays.pop(0)
                        if self.page.open_delays
                        else 0
                    )
        elif self.kind == "more":
            self.page.more_clicks.append(self)
            if self.action == "toggle_menu" and self.page.menu_opens:
                if self.page.menu_open:
                    if self.close_delay_ms > 0:
                        self.page.pending_more_close_ms = self.close_delay_ms
                        self.page.closing_more = self
                    else:
                        self.page.menu_open = False
                        self.page.outer_menu_open = False
                else:
                    self.page.menu_open = True
                    self.page.outer_menu_open = True
                    if (
                        self.page.duration_has_been_set
                        and self.page.duration_revert_on_reopen is not None
                    ):
                        self.page.duration = self.page.duration_revert_on_reopen
                        self.page.display_duration = self.page.duration
                        self.page.duration_revert_on_reopen = None
                    self.page.render_delay_ms = (
                        self.open_delays.pop(0) if self.open_delays else 0
                    )
            elif self.action == "toggle_outer":
                self.page.outer_menu_open = not self.page.outer_menu_open
                if not self.page.outer_menu_open:
                    self.page.menu_open = False
        elif self.kind == "summary":
            self.page.summary_clicks += 1
            if self.page.menu_opens:
                self.page.menu_open = True
        elif self.kind == "send":
            self.page.send_clicks += 1
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
        self.page.duration_has_been_set = True
        self.page.schedule_trigger_update()

    async def evaluate(self, expression, value=_UNSET):
        if value is not _UNSET:
            self.page.duration = int(value)
            self.page.duration_has_been_set = True
            self.page.schedule_trigger_update()
            return None
        if "closest" in expression:
            return (
                self.inside_send_wrapper
                or self.inside_tab
                or self.has_send_path
            )
        return None

    async def input_value(self) -> str:
        return str(self.page.duration)

    async def get_attribute(self, name: str) -> str | None:
        if self.kind == "aria":
            if name == "aria-valuemin":
                return "4"
            if name == "aria-valuemax":
                return "15"
            if name == "aria-valuenow":
                return str(self.page.duration)
        if name == "role":
            return self.role
        if name == "aria-label":
            return self.aria_label
        if name == "title":
            return self.title
        if name == "class":
            return self.class_name
        if name == "data-testid":
            return self.data_testid
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
        if self.box is not None:
            return self.box
        return {"x": 200, "y": 100, "width": 16, "height": 16}

    def locator(self, selector: str):
        assert selector == "xpath=.."
        return _FakeElement(self.page, "track")


class _FakeLocator:
    def __init__(self, page, selector: str, text_filter=None):
        self.page = page
        self.selector = selector
        self.text_filter = text_filter

    @staticmethod
    def _selector_text(selector: str) -> str | None:
        match = re.search(r":has-text\((?:['\"])(.*?)(?:['\"])\)", selector)
        return match.group(1) if match else None

    @staticmethod
    def _contains_attr(selector: str, attribute: str) -> tuple[str, bool] | None:
        match = re.search(
            rf"\[{re.escape(attribute)}\*=['\"](.*?)['\"](\s+i)?\]",
            selector,
            re.IGNORECASE,
        )
        if match is None:
            return None
        return match.group(1), bool(match.group(2))

    def _matches_selector_group(self, item: _FakeElement, selector: str) -> bool:
        selector = selector.strip()
        if " + " in selector:
            _left, right = selector.split(" + ", 1)
            if not item.adjacent_to_model:
                return False
            selector = right.strip()

        if selector == "role=button":
            return item.tag == "button" or item.role == "button"
        if selector.startswith("button") and item.tag != "button":
            return False
        if selector.startswith("[role='button']") and item.role != "button":
            return False
        if selector.startswith('[role="button"]') and item.role != "button":
            return False
        if selector.startswith(".semi-button") and "semi-button" not in item.class_name:
            return False

        selector_text = self._selector_text(selector)
        if selector_text is not None and selector_text not in item._text():
            return False

        # ``:has(svg[aria-label*=...])`` describes the child SVG, not the
        # outer button's aria-label.
        outer_selector = selector.split(":has(svg", 1)[0]
        aria_match = self._contains_attr(outer_selector, "aria-label")
        if aria_match is not None:
            expected, insensitive = aria_match
            actual = item.aria_label or ""
            if insensitive:
                expected, actual = expected.lower(), actual.lower()
            if expected not in actual:
                return False

        class_match = self._contains_attr(outer_selector, "class")
        if class_match is not None:
            expected, insensitive = class_match
            actual = item.class_name
            if insensitive:
                expected, actual = expected.lower(), actual.lower()
            if expected not in actual:
                return False

        if ":has(svg" in selector:
            if not item.has_svg:
                return False
            svg_match = re.search(
                r"svg\[aria-label\*=['\"](.*?)['\"](\s+i)?\]",
                selector,
                re.IGNORECASE,
            )
            if svg_match is not None:
                expected = svg_match.group(1)
                actual = item.svg_aria_label or ""
                if svg_match.group(2):
                    expected, actual = expected.lower(), actual.lower()
                if expected not in actual:
                    return False
            shape_counts = {
                "circle": item.svg_circle_count,
                "rect": item.svg_rect_count,
                "path": item.svg_path_count,
            }
            for shape, count in shape_counts.items():
                if f"{shape}:nth-of-type(3)" in selector and count < 3:
                    return False
            use_match = re.search(
                r"use\[href\*=['\"](.*?)['\"](\s+i)?\]",
                selector,
                re.IGNORECASE,
            )
            if use_match is not None:
                expected = use_match.group(1)
                actual = item.svg_use_href or ""
                if use_match.group(2):
                    expected, actual = expected.lower(), actual.lower()
                if expected not in actual:
                    return False
            svg_class_match = re.search(
                r"svg\[class\*=['\"](.*?)['\"](\s+i)?\]",
                selector,
                re.IGNORECASE,
            )
            if svg_class_match is not None:
                expected = svg_class_match.group(1)
                actual = item.svg_class_name
                if svg_class_match.group(2):
                    expected, actual = expected.lower(), actual.lower()
                if expected not in actual:
                    return False
        return True

    def _matches_selector(self, item: _FakeElement) -> bool:
        return any(
            self._matches_selector_group(item, group)
            for group in self.selector.split(",")
        )

    def _items(self) -> list[_FakeElement]:
        if self.selector == "input[type='range']":
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
            items = [
                item
                for item in self.page.dom_elements
                if self._matches_selector(item)
            ]

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
        more_buttons: list[dict] | None = None,
        model_anchor: bool = False,
        menu_summary: bool = False,
        summary_trigger_opens: bool | None = None,
        duration_revert_on_reopen: int | None = None,
    ):
        self.url = "https://www.doubao.com/chat/create-image"
        self.has_trigger = has_trigger
        self.menu_opens = menu_opens
        self.summary_trigger_opens = (
            menu_opens
            if summary_trigger_opens is None
            else summary_trigger_opens
        )
        self.escape_closes = escape_closes
        self.escape_close_delay_ms = escape_close_delay_ms
        self.pending_escape_close_ms: int | None = None
        self.open_delays = list(open_delays or [])
        self.render_delay_ms = 0
        self.menu_open = False
        self.outer_menu_open = False
        self.pending_more_close_ms: int | None = None
        self.closing_more: _FakeElement | None = None
        self.ratio = ratio
        self.duration = duration
        self.duration_has_been_set = False
        self.duration_revert_on_reopen = duration_revert_on_reopen
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
        self.more_clicks: list[_FakeElement] = []
        self.summary_clicks = 0
        self.send_clicks = 0
        self.ratio_clicks: list[str | None] = []
        self.range_fills: list[str] = []
        self.slider_presses: list[str] = []
        self.slider_focused = False
        self.trigger = _FakeElement(
            self,
            "trigger",
            tag=self.trigger_tag,
            role="button" if self.trigger_role else None,
            box={"x": 260, "y": 100, "width": 90, "height": 32},
        )
        self.model_anchor = (
            _FakeElement(
                self,
                "other",
                "模型 Seedance 2.0 Mini",
                tag="button",
                box={"x": 160, "y": 100, "width": 90, "height": 32},
            )
            if model_anchor
            else None
        )
        self.more_elements = [
            _FakeElement(self, spec.pop("kind", "more"), **spec)
            for raw_spec in (more_buttons or [])
            for spec in [dict(raw_spec)]
        ]
        self.summary_element = (
            _FakeElement(
                self,
                "summary",
                tag="button",
                role="button",
                box={"x": 310, "y": 160, "width": 120, "height": 32},
            )
            if menu_summary
            else None
        )
        self.extra_buttons = [
            _FakeElement(self, "other", value, tag="button")
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

    @property
    def dom_elements(self) -> list[_FakeElement]:
        elements: list[_FakeElement] = []
        if self.has_trigger:
            elements.append(self.trigger)
        if self.model_anchor is not None:
            elements.append(self.model_anchor)
        elements.extend(self.more_elements)
        if self.summary_element is not None:
            elements.append(self.summary_element)
        elements.extend(self.extra_buttons)
        elements.extend(self.ratio_elements)
        return elements

    def locator(self, selector: str):
        return _FakeLocator(self, selector)

    def get_by_role(self, role: str):
        assert role == "button"
        return _FakeLocator(self, "role=button")

    def get_by_text(self, text):
        return _FakeLocator(self, "*").filter(has_text=text)

    async def evaluate(self, _expression):
        anchors = [
            item
            for item in self.dom_elements
            if re.search(r"模型|model|seedance", item._text(), re.IGNORECASE)
        ]
        diagnostics = []
        for item in self.dom_elements:
            if not item.has_svg:
                continue
            box = await item.bounding_box()
            nearest = None
            for anchor in anchors:
                anchor_box = await anchor.bounding_box()
                dx = round(box["x"] - (anchor_box["x"] + anchor_box["width"]))
                dy = round(
                    abs(
                        box["y"]
                        + box["height"] / 2
                        - (anchor_box["y"] + anchor_box["height"] / 2)
                    )
                )
                distance = (dx ** 2 + dy ** 2) ** 0.5
                if nearest is None or distance < nearest["distance"]:
                    nearest = {"dx": dx, "dy": dy, "distance": distance}
            diagnostics.append(
                {
                    "aria_label": item.aria_label,
                    "title": item.title,
                    "class_name": item.class_name,
                    "data_testid": item.data_testid,
                    "inner_text": item._text()[:40],
                    "svg_class": item.svg_class_name or None,
                    "svg_view_box": item.svg_view_box,
                    "circle_count": item.svg_circle_count,
                    "rect_count": item.svg_rect_count,
                    "path_count": item.svg_path_count,
                    "use_href": item.svg_use_href,
                    "bbox": {
                        key: round(box[key])
                        for key in ("x", "y", "width", "height")
                    },
                    "inside_send_wrapper": item.inside_send_wrapper,
                    "inside_tab": item.inside_tab,
                    "nearest_model_dx": nearest and nearest["dx"],
                    "nearest_model_dy": nearest and nearest["dy"],
                    "nearest_model_distance": (
                        round(nearest["distance"]) if nearest else None
                    ),
                }
            )
        diagnostics.sort(
            key=lambda item: (
                item["nearest_model_distance"]
                if item["nearest_model_distance"] is not None
                else float("inf"),
                int(item["inside_send_wrapper"] or item["inside_tab"]),
            )
        )
        return diagnostics[:20]

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
                self.outer_menu_open = False
                self.pending_escape_close_ms = None
        else:
            await asyncio.sleep(0)
        if self.pending_more_close_ms is not None:
            self.pending_more_close_ms = max(
                0,
                self.pending_more_close_ms - milliseconds,
            )
            if self.pending_more_close_ms == 0:
                self.menu_open = False
                self.outer_menu_open = False
                self.pending_more_close_ms = None
                self.closing_more = None
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


class _FakeTabElement:
    """Small DOM element used by the video-tab selector contract tests."""

    def __init__(self, page, *, text: str, tag: str = "div",
                 role: str | None = None, aria_label: str | None = None,
                 semi: bool = False, kind: str = "tab"):
        self.page = page
        self.text = text
        self.tag = tag
        self.role = role
        self.aria_label = aria_label
        self.semi = semi
        self.kind = kind

    async def is_visible(self) -> bool:
        if self.kind == "ratio":
            return self.page.ratio_options_mounted
        return True

    async def inner_text(self) -> str:
        return self.text

    async def get_attribute(self, name: str) -> str | None:
        if name == "role":
            return self.role
        if name == "aria-label":
            return self.aria_label
        return None

    async def click(self) -> None:
        if self.kind == "tab":
            self.page.tab_clicks += 1
            self.page.clicked_tabs.append(self.text)
            self.page.ratio_options_mounted = self.page.mounts_ratio_options


class _FakeTabLocator:
    def __init__(self, page, selector: str, text_filter=None):
        self.page = page
        self.selector = selector
        self.text_filter = text_filter

    @staticmethod
    def _selector_text(selector: str) -> str | None:
        match = re.search(r":has-text\((?:['\"])(.*?)(?:['\"])\)", selector)
        return match.group(1) if match else None

    def _matches_selector(self, item: _FakeTabElement) -> bool:
        selector = self.selector
        selector_text = self._selector_text(selector)
        if selector_text is not None and selector_text not in item.text:
            return False
        if "[aria-label*='视频']" in selector and "视频" not in (item.aria_label or ""):
            return False
        if selector.startswith(".semi-tabs-tab") and not item.semi:
            return False
        if "[role='tab']" in selector and item.role != "tab":
            return False
        if selector in {"[role='tab']", "role=tab"} and item.role != "tab":
            return False
        if selector in {"button, [role='button']", "[role='button'], button"}:
            if item.tag != "button" and item.role != "button":
                return False
        elif selector.startswith("button") and item.tag != "button":
            return False
        elif selector.startswith("[role='button']") and item.role != "button":
            return False
        return True

    def _items(self) -> list[_FakeTabElement]:
        items = self.page.all_elements
        selected = [item for item in items if self._matches_selector(item)]
        if self.text_filter is None:
            return selected
        if isinstance(self.text_filter, re.Pattern):
            return [item for item in selected if self.text_filter.search(item.text)]
        return [item for item in selected if str(self.text_filter) in item.text]

    def filter(self, *, has_text):
        return _FakeTabLocator(self.page, self.selector, has_text)

    async def count(self) -> int:
        return len(self._items())

    def nth(self, index: int):
        return self._items()[index]

    @property
    def first(self):
        return self.nth(0)

    async def all_text_contents(self) -> list[str]:
        return [item.text for item in self._items()]


class _FakeTabPage:
    """Fake Playwright page for `_click_video_tab` selector/validation tests.

    Ratio controls become visible after a successful tab click. This models
    the contract requested by the production helper; real-page menu visibility
    remains a separate integration concern.
    """

    def __init__(
        self,
        *,
        tabs: list[_FakeTabElement] | None = None,
        buttons: list[_FakeTabElement] | None = None,
        ratio_options: list[str] | None = None,
        mounts_ratio_options: bool = True,
        url: str = "https://www.doubao.com/chat/create-image",
    ):
        self.url = url
        self.tab_clicks = 0
        self.clicked_tabs: list[str] = []
        self.mounts_ratio_options = mounts_ratio_options
        self.ratio_options_mounted = False
        self.tabs = tabs or []
        self.buttons = buttons or []
        for item in [*self.tabs, *self.buttons]:
            item.page = self
        self.ratio_elements = [
            _FakeTabElement(self, text=value, tag="button", kind="ratio")
            for value in (ratio_options or ["自动", "3:4", "4:3", "9:16"])
        ]

    @property
    def all_elements(self) -> list[_FakeTabElement]:
        return [*self.tabs, *self.buttons, *self.ratio_elements]

    def locator(self, selector: str):
        return _FakeTabLocator(self, selector)

    def get_by_role(self, role: str, name=None):
        if role == "tab":
            locator = _FakeTabLocator(self, "role=tab")
            return locator.filter(has_text=name) if name is not None else locator
        if role == "button":
            # Playwright's role locator includes native buttons and divs with
            # role=button; ratio options are included only after mount.
            locator = _FakeTabLocator(self, "button, [role='button']")
            return locator.filter(has_text=name) if name is not None else locator
        raise AssertionError(role)

    async def wait_for_timeout(self, _milliseconds: int):
        return None


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


@pytest.mark.asyncio
async def test_find_video_options_trigger_prefers_summary_over_more_button():
    """类型 A 和 B 同时存在时，原组合按钮必须保持最高优先级。"""
    page = _FakePage(
        model_anchor=True,
        more_buttons=[
            {
                "value": "⋯",
                "tag": "button",
                "adjacent_to_model": True,
                "action": "toggle_menu",
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    trigger, kind = await _find_video_options_trigger(page, return_kind=True)

    assert trigger is page.trigger
    assert kind == "A"
    assert page.more_clicks == []


@pytest.mark.asyncio
async def test_find_video_options_trigger_accepts_chinese_more_aria_label():
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        more_buttons=[
            {
                "value": "",
                "tag": "div",
                "role": "button",
                "aria_label": "更多选项",
                "action": "toggle_menu",
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    trigger, kind = await _find_video_options_trigger(page, return_kind=True)

    assert trigger is page.more_elements[0]
    assert kind == "B"


@pytest.mark.asyncio
@pytest.mark.parametrize("aria_label", ["More options", "Video options"])
async def test_find_video_options_trigger_accepts_english_more_or_options_label(
    aria_label,
):
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        more_buttons=[
            {
                "value": "",
                "tag": "button",
                "aria_label": aria_label,
                "action": "toggle_menu",
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    trigger, kind = await _find_video_options_trigger(page, return_kind=True)

    assert trigger is page.more_elements[0]
    assert kind == "B"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "more_spec",
    [
        {
            "value": "",
            "tag": "button",
            "svg_aria_label": "更多",
        },
        {
            "value": "⋯",
            "tag": "button",
            "class_name": "semi-button",
        },
        {
            "value": "…",
            "tag": "div",
            "role": "button",
            "class_name": "toolbar-ellipsis",
        },
    ],
    ids=["nested-svg", "ellipsis-text", "ellipsis-class"],
)
async def test_find_video_options_trigger_accepts_svg_and_ellipsis_variants(
    more_spec,
):
    spec = {
        **more_spec,
        "action": "toggle_menu",
        "box": {"x": 260, "y": 100, "width": 32, "height": 32},
    }
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        more_buttons=[spec],
    )

    trigger, kind = await _find_video_options_trigger(page, return_kind=True)

    assert trigger is page.more_elements[0]
    assert kind == "B"


@pytest.mark.asyncio
async def test_find_video_options_trigger_excludes_send_wrapper_candidate():
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        more_buttons=[
            {
                "kind": "send",
                "value": "⋯",
                "tag": "button",
                "aria_label": "更多选项",
                "inside_send_wrapper": True,
                "adjacent_to_model": True,
                "box": {"x": 255, "y": 100, "width": 32, "height": 32},
            },
            {
                "value": "⋯",
                "tag": "button",
                "class_name": "toolbar-ellipsis",
                "action": "toggle_menu",
                "box": {"x": 300, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    trigger, kind = await _find_video_options_trigger(page, return_kind=True)

    assert trigger is page.more_elements[1]
    assert kind == "B"
    assert page.send_clicks == 0


@pytest.mark.asyncio
async def test_find_video_options_trigger_prefers_target_over_earlier_global_more():
    """DOM 中页头“更多”在前，也必须选择模型按钮旁的视频入口。"""
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        more_buttons=[
            {
                "value": "⋯",
                "tag": "button",
                "aria_label": "更多",
                "action": "none",
                "box": {"x": 900, "y": 20, "width": 32, "height": 32},
            },
            {
                "value": "⋯",
                "tag": "button",
                "aria_label": "更多选项",
                "adjacent_to_model": True,
                "action": "toggle_menu",
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    trigger, kind = await _find_video_options_trigger(page, return_kind=True)

    assert trigger is page.more_elements[1]
    assert kind == "B"


@pytest.mark.asyncio
async def test_find_video_options_trigger_finds_anonymous_svg_near_model():
    """无文字、无 a11y/class、非直接兄弟的 SVG 也能靠受限几何命中。"""
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        more_buttons=[
            {
                "value": "",
                "tag": "button",
                "has_svg": True,
                "svg_circle_count": 3,
                "action": "toggle_menu",
                "box": {"x": 270, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    trigger, kind = await _find_video_options_trigger(page, return_kind=True)

    assert trigger is page.more_elements[0]
    assert kind == "B"
    assert trigger.adjacent_to_model is False


@pytest.mark.asyncio
async def test_anonymous_svg_finder_rejects_distractors_and_picks_three_dot():
    """模型自身、页头、发送、TAB、近邻麦克风都不能抢走三点入口。"""
    page = _FakePage(
        has_trigger=False,
        more_buttons=[
            {
                "value": "模型 Seedance 2.0 Mini",
                "tag": "button",
                "has_svg": True,
                "svg_path_count": 1,
                "box": {"x": 160, "y": 100, "width": 90, "height": 32},
            },
            {
                "value": "",
                "tag": "button",
                "has_svg": True,
                "svg_circle_count": 3,
                "box": {"x": 900, "y": 20, "width": 32, "height": 32},
            },
            {
                "kind": "send",
                "value": "",
                "tag": "button",
                "has_svg": True,
                "svg_path_count": 1,
                "inside_send_wrapper": True,
                "box": {"x": 252, "y": 100, "width": 32, "height": 32},
            },
            {
                "value": "",
                "tag": "button",
                "has_svg": True,
                "svg_path_count": 1,
                "inside_tab": True,
                "box": {"x": 258, "y": 100, "width": 32, "height": 32},
            },
            {
                "value": "",
                "tag": "button",
                "has_svg": True,
                "svg_path_count": 3,
                "adjacent_to_model": True,
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
            {
                "value": "",
                "tag": "button",
                "has_svg": True,
                "svg_circle_count": 3,
                "action": "toggle_menu",
                "box": {"x": 270, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    trigger, kind = await _find_video_options_trigger(page, return_kind=True)

    target = page.more_elements[5]
    assert trigger is target
    assert kind == "B"
    assert page.send_clicks == 0
    assert all(
        element.click_attempts == 0
        for element in page.more_elements
        if element is not target
    )


@pytest.mark.asyncio
async def test_anonymous_svg_finder_requires_model_anchor():
    page = _FakePage(
        has_trigger=False,
        model_anchor=False,
        more_buttons=[
            {
                "value": "",
                "tag": "button",
                "has_svg": True,
                "svg_circle_count": 3,
                "box": {"x": 270, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    assert await _find_video_options_trigger(page, return_kind=True) is None


@pytest.mark.asyncio
async def test_type_a_summary_still_precedes_anonymous_svg_candidate():
    page = _FakePage(
        has_trigger=True,
        model_anchor=True,
        more_buttons=[
            {
                "value": "",
                "tag": "button",
                "has_svg": True,
                "svg_circle_count": 3,
                "action": "toggle_menu",
                "box": {"x": 270, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    trigger, kind = await _find_video_options_trigger(page, return_kind=True)

    assert trigger is page.trigger
    assert kind == "A"
    assert page.more_elements[0].click_attempts == 0


@pytest.mark.asyncio
async def test_open_and_validate_support_anonymous_svg_three_dot():
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        more_buttons=[
            {
                "value": "",
                "tag": "button",
                "has_svg": True,
                "svg_circle_count": 3,
                "action": "toggle_menu",
                "box": {"x": 270, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    trigger, visible_options, kind = await _open_video_options(page)

    assert trigger is page.more_elements[0]
    assert kind == "B"
    assert len(visible_options) >= 4
    assert page.more_clicks == [trigger]

    await _close_video_options(page, trigger, trigger_kind=kind)
    visible_options = await _validate_video_tab_content(page)
    assert len(visible_options) >= 4
    assert page.menu_open is False


@pytest.mark.asyncio
async def test_missing_svg_trigger_logs_complete_geometry_diagnostics(caplog):
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        more_buttons=[
            {
                "value": "",
                "tag": "button",
                "has_svg": True,
                "svg_circle_count": 3,
                "svg_path_count": 2,
                "svg_view_box": "0 0 24 24",
                "data_testid": "far-svg-sentinel",
                "box": {"x": 700, "y": 20, "width": 32, "height": 32},
            },
        ],
    )

    diagnostics = await _diagnose_three_dot_candidates(page)
    assert diagnostics == [
        {
            "aria_label": None,
            "title": None,
            "class_name": "",
            "data_testid": "far-svg-sentinel",
            "inner_text": "",
            "svg_class": None,
            "svg_view_box": "0 0 24 24",
            "circle_count": 3,
            "rect_count": 0,
            "path_count": 2,
            "use_href": None,
            "bbox": {"x": 700, "y": 20, "width": 32, "height": 32},
            "inside_send_wrapper": False,
            "inside_tab": False,
            "nearest_model_dx": 450,
            "nearest_model_dy": 80,
            "nearest_model_distance": 457,
        }
    ]

    with caplog.at_level(logging.WARNING, logger="doupool.video.browser"):
        with pytest.raises(RuntimeError, match="视频参数按钮未找到"):
            await _open_video_options(page)

    messages = [
        record.getMessage()
        for record in caplog.records
        if "event=video_options_trigger_not_found" in record.getMessage()
    ]
    assert messages
    assert "three_dot_candidates=" in messages[-1]
    assert "'data_testid': 'far-svg-sentinel'" in messages[-1]
    assert "'circle_count': 3" in messages[-1]
    assert "'path_count': 2" in messages[-1]
    assert "'bbox': {'x': 700, 'y': 20, 'width': 32, 'height': 32}" in messages[-1]


@pytest.mark.asyncio
async def test_svg_diagnostics_keep_near_model_candidate_after_twenty_globals():
    globals_before_target = [
        {
            "value": "",
            "tag": "button",
            "has_svg": True,
            "svg_path_count": 1,
            "data_testid": f"global-{index}",
            "box": {
                "x": 700 + index,
                "y": 20,
                "width": 32,
                "height": 32,
            },
        }
        for index in range(21)
    ]
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        more_buttons=[
            *globals_before_target,
            {
                "value": "",
                "tag": "button",
                "has_svg": True,
                "svg_circle_count": 3,
                "data_testid": "near-model-target",
                "box": {"x": 270, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    diagnostics = await _diagnose_three_dot_candidates(page)

    assert len(diagnostics) == 20
    assert any(
        item["data_testid"] == "near-model-target" for item in diagnostics
    )


@pytest.mark.asyncio
async def test_open_video_options_clicks_more_and_waits_for_ratio_options():
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        more_buttons=[
            {
                "value": "⋯",
                "tag": "button",
                "adjacent_to_model": True,
                "action": "toggle_menu",
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    trigger, visible_options, kind = await _open_video_options(page)

    assert trigger is page.more_elements[0]
    assert kind == "B"
    assert len(visible_options) >= 4
    assert page.more_clicks == [trigger]
    assert page.menu_open is True


@pytest.mark.asyncio
async def test_open_video_options_supports_two_stage_more_then_summary_menu():
    """截图所示灰度页可能先开三点外层，再点“自动 · 10s”摘要。"""
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        menu_summary=True,
        more_buttons=[
            {
                "value": "⋯",
                "tag": "button",
                "adjacent_to_model": True,
                "action": "toggle_outer",
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    trigger, visible_options, kind = await _open_video_options(page)

    assert trigger is page.more_elements[0]
    assert kind == "B"
    assert len(visible_options) >= 4
    assert page.more_clicks == [trigger]
    assert page.summary_clicks == 1
    assert page.menu_open is True


@pytest.mark.asyncio
async def test_apply_video_options_type_b_accepts_verified_controls_without_summary():
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        duration=9,
        more_buttons=[
            {
                "value": "⋯",
                "tag": "button",
                "adjacent_to_model": True,
                "action": "toggle_menu",
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    await _apply_video_options(page, ratio="1:1", duration=5)

    assert page.ratio == "1:1"
    assert page.duration == 5
    assert page.ratio_clicks == ["1:1"]
    assert page.range_fills == ["5"]
    assert len(page.more_clicks) == 4
    assert all(clicked is page.more_elements[0] for clicked in page.more_clicks)
    assert page.menu_open is False


@pytest.mark.asyncio
async def test_apply_video_options_type_b_reads_back_two_stage_summary():
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        menu_summary=True,
        duration=9,
        more_buttons=[
            {
                "value": "⋯",
                "tag": "button",
                "adjacent_to_model": True,
                "action": "toggle_outer",
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    await _apply_video_options(page, ratio="1:1", duration=5)

    assert page.ratio == "1:1"
    assert page.duration == 5
    assert len(page.more_clicks) == 4
    assert all(clicked is page.more_elements[0] for clicked in page.more_clicks)
    assert page.summary_clicks == 1
    assert page.menu_open is False
    assert page.outer_menu_open is False


@pytest.mark.asyncio
async def test_close_type_b_uses_original_more_toggle_when_escape_fails():
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        escape_closes=False,
        duration=9,
        more_buttons=[
            {
                "value": "⋯",
                "tag": "button",
                "adjacent_to_model": True,
                "action": "toggle_menu",
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    await _apply_video_options(page, ratio="1:1", duration=5)

    target = page.more_elements[0]
    assert len(page.more_clicks) == 4
    assert all(clicked is target for clicked in page.more_clicks)
    assert page.trigger_clicks == 0
    assert page.menu_open is False


@pytest.mark.asyncio
async def test_validate_video_tab_content_supports_two_stage_type_b_menu():
    """TAB 校验也必须走通“三点 → 摘要 → 比例”两级结构。"""
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        menu_summary=True,
        more_buttons=[
            {
                "value": "⋯",
                "tag": "button",
                "adjacent_to_model": True,
                "action": "toggle_outer",
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    visible_options = await _validate_video_tab_content(page)

    root = page.more_elements[0]
    assert len(visible_options) >= 4
    assert page.more_clicks == [root, root]
    assert page.summary_clicks == 1
    assert page.menu_open is False
    assert page.outer_menu_open is False


@pytest.mark.asyncio
async def test_open_video_options_retries_same_type_b_after_cold_render_delay():
    """首开渲染 1.1s 超过单次等待时，同一个正确候选应再试一次。"""
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        more_buttons=[
            {
                "value": "⋯",
                "tag": "button",
                "adjacent_to_model": True,
                "action": "toggle_menu",
                "open_delays": [1_100, 0],
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    trigger, visible_options, kind = await _open_video_options(page)

    root = page.more_elements[0]
    assert trigger is root
    assert kind == "B"
    assert len(visible_options) >= 4
    # 首开 + 首次失败后的关闭 + 同一 root 重开。
    assert page.more_clicks == [root, root, root]


@pytest.mark.asyncio
async def test_open_waits_for_previous_ratio_exit_before_trying_global_more():
    """旧 chips 退场期间不能让下一个全局“更多”借尸还魂为成功候选。"""
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        more_buttons=[
            {
                "value": "⋯",
                "tag": "button",
                "adjacent_to_model": True,
                "action": "toggle_menu",
                "open_delays": [1_100, 1_100],
                "close_delay_ms": 800,
                "box": {"x": 255, "y": 100, "width": 32, "height": 32},
            },
            {
                "value": "⋯",
                "tag": "button",
                "aria_label": "页面顶部更多",
                "action": "none",
                "box": {"x": 270, "y": 20, "width": 32, "height": 32},
            },
            {
                "value": "⋯",
                "tag": "button",
                "aria_label": "视频更多选项",
                "action": "toggle_menu",
                "box": {"x": 450, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    trigger, visible_options, kind = await _open_video_options(page)

    target = page.more_elements[2]
    global_more = page.more_elements[1]
    assert trigger is target
    assert trigger is not global_more
    assert kind == "B"
    assert len(visible_options) >= 4


@pytest.mark.asyncio
async def test_open_falls_back_to_type_b_after_summary_fails_twice():
    page = _FakePage(
        has_trigger=True,
        summary_trigger_opens=False,
        model_anchor=True,
        more_buttons=[
            {
                "value": "⋯",
                "tag": "button",
                "adjacent_to_model": True,
                "action": "toggle_menu",
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    trigger, visible_options, kind = await _open_video_options(page)

    assert trigger is page.more_elements[0]
    assert kind == "B"
    assert len(visible_options) >= 4
    assert page.trigger_clicks == 2
    assert page.more_clicks == [page.more_elements[0]]


@pytest.mark.asyncio
async def test_type_b_ratio_auto_close_reuses_root_more_for_reopen_and_readback():
    """比例点击收起内层后，duration 重开、关闭和回读都不能换成摘要/全局按钮。"""
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        close_menu_on_ratio_click=True,
        duration=9,
        more_buttons=[
            {
                "value": "⋯",
                "tag": "button",
                "adjacent_to_model": True,
                "action": "toggle_menu",
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
            {
                "value": "⋯",
                "tag": "button",
                "aria_label": "页面顶部更多",
                "action": "none",
                "box": {"x": 900, "y": 20, "width": 32, "height": 32},
            },
        ],
    )

    await _apply_video_options(page, ratio="1:1", duration=5)

    root = page.more_elements[0]
    assert page.ratio == "1:1"
    assert page.duration == 5
    # 正常完整 B apply 为 4 次 root toggle；本例比例点击额外收起，
    # duration 阶段需再重开一次，所以合计 5 次。
    assert len(page.more_clicks) == 5
    assert all(clicked is root for clicked in page.more_clicks)
    assert page.menu_open is False
    assert page.outer_menu_open is False


@pytest.mark.asyncio
async def test_type_b_controls_readback_rejects_duration_rollback(caplog):
    """setter 当场成功也不够；关闭后重开读到回滚值必须主动失败。"""
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        duration=9,
        duration_revert_on_reopen=9,
        more_buttons=[
            {
                "value": "⋯",
                "tag": "button",
                "adjacent_to_model": True,
                "action": "toggle_menu",
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
        ],
    )

    with caplog.at_level(logging.WARNING, logger="doupool.video.browser"):
        with pytest.raises(RuntimeError, match="视频参数设置后校验失败"):
            await _apply_video_options(page, ratio="1:1", duration=5)

    root = page.more_elements[0]
    assert page.range_fills == ["5"]
    assert page.duration == 9
    assert len(page.more_clicks) == 4
    assert all(clicked is root for clicked in page.more_clicks)
    assert "event=video_options_readback_failed" in caplog.text
    assert "actual_duration=9" in caplog.text
    assert page.menu_open is False


@pytest.mark.asyncio
async def test_close_type_b_uses_escape_when_original_and_refind_click_fail():
    page = _FakePage(
        has_trigger=False,
        model_anchor=True,
        more_buttons=[
            {
                "value": "⋯",
                "tag": "button",
                "adjacent_to_model": True,
                "action": "toggle_menu",
                "click_error": "refind click failed",
                "box": {"x": 260, "y": 100, "width": 32, "height": 32},
            },
        ],
    )
    page.menu_open = True
    page.outer_menu_open = True
    original = _FakeElement(
        page,
        "more",
        "⋯",
        tag="button",
        action="toggle_menu",
        click_error="original click failed",
        box={"x": 260, "y": 100, "width": 32, "height": 32},
    )

    await _close_video_options(page, original, trigger_kind="B")

    refound = page.more_elements[0]
    assert original.click_attempts == 1
    assert refound.click_attempts == 1
    assert page.keyboard.presses == ["Escape"]
    assert page.menu_open is False
    assert page.outer_menu_open is False


def _tab(*, text: str, tag: str = "div", role: str | None = None,
         aria_label: str | None = None, semi: bool = False) -> _FakeTabElement:
    """Construct a tab/button before its fake page is available."""
    return _FakeTabElement(
        None,
        text=text,
        tag=tag,
        role=role,
        aria_label=aria_label,
        semi=semi,
    )


@pytest.mark.asyncio
async def test_click_video_tab_accepts_role_tab_with_nested_text():
    """A role tab may say ``视频生成`` and contain nested span text."""
    page = _FakeTabPage(
        tabs=[_tab(text="视频生成", role="tab")],
        ratio_options=["3:4", "4:3", "9:16", "16:9"],
    )

    await _click_video_tab(page)

    assert page.tab_clicks >= 1
    assert page.clicked_tabs == ["视频生成"]
    assert page.ratio_options_mounted is True


@pytest.mark.asyncio
async def test_click_video_tab_falls_back_to_native_button_without_role_tab():
    """Newer markup may expose the video tab as a plain button."""
    page = _FakeTabPage(
        buttons=[_tab(text="视频生成", tag="button")],
        ratio_options=["3:4", "4:3", "9:16", "16:9"],
    )

    await _click_video_tab(page)

    assert page.tab_clicks >= 1
    assert page.clicked_tabs == ["视频生成"]
    assert page.ratio_options_mounted is True


@pytest.mark.asyncio
async def test_click_video_tab_fails_loudly_and_logs_visible_tabs(caplog):
    """A missing video tab must stop before prompt paste/send."""
    page = _FakeTabPage(
        tabs=[_tab(text="图像", role="tab"), _tab(text="AI 创作", role="tab")],
        buttons=[_tab(text="发送", tag="button")],
    )

    with caplog.at_level(logging.INFO, logger="doupool.video.browser"):
        with pytest.raises(RuntimeError, match="视频 TAB 未切换") as exc_info:
            await _click_video_tab(page)

    message = str(exc_info.value)
    assert page.url in message
    assert "图像" in message
    assert "AI 创作" in message
    assert page.tab_clicks == 0
    assert page.url in caplog.text
    assert "图像" in caplog.text


@pytest.mark.asyncio
async def test_click_video_tab_keeps_legacy_role_tab_path():
    """The original [role=tab] text=视频 markup remains supported."""
    page = _FakeTabPage(
        tabs=[_tab(text="视频", role="tab")],
        ratio_options=["3:4", "4:3", "9:16", "16:9"],
    )

    await _click_video_tab(page)

    assert page.clicked_tabs == ["视频"]
    assert page.ratio_options_mounted is True


@pytest.mark.asyncio
async def test_click_video_tab_rejects_unmounted_video_content():
    """Clicking a tab is insufficient when its video controls never mount."""
    page = _FakeTabPage(
        tabs=[_tab(text="视频", role="tab")],
        ratio_options=["3:4", "16:9"],
        mounts_ratio_options=False,
    )

    with pytest.raises(RuntimeError, match="视频 TAB 未切换"):
        await _click_video_tab(page)

    assert page.tab_clicks >= 1
    assert page.ratio_options_mounted is False
