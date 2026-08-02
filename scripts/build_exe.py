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
        req = urllib.request.Request(
            url,
            data=f.read_bytes(),
            method="POST",
            headers={
                **headers,
                "Content-Type": "application/octet-stream",
                "Content-Length": str(size),
            },
        )
        with urllib.request.urlopen(req, timeout=300) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        print(f"[upload] ok -> {payload.get('browser_download_url')}")


def main() -> int:
    ap = argparse.ArgumentParser(description="DouStudio 打包脚本")
    ap.add_argument("--mode", choices=["onedir", "onefile"], default="onedir")
    ap.add_argument("--version", default="", help="版本号(如 v0.2.0)")
    ap.add_argument("--upload", action="store_true", help="打包后上传到 GitHub Release")
    args = ap.parse_args()

    dist = build(args.mode)
    if args.mode == "onedir":
        lift_up_browsers(dist)
    version = args.version or os.environ.get("GITHUB_REF_NAME", "dev")
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
