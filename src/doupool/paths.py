from __future__ import annotations

import os
import sys
from pathlib import Path


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def executable_dir() -> Path:
    """Directory containing the running executable (or project root in dev)."""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parents[2]


def resource_dir() -> Path:
    """Directory for read-only bundled assets (PyInstaller _MEIPASS or project root)."""
    if is_frozen():
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            return Path(meipass)
        return executable_dir()
    return Path(__file__).resolve().parents[2]


def frontend_dir() -> Path:
    env = os.environ.get("DOUPOOL_FRONTEND_DIR")
    if env:
        return Path(env)
    # Prefer next to the exe (onedir layout), then bundled resource tree.
    candidates = (
        executable_dir() / "frontend" / "dist",
        resource_dir() / "frontend" / "dist",
        resource_dir() / "dist",
    )
    for path in candidates:
        if (path / "index.html").exists():
            return path
    return candidates[0]


def playwright_browsers_dir() -> Path:
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env and env not in {"0", "1"}:
        return Path(env)
    # Packaged layout: <exe_dir>/ms-playwright
    bundled = executable_dir() / "ms-playwright"
    if bundled.exists():
        return bundled
    resource_bundle = resource_dir() / "ms-playwright"
    if resource_bundle.exists():
        return resource_bundle
    return bundled


def configure_runtime_environment() -> None:
    """Set env vars early so Playwright/pywebview resolve bundled assets."""
    browsers = playwright_browsers_dir()
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    # Avoid Playwright trying to download at runtime in frozen builds.
    if is_frozen():
        os.environ.setdefault("PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD", "1")
