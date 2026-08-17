"""Read-only DOM probes for the Doubao aegis verification popup."""

from __future__ import annotations

from playwright.async_api import Page as AsyncPage
from playwright.sync_api import Page as SyncPage


AEGIS_POPUP_SELECTOR = (
    "div[class*='aegis'], div[class*='verify'], iframe[src*='aegis'], "
    "div[class*='captcha'], div[class*='puzzle']"
)


async def aegis_popup_present(page: AsyncPage) -> bool:
    """Return whether an aegis popup container is currently visible."""
    try:
        await page.wait_for_selector(
            AEGIS_POPUP_SELECTOR,
            state="visible",
            timeout=200,
        )
        return True
    except Exception:
        return False


def aegis_popup_present_sync(page: SyncPage) -> bool:
    """Synchronous equivalent for visible login-browser integrations."""
    try:
        page.wait_for_selector(
            AEGIS_POPUP_SELECTOR,
            state="visible",
            timeout=200,
        )
        return True
    except Exception:
        return False
