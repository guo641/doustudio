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

# v0.3.1.1 兜底:_license_verify 是 Cython .pyd,优先级最高,会遮蔽同目录
# __init__.py,导致 license/__init__.py 里的 monkey-patch 兜底逻辑拿不到模块对象
# 加载。显式把 __init__.py 拷到 onedir 里的同位置(.pyd 旁),让 Python import
# 时优先加载 .py(因为 .py 比 .pyd 名字不完全匹配,但同目录 + 同模块名时
# Python 实际行为是按 sys.path 顺序找 —— 这里 .pyd 是 _license_verify 模块,
# __init__.py 是 license 包,两者不同名不冲突)。
datas.append((
    str(root / "src" / "doupool" / "license" / "__init__.py"),
    "doupool/license",
))

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
    # v0.3.0:离线激活闸门 —— Cython 编译的 verifier 是 .pyd,必须显式列出
    # 否则 PyInstaller 静态分析扫不到,启动时 _license_verify 模块不存在。
    # 此外 fingerprint._decoded_pubkey() 在 import 时引用 doupool.license._embedded_pubkey
    # (XOR 编码的公钥常量,_ 前缀模块静态分析默认不收)。collect_submodules 收整个
    # 子树是最稳的写法,避免下次有人加新的 _helper 又漏列 hiddenimports →
    # 主程序静默崩。同样的修法见 tools/license_keygen/keygen.spec。
    "doupool._license_verify",
    *collect_submodules("doupool.license"),
    "doupool.license.verify_at_import",
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
