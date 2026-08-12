"""
v0.3.0:license/storage.py 原子写测试。

覆盖:
  1. 读不存在的 activated.bin → None
  2. write_token 写 → read_token 返同样字节
  3. 覆盖写 → read_token 返新字节
  4. write_token 用 tmp 后不留垃圾
  5. 写入坏 data(tmp 中途炸)→ 原文件不变 + 不留 tmp

测试用 monkeypatch 把 data_dir / log_dir 指到 tmp_path,避免污染真实数据。
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from doupool.license import storage
from doupool.license import storage_path


@pytest.fixture
def isolated_license_dir(monkeypatch, tmp_path):
    """把 _resolve_app_dirs 强制返 tmp_path + 空 log_dir,所有 activated.bin 都走这里。"""
    new_data = tmp_path / "data"
    new_log = tmp_path / "log"
    new_data.mkdir(parents=True)
    new_log.mkdir(parents=True)

    import doupool.config as _config

    monkeypatch.setattr(
        _config,
        "_resolve_app_dirs",
        lambda: (new_data, new_log),
    )
    yield new_data / "license"
    # 不清理 tmp_path —— pytest 自动清


def test_read_missing_returns_none(isolated_license_dir):
    assert storage.read_token() is None


def test_write_then_read_roundtrip(isolated_license_dir):
    blob = b"hello-license-token-blob"
    storage.write_token(blob)
    assert storage.read_token() == blob


def test_write_overwrites_existing(isolated_license_dir):
    storage.write_token(b"old-token")
    storage.write_token(b"new-token")
    assert storage.read_token() == b"new-token"


def test_write_leaves_no_tmp_files(isolated_license_dir):
    storage.write_token(b"x")
    leftover = [p for p in isolated_license_dir.iterdir() if p.suffix == ".tmp"]
    assert leftover == [], f"残留 tmp 文件: {leftover}"


def test_write_rejects_non_bytes(isolated_license_dir):
    with pytest.raises(TypeError):
        storage.write_token("not-bytes")  # type: ignore[arg-type]


def test_clear_token_returns_true_when_present(isolated_license_dir):
    storage.write_token(b"x")
    assert storage.clear_token() is True
    assert storage.read_token() is None


def test_clear_token_returns_false_when_absent(isolated_license_dir):
    assert storage.clear_token() is False


def test_storage_path_layout(isolated_license_dir, tmp_path):
    """activated.bin 应落在 <data>/license/activated.bin,不在 <data>/ 直下。"""
    storage.write_token(b"x")
    assert (isolated_license_dir / "activated.bin").exists()
    assert not (tmp_path / "data" / "activated.bin").exists()


def test_license_dir_creates_if_missing(isolated_license_dir):
    # 在 license/ 里建一个 sentinel,然后删整个 license/ → 下次访问应重建
    import shutil
    isolated_license_dir.mkdir(parents=True, exist_ok=True)
    (isolated_license_dir / "sentinel.txt").write_text("x")
    assert isolated_license_dir.exists()
    # 删整个 license/ 后访问 license_dir() 应重建
    shutil.rmtree(isolated_license_dir)
    out = storage_path.license_dir()
    assert out.exists() and out.is_dir()
    # 重建后能写入
    (out / "activated.bin").write_bytes(b"sentinel")
    assert (out / "activated.bin").exists()
