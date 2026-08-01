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
import zipfile
from pathlib import Path


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
        # onedir → onefile: 让 spec 里 EXE 自包含(覆盖产物名)
        env = os.environ.copy()
        env["DOUSTUDIO_ONEFILE"] = "1"
        subprocess.check_call(cmd, cwd=str(REPO_ROOT), env=env)
    else:
        subprocess.check_call(cmd, cwd=str(REPO_ROOT))

    dist = _dist_dir(mode)
    if not dist.exists():
        raise RuntimeError(f"build finished but {dist} missing")
    print(f"[build] ok → {dist}")
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
    """用 GitHub CLI 触发 release + 上传(简单粗暴但够用)"""
    if not shutil.which("gh"):
        print("[upload] gh CLI 未安装,跳过上传")
        return
    cmd = [
        "gh", "release", "create", version,
        "--repo", repo,
        "--title", f"DouStudio {version}",
        "--notes", f"自动构建产物。SHA256 见 .sha256 文件。",
        str(zip_path),
        str(sha_path),
    ]
    print(f"[upload] running: gh release create {version} ...")
    env = os.environ.copy()
    env["GH_TOKEN"] = token
    subprocess.check_call(cmd, env=env)


def main() -> int:
    ap = argparse.ArgumentParser(description="DouStudio 打包脚本")
    ap.add_argument("--mode", choices=["onedir", "onefile"], default="onedir")
    ap.add_argument("--version", default="", help="版本号(如 v0.2.0)")
    ap.add_argument("--upload", action="store_true", help="打包后上传到 GitHub Release")
    args = ap.parse_args()

    dist = build(args.mode)
    version = args.version or os.environ.get("GITHUB_REF_NAME", "dev")
    zip_path = package_zip(dist, args.mode, version)
    sha_path = zip_path.with_suffix(".zip.sha256")

    if args.upload:
        token = os.environ.get("GH_TOKEN", "")
        repo = os.environ.get("GITHUB_REPOSITORY", "")
        if not (token and repo):
            print("[upload] 缺 GH_TOKEN 或 GITHUB_REPOSITORY,跳过上传")
        else:
            upload_release(zip_path, sha_path, version, repo, token)

    print("[done] 产物:")
    print(f"  - {zip_path}")
    print(f"  - {sha_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
