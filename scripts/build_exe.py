"""
DouStudio 打包脚本

用法:
  python scripts/build_exe.py --onedir                 # 默认 Windows onedir
  python scripts/build_exe.py --onefile                # 打成单文件(更慢启动)
  python scripts/build_exe.py --onedir --version v0.2.0 --upload
                                            # 打包 + 上传 GitHub Release

环境变量:
  GH_TOKEN          GitHub PAT(用于 --upload)
  GITHUB_REPOSITORY 形如 guo641/doustudio
"""

from __future__ import annotations

import argparse
import hashlib
import os
import platform
import shutil
import subprocess
import sys
import sysconfig
import urllib.parse
import zipfile
from pathlib import Path


# 强制 stdout/stderr 用 utf-8,避免 Windows runner 默认 cp1252 撞上中文/箭头字符
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, ValueError):
        pass


REPO_ROOT = Path(__file__).resolve().parents[1]


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _dist_dir(mode: str) -> Path:
    name = "DouStudio" + ("_onefile" if mode == "onefile" else "")
    return REPO_ROOT / "dist" / name


def _detect_platform() -> str:
    """返回 windows-x86_64 / linux-x86_64 / macos-arm64 等"""
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "windows":
        arch = "x86_64" if "64" in machine or "amd64" in machine else machine
    elif system == "darwin":
        arch = "arm64" if machine in ("arm64", "aarch64") else "x86_64"
    else:
        arch = "x86_64" if "64" in machine else machine
    return f"{system}-{arch}"


def _resolve_npm() -> str:
    """
    在 PATH 里找 npm,找不到时兜底常见 Windows 安装位置。
    Git Bash / PyInstaller 子进程经常不继承完整 PATH,直接 shutil.which 会落空。
    """
    found = shutil.which("npm") or shutil.which("npm.cmd") or shutil.which("npm.exe")
    if found:
        return found
    # Windows 常见位置(官方安装包 + nvm-windows)
    candidates = [
        r"C:\Program Files\nodejs\npm.cmd",
        r"C:\Program Files (x86)\nodejs\npm.cmd",
        os.path.expandvars(r"%NVM_SYMLINK%\npm.cmd"),
        os.path.expandvars(r"%APPDATA%\nvm\v.bat"),  # nvm-windows 旧版
    ]
    for cand in candidates:
        if cand and Path(cand).exists():
            return cand
    raise RuntimeError(
        "找不到 npm。PATH 没 Node.js,常见原因:\n"
        "  - 没装 Node:https://nodejs.org/\n"
        "  - 装了但当前 shell 的 PATH 不含 Node 目录(Windows 默认 C:\\Program Files\\nodejs)\n"
        "  - 用 nvm-windows:需让 NVM_SYMLINK 在 PATH(默认就在,但部分 sandbox 会过滤)\n"
        "如果确认 Node 已装,请在新开的 cmd/PowerShell 重跑本脚本"
    )


def _run(cmd: list[str], cwd: Path, what: str) -> None:
    """运行子命令并 fail-loud:returncode != 0 直接抛 RuntimeError"""
    print(f"[{what}] $ {' '.join(cmd)}  (cwd={cwd})")
    try:
        subprocess.run(cmd, cwd=str(cwd), check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"{what} 失败: returncode={exc.returncode}\n"
            f"  cmd: {' '.join(cmd)}\n"
            f"  cwd: {cwd}"
        ) from exc


def compile_cython_extensions() -> None:
    """
    v0.3.0:编译 verifier.pyx → 当前 Python ABI 对应的 _license_verify*.pyd。
    必须在 PyInstaller 之前跑,因为 spec 依赖 _license_verify 存在(否则 hiddenimports 落空)。
    - 如果当前 ABI 的 .pyd 已存在且比 .pyx 新,跳过
    - 否则调 setup.py build_ext --inplace
    - 编译失败或产物 ABI 不匹配时直接终止;发布包绝不降级到纯 Python verifier。
    """
    pyx = REPO_ROOT / "src" / "doupool" / "license" / "verifier.pyx"
    if not pyx.exists():
        raise RuntimeError(f"找不到 {pyx},请确认仓库结构")
    extension_suffix = sysconfig.get_config_var("EXT_SUFFIX")
    if not extension_suffix or not extension_suffix.endswith(".pyd"):
        raise RuntimeError(
            f"当前解释器没有 Windows .pyd 扩展后缀: {extension_suffix!r}"
        )
    expected_pyd = pyx.parent / f"_license_verify{extension_suffix}"
    if expected_pyd.exists() and expected_pyd.stat().st_mtime > pyx.stat().st_mtime:
        print(f"[cython] 当前 ABI 的 .pyd 已是最新,跳过编译 -> {expected_pyd.name}")
        return
    setup_py = REPO_ROOT / "setup.py"
    if not setup_py.exists():
        raise RuntimeError(f"找不到 {setup_py},请确认仓库根有 setup.py(Cython 编译入口)")
    print(f"[cython] 编译 {pyx.name} ...")
    _run(
        [sys.executable, str(setup_py), "build_ext", "--inplace"],
        cwd=REPO_ROOT,
        what="Cython build_ext --inplace",
    )
    if not expected_pyd.exists():
        raise RuntimeError(
            f"Cython 编译跑完但当前 ABI 产物不存在: {expected_pyd}"
        )
    print(f"[cython] ok -> {expected_pyd.name}")


def build_keygen() -> Path:
    """
    v0.3.0:构建独立 LicenseKeygen.exe(给开发者签发激活码用)。
    走 tools/license_keygen/keygen.spec,产物在 dist/LicenseKeygen/。
    """
    spec = REPO_ROOT / "tools" / "license_keygen" / "keygen.spec"
    if not spec.exists():
        raise RuntimeError(f"找不到 {spec},请确认仓库结构")
    print(f"[keygen] mode=keygen, platform={_detect_platform()}")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        str(spec),
    ]
    try:
        subprocess.run(cmd, cwd=str(REPO_ROOT), check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"keygen PyInstaller 失败: returncode={exc.returncode}\n"
            f"  cmd: {' '.join(cmd)}"
        ) from exc
    dist = REPO_ROOT / "dist" / "LicenseKeygen"
    if not dist.exists():
        raise RuntimeError(f"keygen build finished but {dist} missing")
    print(f"[keygen] ok -> {dist}")
    return dist


def build_frontend() -> None:
    """
    在 PyInstaller 之前重建 frontend/dist。

    v0.2.34 教训:packaging/doubao_manager.spec 只读 frontend/dist,**不重建**。
    如果开发改完 .vue/.ts 没手动 npm run build,exe 里就是旧 UI,新字段不会显示。
    这里强制 npm ci(只在 node_modules 缺失时) + npm run build,失败立刻中断打包,
    绝不允许带着陈旧的 dist 出包。
    """
    frontend = REPO_ROOT / "frontend"
    if not (frontend / "package.json").exists():
        raise RuntimeError(f"找不到 {frontend / 'package.json'},请确认仓库结构")
    npm = _resolve_npm()
    if not (frontend / "node_modules").exists():
        _run([npm, "ci", "--no-audit", "--prefer-offline"], cwd=frontend, what="frontend npm ci")
    else:
        print("[frontend] node_modules 已存在,跳过 npm ci")
    _run([npm, "run", "build"], cwd=frontend, what="frontend npm run build")
    dist_marker = frontend / "dist" / "index.html"
    if not dist_marker.exists():
        raise RuntimeError(
            f"frontend npm run build 跑完但 {dist_marker} 不存在,"
            "可能是 vite 配置改了输出路径或 build 静默失败"
        )
    print(f"[frontend] ok -> {dist_marker}")


def lift_up_browsers(dist: Path) -> None:
    """
    把仓库根的 ms-playwright/ 拷到 dist/<app>/ms-playwright/,
    对齐 packaging/runtime_hook.py 设的 PLAYWRIGHT_BROWSERS_PATH=<exe_dir>/ms-playwright。

    调用方负责在执行本脚本前设置 PLAYWRIGHT_BROWSERS_PATH=<repo>/ms-playwright,
    这样 `playwright install chromium` 就会把浏览器装到仓库根。
    """
    target = dist / "ms-playwright"
    if target.exists():
        print(f"[lift] 已有 {target}, 跳过")
        return
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH", "").strip()
    candidates: list[Path] = []
    if env and Path(env).exists():
        candidates.append(Path(env))
    repo_default = REPO_ROOT / "ms-playwright"
    if repo_default.exists():
        candidates.append(repo_default)
    # PyInstaller 偶发会把 browsers 打进 _internal/(取决于 collect_submodules)
    internal = dist / "_internal" / "ms-playwright"
    if internal.exists():
        candidates.append(internal)

    for src in candidates:
        try:
            print(f"[lift] copy {src} -> {target}")
            shutil.copytree(src, target)
            return
        except OSError as exc:
            print(f"[lift] 从 {src} 拷贝失败: {exc}")
            continue
    raise RuntimeError(
        "找不到可用的 ms-playwright 目录。请确认在执行本脚本前已设置 "
        "PLAYWRIGHT_BROWSERS_PATH=<repo>/ms-playwright 并运行 `playwright install chromium`。\n"
        f"  探测过: {candidates or '(空)'}\n"
        f"  期望目标: {target}"
    )


def build(mode: str = "onedir") -> Path:
    """调 PyInstaller 出包,返回产物根目录"""
    print(f"[build] mode={mode}, platform={_detect_platform()}")
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm",
        "--clean",
        str(REPO_ROOT / "packaging" / "doubao_manager.spec"),
    ]
    if mode == "onefile":
        env = os.environ.copy()
        env["DOUSTUDIO_ONEFILE"] = "1"
    else:
        env = None
    try:
        subprocess.run(cmd, cwd=str(REPO_ROOT), env=env, check=True)
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"PyInstaller 失败: returncode={exc.returncode}\n"
            f"  cmd: {' '.join(cmd)}\n"
            f"  cwd: {REPO_ROOT}\n"
            f"  (stderr/stdout 已由 PyInstaller 直接打印到上方日志)"
        ) from exc

    dist = _dist_dir(mode)
    if not dist.exists():
        raise RuntimeError(f"build finished but {dist} missing")
    print(f"[build] ok -> {dist}")
    return dist


def assert_compiled_verifier_in_dist(dist: Path) -> Path:
    """Fail the release if PyInstaller omitted the current-ABI verifier."""
    extension_suffix = sysconfig.get_config_var("EXT_SUFFIX")
    if not extension_suffix or not extension_suffix.endswith(".pyd"):
        raise RuntimeError(
            f"当前解释器没有 Windows .pyd 扩展后缀: {extension_suffix!r}"
        )
    expected_name = f"_license_verify{extension_suffix}"
    matches = [path for path in dist.rglob(expected_name) if path.is_file()]
    if not matches:
        bundled = sorted(path.name for path in dist.rglob("_license_verify*.pyd"))
        raise RuntimeError(
            "PyInstaller 产物缺少当前 ABI 的 compiled verifier: "
            f"{expected_name};发现={bundled or '(无)'}"
        )
    selected = matches[0]
    print(
        f"[cython] bundle verified -> {selected.relative_to(dist)} "
        f"sha256={_sha256(selected)}"
    )
    return selected


def package_zip(dist: Path, mode: str, version: str) -> Path:
    """把 dist 打成 zip + sha256,返回 zip 路径"""
    plat = _detect_platform()
    zip_name = f"DouStudio-{version}-{plat}.zip"
    zip_path = REPO_ROOT / "dist" / zip_name
    if zip_path.exists():
        zip_path.unlink()
    print(f"[zip] packing {zip_path.name} ...")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for root in dist.rglob("*"):
            if root.is_dir():
                continue
            rel = root.relative_to(dist.parent)
            zf.write(root, rel.as_posix())
    digest = _sha256(zip_path)
    sha_path = zip_path.with_suffix(".zip.sha256")
    sha_path.write_text(f"{digest}  {zip_path.name}\n", encoding="utf-8")
    print(f"[zip] {zip_path} ({zip_path.stat().st_size // 1024} KB)")
    print(f"[zip] sha256 = {digest}")
    return zip_path


def upload_release(zip_path: Path, sha_path: Path, version: str, repo: str, token: str) -> None:
    """
    用 GitHub REST API 上传 zip + sha256 到既有 release。
    - 优先 reuse 现有 release(同名 tag),否则新建
    - 不依赖 gh CLI(Windows runner 默认没装)
    - 自动用 sha256 做 deterministic-name
    """
    import json
    import urllib.request
    import urllib.error

    api = f"https://api.github.com/repos/{repo}"
    headers = {
        "Authorization": f"token {token}",
        "Accept": "application/vnd.github+json",
        "User-Agent": "DouStudio-Release/1.0",
    }

    def _req(method: str, url: str, body=None) -> dict | None:
        data = None if body is None else json.dumps(body).encode("utf-8")
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                raw = resp.read()
                if not raw or resp.status in (204, 304):
                    return None
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # 让调用方根据 HTTPError.code 自己决定怎么走(404 → 创建 release 等),
            # 不要在这里改异常类型,否则外部 except urllib.error.HTTPError 接不到。
            raise

    # 1. 找/建 release
    try:
        rel = _req("GET", f"{api}/releases/tags/{version}")
        release_id = rel["id"]
        upload_url = rel["upload_url"]
        print(f"[upload] reuse release {version} (id={release_id})")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise RuntimeError(f"GitHub releases/tags 失败: HTTP {exc.code}") from exc
        print(f"[upload] release {version} 不存在,创建")
        rel = _req(
            "POST", f"{api}/releases",
            {
                "tag_name": version,
                "name": f"DouStudio {version}",
                "body": (
                    "自动构建产物。SHA256 见同名 .sha256 文件。\n\n"
                    "## 验证\n\n"
                    "```bash\n"
                    "sha256sum -c DouStudio-{ver}-<plat>.zip.sha256\n"
                    "```"
                ).format(ver=version),
                "draft": False,
                "prerelease": False,
            },
        )
        release_id = rel["id"]
        upload_url = rel["upload_url"]

    # 2. 上传 zip + sha256(覆盖模式:先删同名旧 asset,再 POST)
    upload_base = upload_url.split("{", 1)[0]  # 去掉 {?name,label}

    # 列出现有 assets,删除同名的以便覆盖
    # GitHub API 正确路径:/repos/{owner}/{repo}/releases/assets/{asset_id}
    existing = _req("GET", f"{api}/releases/{release_id}/assets")
    if isinstance(existing, list):
        for asset in existing:
            if asset.get("name") in (zip_path.name, sha_path.name):
                old_id = asset["id"]
                print(f"[upload] 删除旧 asset {asset['name']} (id={old_id})")
                try:
                    _req("DELETE", f"{api}/releases/assets/{old_id}")
                except urllib.error.HTTPError as exc:
                    if exc.code == 404:
                        # 并发删除或已不存在,忽略
                        print(f"[upload] 旧 asset 已不存在 (404),忽略")
                        continue
                    raise

    for f in (zip_path, sha_path):
        size = f.stat().st_size
        name = f.name
        url = f"{upload_base}?name={urllib.parse.quote(name)}"
        print(f"[upload] {name} ({size} bytes) -> {url}")
        # v0.2.31:大文件(>200MB)上传 GitHub 偶发 ConnectionResetError / 写超时,
        # 加指数退避重试。zip + sha 各最多重试 5 次,每次间隔 2/4/8/16/32s。
        import time as _time
        data = f.read_bytes()
        attempt = 0
        last_exc: Exception | None = None
        while attempt < 5:
            try:
                req = urllib.request.Request(
                    url,
                    data=data,
                    method="POST",
                    headers={
                        **headers,
                        "Content-Type": "application/octet-stream",
                        "Content-Length": str(size),
                    },
                )
                with urllib.request.urlopen(req, timeout=600) as resp:
                    payload = json.loads(resp.read().decode("utf-8"))
                print(f"[upload] ok -> {payload.get('browser_download_url')}")
                last_exc = None
                break
            except (urllib.error.URLError, ConnectionResetError, TimeoutError) as exc:
                last_exc = exc
                attempt += 1
                wait = 2 ** attempt
                print(f"[upload] {name} 第 {attempt}/5 次失败: {exc};{wait}s 后重试", file=sys.stderr)
                _time.sleep(wait)
        if last_exc is not None:
            raise last_exc


def main() -> int:
    ap = argparse.ArgumentParser(description="DouStudio 打包脚本")
    ap.add_argument("--mode", choices=["onedir", "onefile"], default="onedir")
    ap.add_argument("--version", default="", help="版本号(如 v0.2.0)")
    ap.add_argument("--upload", action="store_true", help="打包后上传到 GitHub Release")
    ap.add_argument(
        "--upload-only",
        action="store_true",
        help="跳过 build/zip,只上传 dist/ 下既有的 zip + sha(用于重试上传)",
    )
    ap.add_argument(
        "--skip-frontend-build",
        action="store_true",
        help="跳过前端 npm run build(默认会自动跑)。spec 只读 frontend/dist,"
        "不重建会打包进陈旧的 UI(就是 v0.2.34 '字段不见了' 的根因)",
    )
    ap.add_argument(
        "--keygen",
        action="store_true",
        help="只构建 LicenseKeygen.exe(给开发者签发激活码的工具),不构建主程序",
    )
    args = ap.parse_args()

    version = args.version or os.environ.get("GITHUB_REF_NAME", "dev")
    if args.keygen:
        # keygen 不需要 verifier(只签发不验签),所以不跑 cython 编译 —— 这
        # 跟主程序分开,避免没 MSVC 的开发者卡在这里。打 keygen 时只需要
        # cryptography 能正常 import 即可。
        dist = build_keygen()
        print("[done] keygen 产物:")
        print(f"  - {dist}")
        return 0

    if args.upload_only:
        plat = _detect_platform()
        zip_path = REPO_ROOT / "dist" / f"DouStudio-{version}-{plat}.zip"
        sha_path = zip_path.with_suffix(".zip.sha256")
        if not (zip_path.exists() and sha_path.exists()):
            raise SystemExit(f"--upload-only 需要 {zip_path} 与 {sha_path} 存在")
        print(f"[upload-only] 跳过 build,直接用 {zip_path.name} ({zip_path.stat().st_size // 1024} KB)")
    else:
        compile_cython_extensions()
        if not args.skip_frontend_build:
            build_frontend()
        dist = build(args.mode)
        assert_compiled_verifier_in_dist(dist)
        if args.mode == "onedir":
            lift_up_browsers(dist)
        zip_path = package_zip(dist, args.mode, version)
        sha_path = zip_path.with_suffix(".zip.sha256")

    if args.upload:
        token = os.environ.get("GH_TOKEN", "")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if not (token and repo):
            print("[upload] 缺 GH_TOKEN 或 GITHUB_REPOSITORY,跳过上传")
        else:
            try:
                upload_release(zip_path, sha_path, version, repo, token)
            except Exception as exc:
                print(f"[upload] FAILED: {exc}", file=sys.stderr)
                raise

    print("[done] 产物:")
    print(f"  - {zip_path}")
    print(f"  - {sha_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
