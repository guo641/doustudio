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

import json
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from doupool.captcha.solver import CaptchaKind
from doupool.video.browser import (
    EDITOR_SEL,
    SEND_BTN_SEL,
    SEND_BTN_FALLBACK_SEL,
    VIDEO_TAB_SEL,
    PlaywrightVideoRunner,
    _ack_interceptor,
    _build_launch_kwargs,
    _pre_submit_aegis_gate,
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
    )

    # 契约:result 必含 3 字段(service.py **ack 解包不会 KeyError)
    assert result["conversation_id"] == "C1"
    assert result["section_id"] == "S1"
    assert result["question_id"] == "Q1"
    # update 至少调过一次 status=generating 把 ack 写进去
    update.assert_any_call(status="generating", **result)


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
async def test_pre_submit_aegis_gate_credentials_disabled_degrades_gracefully(
    monkeypatch, fast_gate_constants,
):
    """case 5:图鉴凭证关(没配 / enabled=false)→ make_client 抛
    AegisCaptchaDisabled → 网关不挂,降级放行 + update 提示文案。

    不能让凭证关变成「弹窗卡死 + submit 撞 aegis」—— 后者比直接 fail 更糟。
    应该:mark cooldown + 告诉用户「需要手动拖」+ 放行让 submit 撞弹窗 →
    走原有失败路径(用户看错误文案)。
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
    # 凭证关 → 降级放行(让 submit 撞弹窗走原失败路径,不让 task 神秘卡死)
    assert allowed is True, "凭证关应降级放行,不能让任务卡死"
    update_msgs = [c.kwargs.get("error_message", "") for c in update.call_args_list]
    # 应该告诉用户「凭证缺失」或「需要手动拖」
    assert any(
        "凭证" in m or "手动" in m or "图鉴" in m or "停用" in m for m in update_msgs
    ), f"凭证关必须有提示; 实际={update_msgs}"


# ------------------------- v0.3.2.3 submit_via_ui 行为契约 ------------------------- #


@pytest.mark.asyncio
async def test_submit_via_ui_raises_when_gate_blocks(monkeypatch, fast_gate_constants):
    """v0.3.2.3 submit_via_ui 契约:_pre_submit_aegis_gate 返 False → 整体 raise RuntimeError,不能进入 step 3 后续 click 流程。

    防止代码被改回 fire-and-forget:即使 gate 拒绝,后续 try_click 也不该被调。
    """
    page = _FakePage(url="https://www.doubao.com/chat/create-image")
    update = MagicMock()

    monkeypatch.setattr(
        "doupool.video.browser._pre_submit_aegis_gate",
        AsyncMock(return_value=False),
    )
    # _try_solve_captcha_in_video 兜底也被 step 6 调,需要 stub
    monkeypatch.setattr(
        "doupool.video.browser._try_solve_captcha_in_video",
        AsyncMock(return_value=False),
    )

    with pytest.raises(RuntimeError, match="aegis 拖拽验证未通过"):
        await submit_via_ui(page, "x", profile_dir=Path("/tmp/p"), update=update)

    # 关键:raise 之后,send button 不该被 click(mouse.downs 应为 0)。
    # VIDEO_TAB_SEL 也不该被 click(还没到 step 3)。
    assert page.mouse.downs == 0, (
        f"gate 拒绝后 mouse.downs 必须为 0(不该进 step 3/6); "
        f"实际={page.mouse.downs}, moves={page.mouse.moves}"
    )
