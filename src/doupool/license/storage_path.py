"""
v0.3.0:激活信息落盘路径解析 %LOCALAPPDATA%\\DouStudio\\license\\activated.bin 。

复用 doupool.config._resolve_app_dirs 的 data_dir,但保留独立的 license/ 子
目录,便于:① 备份/迁移只拷 activated.bin ② 用户手动清除授权时只删这个文件夹
即可,不会误删数据库 / 视频 / 日志。

不在 data_dir/ 直下放 activated.bin 是为了避免和 settings.json / accounts/
db 混在一起 —— 离线工具易激活 → 失败 → 删 license/ 文件夹是更放心的恢复路径。

注意:_resolve_app_dirs 用模块级属性查找 `doupool.config._resolve_app_dirs()`,
而不是 `from doupool.config import _resolve_app_dirs`,这样测试 monkeypatch
_config._resolve_app_dirs 时本模块也会被影响(import 时锁定函数引用会让
monkeypatch 失效)。
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import doupool.config as _config


def license_dir() -> Path:
    """%LOCALAPPDATA%\\DouStudio\\license\\ —— 首次访问时创建,不抛错。"""
    data_dir, _ = _config._resolve_app_dirs()
    out = Path(data_dir) / "license"
    out.mkdir(parents=True, exist_ok=True)
    return out


def activated_bin_path() -> Path:
    """主要持久化文件 —— 激活成功的 token(签名 + payload + fingerprint)以二进制存。"""
    return license_dir() / "activated.bin"


def tmp_activated_bin_path() -> Path:
    """原子写用的 tmpfile 路径。和 activated.bin 同目录,便于 Path.replace 原子替换。"""
    fd, name = tempfile.mkstemp(
        prefix="activated.",
        suffix=".bin.tmp",
        dir=str(license_dir()),
    )
    os.close(fd)
    return Path(name)
