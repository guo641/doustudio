# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for DoubaoManager (Windows onedir).

Expected cwd: repository root.
Requires frontend/dist and ms-playwright to already exist.
"""

from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules

block_cipher = None
root = Path(SPECPATH).resolve().parent

datas = []
binaries = []
hiddenimports = []

# Frontend SPA
frontend_dist = root / "frontend" / "dist"
if frontend_dist.exists():
    datas.append((str(frontend_dist), "frontend/dist"))

# Package data / submodules that PyInstaller may miss
for package in ("uvicorn", "fastapi", "starlette", "anyio", "httpx", "peewee", "webview", "playwright"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
        datas += pkg_datas
        binaries += pkg_binaries
        hiddenimports += pkg_hidden
    except Exception:
        hiddenimports += collect_submodules(package)
        try:
            datas += collect_data_files(package)
        except Exception:
            pass

hiddenimports += [
    "doupool",
    "doupool.api.app",
    "doupool.desktop",
    "doupool.login.browser",
    "doupool.login.service",
    "doupool.video.browser",
    "doupool.video.service",
    "doupool.settings.service",
    "doupool.watermark.zhuceka",
    "doupool.watermark",
    "doupool.prompt_reviser",
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "playwright.sync_api",
    "playwright._impl._driver",
]

a = Analysis(
    [str(root / "packaging" / "entry.py")],
    pathex=[str(root / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[str(root / "packaging" / "hooks")],
    hooksconfig={},
    runtime_hooks=[str(root / "packaging" / "runtime_hook.py")],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="DouStudio",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(root / "packaging" / "icon.ico") if (root / "packaging" / "icon.ico").exists() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="DouStudio",
)
