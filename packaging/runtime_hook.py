"""Runtime hook executed before application code.

Ensures Playwright uses browsers packaged next to the executable.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _configure() -> None:
    if not getattr(sys, "frozen", False):
        return
    exe_dir = Path(sys.executable).resolve().parent
    browsers = exe_dir / "ms-playwright"
    os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers)
    os.environ["PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD"] = "1"
    # Help some native loaders resolve sibling DLLs on Windows.
    if hasattr(os, "add_dll_directory") and sys.platform == "win32":
        try:
            os.add_dll_directory(str(exe_dir))
            internal = exe_dir / "_internal"
            if internal.exists():
                os.add_dll_directory(str(internal))
        except OSError:
            pass


_configure()
