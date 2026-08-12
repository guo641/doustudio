"""v0.3.1.2:helper `_try_solve_captcha_in_video` 单测。

video runner 路径接 aegis solver 的接入点是模块级 async 函数
`_try_solve_captcha_in_video(page, profile_dir, update, *, wait_for_popup_seconds)`
(见 src/doupool/video/browser.py)。它与 login 路径的
`_try_solve_captcha_if_needed` 是两套实现(一个 sync+asyncio.run,一个
直接 async)—— **不能**复用,所以单独写一套 mock-friendly 单测。

设计要点:
- 失败一律不 raise —— helper 出问题不能让 task 直接挂,这是用户决策
- cooldown 同账号 login/video 共享:account_key = `str(profile_dir)`
- `update(error_message=...)` 通道用于上报图鉴进度,不引新 status 字段
- `wait_for_popup_seconds=0` 是 poll 路径(弹窗已在),>0 是提交前等弹窗

本测试不真启 Playwright —— 所有依赖(monkeypatch)+ AsyncMock 包,
用 pytest-asyncio 跑。
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from doupool.captcha.config import CaptchaCredentials
from doupool.captcha.solver import (
    AegisCaptchaDisabled,
    AegisCaptchaFailed,
    CaptchaKind,
    clear_cooldown,
    is_in_cooldown,
    mark_cooldown,
)
from doupool.video import browser as video_browser
from doupool.video.browser import (
    _CAPTCHA_DETECT_INTERVAL_POLLS,
    _CAPTCHA_DETECT_WAIT_BEFORE_SUBMIT_SECONDS,
    _try_solve_captcha_in_video,
)


# v0.3.1.2:确保每个 test 用全新 profile_dir,避免 cooldown 串到下一个 test
@pytest.fixture
def profile_dir(tmp_path: Path) -> Path:
    pd = tmp_path / "chromium-profile"
    pd.mkdir()
    return pd


@pytest.fixture(autouse=True)
def _reset_cooldown():
    """测试前后清理 cooldown —— helper 用 str(profile_dir) 做 key,目录在
    tmp_path 里,本来不会串,但 mark_cooldown 已经把 dict 写脏了,清一下
    避免 test 顺序耦合。
    """
    yield
    # 模块级 dict,这里没办法 iter key,只清空已知 marker 之外的;
    # 直接 monkeypatch 进 helper 的 _captcha_is_in_cooldown / _captcha_mark_cooldown
    # 才是稳的 —— 见下面 fixture。
    try:
        from doupool.captcha.solver import _captcha_cooldown
        _captcha_cooldown.clear()
    except ImportError:
        pass


def _stub_captcha_modules(
    monkeypatch: pytest.MonkeyPatch,
    *,
    detect_kind: CaptchaKind | Exception = CaptchaKind.UNKNOWN,
    solve_side_effect: Exception | None = None,
    make_client_side_effect: Exception | None = None,
    load_credentials_return: CaptchaCredentials | None = None,
):
    """把 helper 调用的所有 captcha 子函数 / 凭证读都 monkeypatch 掉。

    - detect_kind: detect_aegis_captcha 返的 Kind(默认 UNKNOWN = 无弹窗)
      也可传 Exception 模拟 detect 自身抛错
    - solve_side_effect: solve_aegis_captcha 抛什么异常(None = 正常返回)
    - make_client_side_effect: make_client 抛什么异常(通常 AegisCaptchaDisabled)
    - load_credentials_return: load_credentials 返什么(默认 None,函数内部会用 None
      触发 AegisCaptchaDisabled —— None 不传就是「凭证关掉」场景)
    """
    # 1) detect_aegis_captcha
    if isinstance(detect_kind, Exception):
        async def _detect(_page):
            raise detect_kind
        detect_mock = _detect
    else:
        async def _detect(_page):
            return detect_kind
        detect_mock = _detect

    # 2) solve_aegis_captcha
    if solve_side_effect is not None:
        async def _solve(_page, _client, on_state=None):
            if on_state is not None:
                # 模拟 solver 内部确实调了 on_state —— 验证 update 通道被触发
                on_state("uploading")
                on_state("dragging")
                on_state("ok")
            raise solve_side_effect
        solve_mock = _solve
    else:
        async def _solve(_page, _client, on_state=None):
            if on_state is not None:
                on_state("uploading")
                on_state("dragging")
                on_state("ok")
            return None
        solve_mock = _solve

    # 3) make_client
    if make_client_side_effect is not None:
        def _make_client(_creds):
            raise make_client_side_effect
        make_client_mock = _make_client
    else:
        client = MagicMock()
        client.close = MagicMock()
        def _make_client(_creds):
            return client
        make_client_mock = _make_client

    # 4) load_credentials
    def _load_creds(_settings=None):
        return load_credentials_return
    load_creds_mock = _load_creds

    monkeypatch.setattr(video_browser, "_detect_aegis_captcha", detect_mock)
    monkeypatch.setattr(video_browser, "_solve_aegis_captcha", solve_mock)
    monkeypatch.setattr(video_browser, "_make_captcha_client", make_client_mock)
    monkeypatch.setattr(video_browser, "_load_captcha_credentials", load_creds_mock)


def _usable_creds() -> CaptchaCredentials:
    # CaptchaCredentials 只三个字段(username / password / enabled)。
    # 「来源(env vs sqlite)」是 load_credentials 内部的实现细节,不在数据类里。
    return CaptchaCredentials(
        username="demo", password="demo", enabled=True,
    )


# ─────────────────────────────────────────────────────────────────────────────
# 1. cooldown 已设 → helper 不调 detect/solve
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_skips_when_in_cooldown(
    monkeypatch: pytest.MonkeyPatch, profile_dir: Path,
):
    """cooldown 已设 → 立即返 False,detect/solve 都不调。"""
    account_key = str(profile_dir)
    mark_cooldown(account_key)

    detect_called = False
    async def _should_not_run(_page):
        nonlocal detect_called
        detect_called = True
        return CaptchaKind.DRAG_SHAPE

    monkeypatch.setattr(video_browser, "_detect_aegis_captcha", _should_not_run)

    updates: list[dict] = []
    result = await _try_solve_captcha_in_video(
        MagicMock(), profile_dir, lambda **kw: updates.append(kw),
    )
    assert result is False
    assert detect_called is False
    assert updates == []

    clear_cooldown(account_key)


# ─────────────────────────────────────────────────────────────────────────────
# 2. 无弹窗(detect 返 UNKNOWN)→ 返 False,不调 solve
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_popup_returns_false_without_solving(
    monkeypatch: pytest.MonkeyPatch, profile_dir: Path,
):
    _stub_captcha_modules(
        monkeypatch,
        detect_kind=CaptchaKind.UNKNOWN,
        load_credentials_return=_usable_creds(),
    )
    updates: list[dict] = []
    result = await _try_solve_captcha_in_video(
        MagicMock(), profile_dir, lambda **kw: updates.append(kw),
    )
    assert result is False
    # cooldown 不该被 mark(没解成也没失败,只是没弹窗)
    assert not is_in_cooldown(str(profile_dir))
    assert updates == []


# ─────────────────────────────────────────────────────────────────────────────
# 3. detect 抛异常 → 静默吞,不 raise,cooldown 不动
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_detect_exception_swallowed(
    monkeypatch: pytest.MonkeyPatch, profile_dir: Path,
):
    _stub_captcha_modules(
        monkeypatch,
        detect_kind=RuntimeError("playwright detached"),
        load_credentials_return=_usable_creds(),
    )
    updates: list[dict] = []
    # 关键断言:不抛
    result = await _try_solve_captcha_in_video(
        MagicMock(), profile_dir, lambda **kw: updates.append(kw),
    )
    assert result is False
    assert not is_in_cooldown(str(profile_dir))


# ─────────────────────────────────────────────────────────────────────────────
# 4. 弹窗就位 + 凭证 OK + 成功 → mark_cooldown + update 报错,返 True
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_happy_path_solves_and_marks_cooldown(
    monkeypatch: pytest.MonkeyPatch, profile_dir: Path,
):
    _stub_captcha_modules(
        monkeypatch,
        detect_kind=CaptchaKind.DRAG_SHAPE,
        load_credentials_return=_usable_creds(),
    )
    updates: list[dict] = []
    result = await _try_solve_captcha_in_video(
        MagicMock(), profile_dir, lambda **kw: updates.append(kw),
        wait_for_popup_seconds=0,
    )
    assert result is True
    assert is_in_cooldown(str(profile_dir))
    # update 通道应该被打过 progress + ok 提示
    msgs = [u.get("error_message", "") for u in updates]
    assert any("正在通过图鉴打码平台识别拖拽验证" in m for m in msgs), msgs
    assert any("拖拽验证已通过" in m for m in msgs), msgs

    clear_cooldown(str(profile_dir))


# ─────────────────────────────────────────────────────────────────────────────
# 5. wait_for_popup_seconds > 0 → 真 sleep 那个时长
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_for_popup_seconds_sleeps(
    monkeypatch: pytest.MonkeyPatch, profile_dir: Path,
):
    """提交前路径要 wait_for_popup 让弹窗出现,helper 内部 asyncio.sleep,
    # 实际真睡 —— 用 monkeypatch asyncio.sleep 加速测试 + 断言被调一次。
    """
    _stub_captcha_modules(
        monkeypatch,
        detect_kind=CaptchaKind.UNKNOWN,  # 无弹窗也走 sleep
        load_credentials_return=_usable_creds(),
    )
    sleep_mock = AsyncMock()
    monkeypatch.setattr(video_browser.asyncio, "sleep", sleep_mock)

    await _try_solve_captcha_in_video(
        MagicMock(), profile_dir, lambda **kw: None,
        wait_for_popup_seconds=_CAPTCHA_DETECT_WAIT_BEFORE_SUBMIT_SECONDS,
    )
    sleep_mock.assert_awaited_once_with(_CAPTCHA_DETECT_WAIT_BEFORE_SUBMIT_SECONDS)


# ─────────────────────────────────────────────────────────────────────────────
# 6. wait_for_popup_seconds=0(poll 路径)→ 不 sleep
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_poll_path_does_not_sleep(
    monkeypatch: pytest.MonkeyPatch, profile_dir: Path,
):
    _stub_captcha_modules(
        monkeypatch,
        detect_kind=CaptchaKind.UNKNOWN,
        load_credentials_return=_usable_creds(),
    )
    sleep_mock = AsyncMock()
    monkeypatch.setattr(video_browser.asyncio, "sleep", sleep_mock)

    await _try_solve_captcha_in_video(
        MagicMock(), profile_dir, lambda **kw: None,
        wait_for_popup_seconds=0,
    )
    sleep_mock.assert_not_called()


# ─────────────────────────────────────────────────────────────────────────────
# 7. 弹窗就位 + 凭证缺失 → mark_cooldown + 上报 + 不 raise
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_disabled_credentials_marks_cooldown_and_reports(
    monkeypatch: pytest.MonkeyPatch, profile_dir: Path,
):
    _stub_captcha_modules(
        monkeypatch,
        detect_kind=CaptchaKind.DRAG_SHAPE,
        make_client_side_effect=AegisCaptchaDisabled("no credentials"),
        load_credentials_return=None,
    )
    updates: list[dict] = []
    # 关键断言:不抛
    result = await _try_solve_captcha_in_video(
        MagicMock(), profile_dir, lambda **kw: updates.append(kw),
    )
    assert result is True
    assert is_in_cooldown(str(profile_dir))
    msgs = [u.get("error_message", "") for u in updates]
    assert any("图鉴打码未启用或凭证缺失" in m for m in msgs), msgs


# ─────────────────────────────────────────────────────────────────────────────
# 8. solver 抛 AegisCaptchaFailed → mark_cooldown + 上报 + 不 raise
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_solver_failure_marks_cooldown_and_reports(
    monkeypatch: pytest.MonkeyPatch, profile_dir: Path,
):
    _stub_captcha_modules(
        monkeypatch,
        detect_kind=CaptchaKind.DRAG_SHAPE,
        solve_side_effect=AegisCaptchaFailed("ttshitu returned 400"),
        load_credentials_return=_usable_creds(),
    )
    updates: list[dict] = []
    result = await _try_solve_captcha_in_video(
        MagicMock(), profile_dir, lambda **kw: updates.append(kw),
    )
    assert result is True
    assert is_in_cooldown(str(profile_dir))
    msgs = [u.get("error_message", "") for u in updates]
    assert any("图鉴自动解算失败" in m for m in msgs), msgs


# ─────────────────────────────────────────────────────────────────────────────
# 9. solver 抛 AegisCaptchaDisabled(mid-run)→ mark_cooldown + 上报
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_solver_disabled_midrun(
    monkeypatch: pytest.MonkeyPatch, profile_dir: Path,
):
    _stub_captcha_modules(
        monkeypatch,
        detect_kind=CaptchaKind.SLIDE_PUZZLE,
        solve_side_effect=AegisCaptchaDisabled("auth expired mid-run"),
        load_credentials_return=_usable_creds(),
    )
    updates: list[dict] = []
    result = await _try_solve_captcha_in_video(
        MagicMock(), profile_dir, lambda **kw: updates.append(kw),
    )
    assert result is True
    assert is_in_cooldown(str(profile_dir))
    msgs = [u.get("error_message", "") for u in updates]
    assert any("凭证失效" in m for m in msgs), msgs


# ─────────────────────────────────────────────────────────────────────────────
# 10. solver 抛完全意料外的 Exception → 静默吞 + mark_cooldown
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_solver_unexpected_exception_swallowed(
    monkeypatch: pytest.MonkeyPatch, profile_dir: Path,
):
    _stub_captcha_modules(
        monkeypatch,
        detect_kind=CaptchaKind.DRAG_SHAPE,
        solve_side_effect=RuntimeError("ttshitu base64 decode boom"),
        load_credentials_return=_usable_creds(),
    )
    result = await _try_solve_captcha_in_video(
        MagicMock(), profile_dir, lambda **kw: None,
    )
    assert result is True
    assert is_in_cooldown(str(profile_dir))


# ─────────────────────────────────────────────────────────────────────────────
# 11. update() 抛异常 → helper 不挂
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_exception_swallowed(
    monkeypatch: pytest.MonkeyPatch, profile_dir: Path,
):
    """前端断网 / DB 写失败 → update 抛,helper 仍要把 cooldown 标上
    并让 task 继续走(不能让图鉴流程把整个 task 干挂)。
    """
    _stub_captcha_modules(
        monkeypatch,
        detect_kind=CaptchaKind.DRAG_SHAPE,
        load_credentials_return=_usable_creds(),
    )
    def _broken_update(**_kw):
        raise RuntimeError("DB closed")
    result = await _try_solve_captcha_in_video(
        MagicMock(), profile_dir, _broken_update,
    )
    assert result is True
    assert is_in_cooldown(str(profile_dir))


# ─────────────────────────────────────────────────────────────────────────────
# 12. account_key 一致 —— login 路径 mark_cooldown 后 video 路径能看见
# ─────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cooldown_shared_with_login_path(profile_dir: Path):
    """account_key = str(profile_dir) 是 login + video 共享的约定,
    确保 video helper 用这个 key 调 is_in_cooldown / mark_cooldown。

    间接验证:从 login/browser.py 的 alias 拿的就是同一个模块级 dict。
    """
    from doupool.login import browser as login_browser

    account_key = str(profile_dir)
    # login 路径 mark_cooldown(用其 alias)
    login_browser._mark_captcha_cooldown(account_key)
    assert is_in_cooldown(account_key)
    # video helper 用同样 key 调 is_in_cooldown
    assert video_browser._captcha_is_in_cooldown(account_key)

    clear_cooldown(account_key)
    assert not video_browser._captcha_is_in_cooldown(account_key)


# ─────────────────────────────────────────────────────────────────────────────
# 13. 模块常量值是合理默认
# ─────────────────────────────────────────────────────────────────────────────


def test_module_constants_reasonable():
    """提交前等 4s、poll 每 3 轮 —— 改动要慎重,单测盯住。"""
    assert _CAPTCHA_DETECT_WAIT_BEFORE_SUBMIT_SECONDS >= 2.0
    assert _CAPTCHA_DETECT_WAIT_BEFORE_SUBMIT_SECONDS <= 10.0
    assert _CAPTCHA_DETECT_INTERVAL_POLLS >= 2
    assert _CAPTCHA_DETECT_INTERVAL_POLLS <= 10