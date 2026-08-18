"""v0.3.1 license server: FastAPI 入口。

启动顺序:
  1. init_db()
  2. KmsAdapter() 加载口令保护的本地 Ed25519 私钥
  3. 注册 API 路由
  4. uvicorn 启动

调用方式:
  uvicorn app.main:app --host 0.0.0.0 --port 8443 --workers 2 \
    --ssl-keyfile /etc/doustudio/tls/key.pem \
    --ssl-certfile /etc/doustudio/tls/cert.pem

生产:systemd 托管,uvicorn 直接挂固定公网 IP 的自签证书。
"""
from __future__ import annotations

import logging

from fastapi import FastAPI

from .api import heartbeat as heartbeat_api
from .crypto.kms_adapter import KmsAdapter
from .storage.db import init_db

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("doustudio.license")


# 模块级 singleton(被 heartbeat_api.get_kms 引用)
init_db()
_kms_singleton = KmsAdapter()
logger.info("License server ready with encrypted local Ed25519 signer")


app = FastAPI(
    title="DouStudio License Server",
    version="0.3.1",
    description="v0.3.1 半在线心跳 — 客户端启动握手 + 后台心跳续期",
)

app.include_router(heartbeat_api.router)


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "version": "0.3.1"}
