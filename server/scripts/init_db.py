"""v0.3.1: 初始化 license server 数据库。"""
from __future__ import annotations

import sys
from pathlib import Path

# 允许从 server/ 目录跑: python scripts/init_db.py
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.storage.db import init_db  # noqa: E402


if __name__ == "__main__":
    init_db()
    print(f"数据库已初始化")