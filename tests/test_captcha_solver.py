"""tests/test_captcha_solver.py

solver 的单测 —— 不真起 Playwright,用 FakePage / FakeMouse 替身。
覆盖:
  - human_like_drag:起点终点交换 / 距离太短 / 抖动方向
  - _parse_points_str 已经被 ttshitu_client 测试覆盖,这里不复测
  - solve_aegis_captcha:成功路径 / 失败重试 / 凭证 disable
  - _captcha_gone:DOM 探测
"""
from __future__ import annotations

import asyncio
import base64

import pytest

from doupool.captcha.solver import (
    AegisCaptchaDisabled,
    AegisCaptchaFailed,
    CaptchaKind,
    detect_aegis_captcha,
    human_like_drag,
    make_client,
    _captcha_gone,
    _find_element_box,
)
from doupool.captcha.config import CaptchaCredentials
from doupool.captcha.ttshitu_client import (
    TtshituSolve,
    TYPEID_COORDINATE_1_4,
    TYPEID_SINGLE_GAP,
    TtshituCaptchaClient as _RealClient,
)


def _parse_points_str(s: str):
    return _RealClient._parse_points_str(s)


# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class FakeMouse:
    def __init__(self):
        self.events: list[tuple[str, tuple[float, float], int]] = []

    async def move(self, x: float, y: float, steps: int = 1) -> None:  # noqa: ARG002
        self.events.append(("move", (x, y), steps))

    async def down(self) -> None:
        self.events.append(("down", (0.0, 0.0), 0))

    async def up(self) -> None:
        self.events.append(("up", (0.0, 0.0), 0))


class FakeLocator:
    def __init__(self, count: int = 1, visible: bool = True, box: dict | None = None):
        self._count = count
        self._visible = visible
        self._box = box

    @property
    def first(self) -> "FakeLocator":
        return self

    async def count(self) -> int:
        return self._count

    async def is_visible(self) -> bool:
        return self._count > 0 and self._visible

    async def wait_for(self, state: str, timeout: float = 0) -> None:
        if self._count == 0 or not self._visible:
            raise RuntimeError("not visible")

    async def bounding_box(self) -> dict | None:
        return self._box

    async def screenshot(self) -> bytes:
        return b"png"

    def set_visible(self, v: bool) -> None:
        """测试用:模拟弹窗关闭。"""
        self._visible = v
        if not v:
            self._count = 0


class FakeFrame:
    def __init__(self, content: str = "", is_main: bool = False):
        self.content_str = content
        self._main = is_main

    @property
    def is_main(self) -> bool:
        return self._main

    async def content(self) -> str:
        return self.content_str


class FakePage:
    def __init__(
        self,
        *,
        mouse: FakeMouse | None = None,
        frames: list[FakeFrame] | None = None,
        content: str = "",
        locators: dict[str, FakeLocator] | None = None,
    ):
        self.mouse = mouse or FakeMouse()
        self._all_frames = frames or []
        self._content = content
        self._locators = locators or {}

    @property
    def frames(self) -> list[FakeFrame]:
        # solver 先遍历非 main frame(safe_inner_html 跳过 main_frame),再退回 page.content()
        return [f for f in self._all_frames if not f.is_main]

    @property
    def main_frame(self) -> FakeFrame | None:
        for f in self._all_frames:
            if f.is_main:
                return f
        return None

    async def content(self) -> str:
        return self._content

    def locator(self, sel: str) -> FakeLocator:
        return self._locators.get(sel, FakeLocator(count=0, visible=False))

    async def screenshot(self) -> bytes:
        return b"png"


# ---------------------------------------------------------------------------
# human_like_drag
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_like_drag_basic_moves():
    mouse = FakeMouse()
    page = FakePage(mouse=mouse)
    await human_like_drag(page, start_xy=(100, 200), end_xy=(400, 200), steps=20)
    # mouse.down() / up() 必须各调一次
    kinds = [e[0] for e in mouse.events]
    assert kinds[0] in ("move", "down")  # 通常是先 down
    assert "down" in kinds
    assert "up" in kinds
    # 至少 steps+1 次 move (起点 → 终点)
    moves = [e for e in mouse.events if e[0] == "move"]
    assert len(moves) >= 20


@pytest.mark.asyncio
async def test_human_like_drag_short_distance_skips_bezier():
    mouse = FakeMouse()
    page = FakePage(mouse=mouse)
    await human_like_drag(page, start_xy=(100, 100), end_xy=(105, 102), steps=8)
    moves = [e for e in mouse.events if e[0] == "move"]
    assert len(moves) >= 1


@pytest.mark.asyncio
async def test_human_like_drag_min_steps_enforced():
    """steps=2 也要至少 8 步,避免鼠标瞬移。"""
    mouse = FakeMouse()
    page = FakePage(mouse=mouse)
    await human_like_drag(page, start_xy=(0, 0), end_xy=(500, 0), steps=2)
    moves = [e for e in mouse.events if e[0] == "move"]
    assert len(moves) >= 8


@pytest.mark.asyncio
async def test_human_like_drag_zero_distance_handled():
    """start == end,不能除零。"""
    mouse = FakeMouse()
    page = FakePage(mouse=mouse)
    await human_like_drag(page, start_xy=(100, 100), end_xy=(100, 100))
    # 不崩就行


@pytest.mark.asyncio
async def test_human_like_drag_pure_horizontal():
    """dy=0 不能让 perpendicular 算成 (0,0),要 fallback 到 (0,1)。"""
    mouse = FakeMouse()
    page = FakePage(mouse=mouse)
    await human_like_drag(page, start_xy=(0, 100), end_xy=(300, 100), steps=12)
    assert any(e[0] == "down" for e in mouse.events)


# ---------------------------------------------------------------------------
# detect_aegis_captcha
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_detect_drag_shape():
    page = FakePage(content="<html>请拖动下方图片</html>")
    kind = await detect_aegis_captcha(page)
    assert kind == CaptchaKind.DRAG_SHAPE


@pytest.mark.asyncio
async def test_detect_slide_puzzle():
    page = FakePage(content="<html>向右拖动滑块完成拼图</html>")
    kind = await detect_aegis_captcha(page)
    assert kind == CaptchaKind.SLIDE_PUZZLE


@pytest.mark.asyncio
async def test_detect_none():
    page = FakePage(content="<html>普通聊天界面</html>")
    kind = await detect_aegis_captcha(page)
    assert kind == CaptchaKind.UNKNOWN


@pytest.mark.asyncio
async def test_detect_prefers_iframe_with_phrase():
    page = FakePage(
        frames=[
            FakeFrame(content="<html>无验证</html>", is_main=True),
            FakeFrame(content="<html>请拖动</html>", is_main=False),
        ],
    )
    kind = await detect_aegis_captcha(page)
    assert kind == CaptchaKind.DRAG_SHAPE


# ---------------------------------------------------------------------------
# _find_element_box
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_find_element_box_picks_first_visible():
    page = FakePage(locators={
        "[class*='aegis'][class*='target']": FakeLocator(box={"x": 10, "y": 20, "width": 100, "height": 80}, visible=True),
        "[class*='aegis'][class*='shape']": FakeLocator(box={"x": 50, "y": 60, "width": 30, "height": 30}, visible=True),
    })
    box = await _find_element_box(page, ("[class*='aegis'][class*='target']", "[class*='aegis'][class*='shape']"))
    assert box == (10.0, 20.0, 100.0, 80.0)


@pytest.mark.asyncio
async def test_find_element_box_none_when_missing():
    page = FakePage()
    box = await _find_element_box(page, ("[class*='nope']",))
    assert box is None


@pytest.mark.asyncio
async def test_find_element_box_skips_invisible():
    page = FakePage(locators={
        "[class*='aegis'][class*='target']": FakeLocator(box={"x": 1, "y": 2, "width": 3, "height": 4}, visible=False),
        "[class*='aegis'][class*='shape']": FakeLocator(box={"x": 50, "y": 60, "width": 30, "height": 30}, visible=True),
    })
    box = await _find_element_box(page, ("[class*='aegis'][class*='target']", "[class*='aegis'][class*='shape']"))
    assert box == (50.0, 60.0, 30.0, 30.0)


# ---------------------------------------------------------------------------
# _captcha_gone
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_captcha_gone_when_no_locators():
    page = FakePage()
    assert await _captcha_gone(page) is True


@pytest.mark.asyncio
async def test_captcha_gone_still_visible():
    page = FakePage(locators={
        "[class*='aegis'][class*='dialog']": FakeLocator(count=1, visible=True),
    })
    assert await _captcha_gone(page) is False


# ---------------------------------------------------------------------------
# make_client
# ---------------------------------------------------------------------------


def test_make_client_usable():
    creds = CaptchaCredentials("u", "p", enabled=True)
    client = make_client(creds)
    assert client is not None


def test_make_client_disabled_raises():
    with pytest.raises(AegisCaptchaDisabled):
        make_client(CaptchaCredentials("", "", enabled=False))
    with pytest.raises(AegisCaptchaDisabled):
        make_client(None)


# ---------------------------------------------------------------------------
# _parse_points_str (跨模块的辅助函数,顺便测一下)
# ---------------------------------------------------------------------------


def test_parse_points_str_variants():
    assert _parse_points_str("100,200") == [(100, 200)]
    assert _parse_points_str("100,200|300,400") == [(100, 200), (300, 400)]
    assert _parse_points_str(" 100, 200 | 300 ,400 ") == [(100, 200), (300, 400)]
    assert _parse_points_str("") == []
    assert _parse_points_str("|100,200") == [(100, 200)]  # 空 token 跳过
    assert _parse_points_str("abc") == []
    assert _parse_points_str("100") == []  # 单值无逗号


# ---------------------------------------------------------------------------
# solve_aegis_captcha 流程 —— mock 整个 client
# ---------------------------------------------------------------------------


class FakeTtshituClient:
    """mock 图鉴 client:可控成功 / 失败次数。"""

    def __init__(self, *, results: list[TtshituSolve | Exception]):
        self._results = list(results)
        self.calls: list[int] = []
        self.closed = False

    def solve_image(self, png: bytes, *, typeid: int) -> TtshituSolve:
        self.calls.append(typeid)
        if not self._results:
            raise RuntimeError("no more results configured")
        nxt = self._results.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    def close(self) -> None:
        self.closed = True


def _ok_solve(typeid: int = TYPEID_COORDINATE_1_4) -> TtshituSolve:
    return TtshituSolve(
        points=[(120, 45)],
        cost_ms=42,
        raw={"code": 0, "data": {"result": "120,45"}},
        typeid=typeid,
    )


@pytest.mark.asyncio
async def test_solve_aegis_captcha_happy_path():
    """截图 → typeid 27 → 拟人拖拽 → DOM 弹窗消失 → 返 solve。"""
    handle_box = {"x": 50, "y": 400, "width": 60, "height": 60}
    target_box = {"x": 200, "y": 100, "width": 600, "height": 400}
    aegis_dialog = FakeLocator(count=1, visible=True)
    verify_div = FakeLocator(count=1, visible=True)
    captcha_div = FakeLocator(count=1, visible=True)
    puzzle_div = FakeLocator(count=1, visible=True)

    def fake_on_state(s: str) -> None:
        states.append(s)
        # 模拟「aegis 校验完成后弹窗消失」
        if s == "verifying":
            aegis_dialog.set_visible(False)
            verify_div.set_visible(False)
            captcha_div.set_visible(False)
            puzzle_div.set_visible(False)

    page = FakePage(
        locators={
            "[class*='aegis'] [class*='slider']": FakeLocator(box=handle_box, visible=True),
            "[class*='aegis'] [class*='target']": FakeLocator(box=target_box, visible=True),
            "[class*='aegis'][class*='dialog']": aegis_dialog,
            "[class*='verify']": verify_div,
            "[class*='captcha']": captcha_div,
            "[class*='puzzle']": puzzle_div,
        },
    )
    client = FakeTtshituClient(results=[_ok_solve()])

    from doupool.captcha.solver import solve_aegis_captcha
    states: list[str] = []
    solve = await solve_aegis_captcha(page, client, on_state=fake_on_state)
    assert len(client.calls) == 1
    assert client.calls[0] == TYPEID_COORDINATE_1_4
    assert solve.points == [(120, 45)]
    assert "ok" in states
    assert any(e[0] == "down" for e in page.mouse.events)


@pytest.mark.asyncio
async def test_solve_aegis_captcha_falls_back_to_typeid_33():
    """第 2 次失败 → 第 2 次用 typeid 33(typeid 顺序敏感)。"""
    from doupool.captcha.ttshitu_client import TtshituError
    client = FakeTtshituClient(results=[
        TtshituError("network: timeout", typeid=TYPEID_COORDINATE_1_4),
        _ok_solve(typeid=TYPEID_SINGLE_GAP),
    ])
    # 弹窗消失时机太复杂,这里只断言 typeid 顺序
    page = FakePage(locators={
        "[class*='aegis'][class*='slider']": FakeLocator(box={"x": 0, "y": 0, "width": 50, "height": 50}),
        "[class*='aegis'][class*='dialog']": FakeLocator(count=1, visible=True),
        "[class*='verify']": FakeLocator(count=1, visible=True),
        "[class*='captcha']": FakeLocator(count=1, visible=True),
        "[class*='puzzle']": FakeLocator(count=1, visible=True),
    })
    from doupool.captcha.solver import solve_aegis_captcha
    try:
        await solve_aegis_captcha(page, client, max_attempts=2)
    except AegisCaptchaFailed:
        # 第二次调用了 typeid 33 即可,结果成不成都行
        pass
    assert client.calls == [TYPEID_COORDINATE_1_4, TYPEID_SINGLE_GAP]


@pytest.mark.asyncio
async def test_solve_aegis_captcha_exhausts_attempts():
    from doupool.captcha.ttshitu_client import TtshituError
    client = FakeTtshituClient(results=[
        TtshituError("e1"),
        TtshituError("e2"),
        TtshituError("e3"),
    ])
    page = FakePage(locators={
        "[class*='aegis'][class*='dialog']": FakeLocator(count=1, visible=True),
        "[class*='verify']": FakeLocator(count=1, visible=True),
        "[class*='captcha']": FakeLocator(count=1, visible=True),
        "[class*='puzzle']": FakeLocator(count=1, visible=True),
    })
    from doupool.captcha.solver import solve_aegis_captcha
    with pytest.raises(AegisCaptchaFailed):
        await solve_aegis_captcha(page, client, max_attempts=3)
    assert len(client.calls) == 3