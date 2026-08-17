from __future__ import annotations

import threading
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import doupool.video.browser as browser_module
from doupool.video.aegis_probe import (
    AEGIS_POPUP_SELECTOR,
    aegis_popup_present,
    aegis_popup_present_sync,
)
from doupool.video.browser import (
    AEGIS_BLOCKED_MESSAGE,
    AegisBlocked,
    PlaywrightVideoRunner,
    _handle_aegis_in_poll,
    _pre_submit_aegis_gate,
)
from doupool.video.protocol import DoubaoRateLimited


class _AsyncProbePage:
    def __init__(self, *, present: bool):
        self.present = present
        self.calls: list[tuple[str, str, int]] = []

    async def wait_for_selector(self, selector, *, state, timeout):
        self.calls.append((selector, state, timeout))
        if not self.present:
            raise RuntimeError("not found")
        return object()


class _SyncProbePage:
    def __init__(self, *, present: bool):
        self.present = present

    def wait_for_selector(self, selector, *, state, timeout):
        assert selector == AEGIS_POPUP_SELECTOR
        assert state == "visible"
        assert timeout == 200
        if not self.present:
            raise RuntimeError("not found")
        return object()


@pytest.mark.asyncio
async def test_aegis_popup_present_is_read_only_boolean_probe():
    present = _AsyncProbePage(present=True)
    absent = _AsyncProbePage(present=False)

    assert await aegis_popup_present(present) is True
    assert await aegis_popup_present(absent) is False
    assert present.calls == [(AEGIS_POPUP_SELECTOR, "visible", 200)]


def test_aegis_popup_present_sync_matches_async_semantics():
    assert aegis_popup_present_sync(_SyncProbePage(present=True)) is True
    assert aegis_popup_present_sync(_SyncProbePage(present=False)) is False


@pytest.mark.asyncio
async def test_poll_probe_absent_allows_poll(monkeypatch):
    monkeypatch.setattr(browser_module, "aegis_popup_present", AsyncMock(return_value=False))

    assert await _handle_aegis_in_poll(MagicMock(), Path("profile"), MagicMock()) is False


@pytest.mark.asyncio
async def test_poll_probe_present_raises_fixed_user_message(monkeypatch):
    update = MagicMock()
    monkeypatch.setattr(browser_module, "aegis_popup_present", AsyncMock(return_value=True))

    with pytest.raises(AegisBlocked, match=AEGIS_BLOCKED_MESSAGE):
        await _handle_aegis_in_poll(MagicMock(), Path("profile"), update)

    update.assert_called_once_with(error_message=AEGIS_BLOCKED_MESSAGE)


@pytest.mark.asyncio
async def test_pre_submit_gate_absent_allows_submit(monkeypatch):
    monkeypatch.setattr(browser_module, "_UI_AEGIS_WAIT_SECONDS", 0.02)
    monkeypatch.setattr(browser_module, "_UI_AEGIS_DETECT_POLL_INTERVAL", 0.005)
    monkeypatch.setattr(browser_module, "aegis_popup_present", AsyncMock(return_value=False))

    assert await _pre_submit_aegis_gate(MagicMock(), Path("profile"), MagicMock()) is None


@pytest.mark.asyncio
async def test_pre_submit_gate_present_blocks_submit(monkeypatch):
    update = MagicMock()
    monkeypatch.setattr(browser_module, "aegis_popup_present", AsyncMock(return_value=True))

    with pytest.raises(AegisBlocked, match=AEGIS_BLOCKED_MESSAGE):
        await _pre_submit_aegis_gate(MagicMock(), Path("profile"), update)

    update.assert_called_once_with(error_message=AEGIS_BLOCKED_MESSAGE)


@pytest.mark.asyncio
async def test_release_profile_context_closes_and_clears_caches():
    runner = PlaywrightVideoRunner()
    profile = Path("profile-a")
    context = MagicMock()
    context.close = AsyncMock()
    page = MagicMock()
    page.is_closed.return_value = False
    page.close = AsyncMock()
    context.pages = [page]
    runner._contexts[str(profile)] = context
    runner._tokens[str(profile)] = MagicMock()

    await runner._release_profile_context(profile)
    await runner._release_profile_context(profile)

    context.close.assert_awaited_once()
    page.close.assert_awaited_once()
    assert str(profile) not in runner._contexts
    assert str(profile) not in runner._tokens


@pytest.mark.asyncio
async def test_run_aegis_blocked_releases_profile(monkeypatch):
    runner = PlaywrightVideoRunner()
    profile = Path("profile-run")
    page = MagicMock(url="https://www.doubao.com/chat/")
    page.close = AsyncMock()
    context = MagicMock()
    context.is_closed.return_value = False
    context.new_page = AsyncMock(return_value=page)
    runner._get_shared_context = AsyncMock(
        return_value=(context, MagicMock(device_id="device", web_id="web"))
    )
    runner._release_profile_context = AsyncMock()
    monkeypatch.setattr(
        browser_module,
        "_pre_submit_aegis_gate",
        AsyncMock(side_effect=AegisBlocked(AEGIS_BLOCKED_MESSAGE)),
    )

    with pytest.raises(AegisBlocked, match=AEGIS_BLOCKED_MESSAGE):
        await runner.run(
            profile,
            "prompt",
            "seedance_v2.0_mini",
            "1:1",
            5,
            MagicMock(),
            threading.Event(),
        )

    runner._release_profile_context.assert_awaited_once_with(profile)
    page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_recheck_aegis_blocked_releases_profile(monkeypatch):
    runner = PlaywrightVideoRunner()
    profile = Path("profile-recheck")
    page = MagicMock(url="https://www.doubao.com/chat/")
    page.close = AsyncMock()
    page.goto = AsyncMock()
    page.wait_for_timeout = AsyncMock()
    page.evaluate = AsyncMock()
    context = MagicMock()
    context.is_closed.return_value = False
    context.new_page = AsyncMock(return_value=page)
    runner._get_shared_context = AsyncMock(return_value=(context, MagicMock()))
    runner._release_profile_context = AsyncMock()
    monkeypatch.setattr(
        browser_module,
        "_handle_aegis_in_poll",
        AsyncMock(side_effect=AegisBlocked(AEGIS_BLOCKED_MESSAGE)),
    )

    with pytest.raises(AegisBlocked, match=AEGIS_BLOCKED_MESSAGE):
        await runner.recheck_result(
            profile,
            "conversation",
            MagicMock(),
            threading.Event(),
        )

    runner._release_profile_context.assert_awaited_once_with(profile)
    page.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_submit_poll_aegis_blocked_stops_before_chain(monkeypatch):
    runner = PlaywrightVideoRunner(timeout=10, poll_interval=1)
    page = MagicMock()
    page.evaluate = AsyncMock()

    @asynccontextmanager
    async def fake_interceptor(_page):
        yield {"text": "ack"}

    monkeypatch.setattr(browser_module, "_ack_interceptor", fake_interceptor)
    monkeypatch.setattr(browser_module, "submit_via_ui", AsyncMock())
    monkeypatch.setattr(browser_module, "_wait_for_ack", AsyncMock(return_value="ack"))
    monkeypatch.setattr(
        browser_module,
        "parse_sse_ack",
        MagicMock(return_value={
            "conversation_id": "conversation",
            "section_id": "section",
            "question_id": "question",
        }),
    )
    monkeypatch.setattr(
        browser_module,
        "_handle_aegis_in_poll",
        AsyncMock(side_effect=AegisBlocked(AEGIS_BLOCKED_MESSAGE)),
    )

    with pytest.raises(AegisBlocked, match=AEGIS_BLOCKED_MESSAGE):
        await runner._submit_and_poll(
            page,
            "prompt",
            "seedance_v2.0_mini",
            "1:1",
            5,
            "fingerprint",
            MagicMock(),
            "t2v",
            [],
            MagicMock(),
            threading.Event(),
            Path("profile"),
        )

    page.evaluate.assert_not_awaited()


@pytest.mark.asyncio
async def test_risk_control_no_longer_enters_auto_solve_retry(monkeypatch):
    runner = PlaywrightVideoRunner()
    profile = Path("profile-risk")
    page = MagicMock(url="https://www.doubao.com/chat/")
    page.close = AsyncMock()
    context = MagicMock()
    context.is_closed.return_value = False
    context.new_page = AsyncMock(return_value=page)
    runner._get_shared_context = AsyncMock(
        return_value=(context, MagicMock(device_id="device", web_id="web"))
    )
    monkeypatch.setattr(browser_module, "_pre_submit_aegis_gate", AsyncMock())
    runner._submit_and_poll = AsyncMock(
        side_effect=DoubaoRateLimited("risk", is_risk_control=True)
    )

    with pytest.raises(DoubaoRateLimited):
        await runner.run(
            profile,
            "prompt",
            "seedance_v2.0_mini",
            "1:1",
            5,
            MagicMock(),
            threading.Event(),
        )

    runner._submit_and_poll.assert_awaited_once()
