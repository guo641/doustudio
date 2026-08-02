"""
DouStudio 热更新检查器

启动时 + 用户手动「检查更新」按钮触发。
拉 GitHub releases latest,和当前版本对比,有新版本就回报 + 给出下载链接。
"""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlparse

import httpx


logger = logging.getLogger("doustudio.updater")


# 项目常量
GITHUB_REPO = os.environ.get("DOUSTUDIO_GH_REPO", "guo641/doustudio")
UPDATE_CHECK_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASE_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"
REQUEST_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class UpdateInfo:
    """前端展示用"""

    current_version: str
    latest_version: str
    has_update: bool
    release_url: str
    release_notes: str
    asset_urls: dict[str, str]  # platform_key → zip url,如 {"windows-x86_64": "..."}


def parse_version(tag: str) -> tuple[int, ...]:
    """v0.2.0 → (0, 2, 0); 去掉前缀 v 和非数字"""
    s = tag.strip().lstrip("vV")
    parts: list[int] = []
    for piece in s.split("."):
        digits = ""
        for ch in piece:
            if ch.isdigit():
                digits += ch
            else:
                break
        if digits:
            parts.append(int(digits))
    return tuple(parts) if parts else (0,)


def detect_platform() -> str:
    import platform as _plat
    system = _plat.system().lower()
    machine = _plat.machine().lower()
    if system == "windows":
        arch = "x86_64" if "64" in machine or "amd64" in machine else machine
    elif system == "darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
    else:
        arch = "x86_64" if "64" in machine else machine
    return f"{system}-{arch}"


def _platform_from_asset_name(name: str) -> Optional[str]:
    """DouStudio-v0.2.0-windows-x86_64.zip → windows-x86_64"""
    if not name.endswith(".zip"):
        return None
    base = name[:-4]  # 去掉 .zip
    # 取最后两段作为平台
    parts = base.split("-")
    if len(parts) >= 3 and parts[0] == "DouStudio":
        return "-".join(parts[-2:])  # windows-x86_64 / macos-arm64 / linux-x86_64
    return None


async def check_for_update(current_version: str) -> UpdateInfo:
    """
    调 GitHub releases/latest,返回 UpdateInfo(没新版本也返回 latest_version 字段)。

    失败不抛异常,降级返回 has_update=False + 旧 current_version。
    """
    plat = detect_platform()
    fallback = UpdateInfo(
        current_version=current_version,
        latest_version=current_version,
        has_update=False,
        release_url=RELEASE_PAGE_URL,
        release_notes="",
        asset_urls={},
    )
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "DouStudio-Updater/1.0",
    }
    try:
        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(UPDATE_CHECK_URL, headers=headers)
    except Exception as exc:  # noqa: BLE001 — 网络/超时/连接错误统一降级
        logger.warning("updater: 网络失败: %s", exc)
        return fallback

    if response.status_code == 403:
        # GitHub rate limit
        logger.warning("updater: GitHub API 限速 (HTTP 403)")
        return fallback
    if response.status_code != 200:
        logger.warning("updater: GitHub API 返回 HTTP %d", response.status_code)
        return fallback

    try:
        payload = response.json()
    except ValueError as exc:
        logger.warning("updater: 返回非 JSON: %s", exc)
        return fallback

    latest_tag = payload.get("tag_name") or ""
    latest_version = latest_tag or "0.0.0"
    body = payload.get("body") or ""
    html_url = payload.get("html_url") or RELEASE_PAGE_URL

    # 资产:取匹配当前平台的 zip
    assets: list[dict] = payload.get("assets", []) or []
    asset_urls: dict[str, str] = {}
    for a in assets:
        name = a.get("name") or ""
        plat_key = _platform_from_asset_name(name)
        if plat_key and a.get("browser_download_url"):
            asset_urls[plat_key] = a["browser_download_url"]

    has_update = parse_version(latest_version) > parse_version(current_version)
    return UpdateInfo(
        current_version=current_version,
        latest_version=latest_version,
        has_update=has_update,
        release_url=html_url,
        release_notes=body[:2000],
        asset_urls=asset_urls,
    )


# 为让上面的 import os 在 dataclass 后仍可用,补一行 import


def schedule_background_check(current_version: str, callback) -> None:
    """
    在后台异步线程跑一次 update check,完成后 callback(update_info)。
    callback 签名: (UpdateInfo) -> None,需要线程安全。
    """
    def _runner():
        try:
            info = asyncio.run(check_for_update(current_version))
            callback(info)
        except Exception as exc:  # noqa: BLE001
            logger.warning("updater: background check 异常: %s", exc)

    import threading
    t = threading.Thread(target=_runner, name="doustudio-updater", daemon=True)
    t.start()
