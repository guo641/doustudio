"""v0.3.1 license server 配置。

所有可调常量集中在这里,部署时改环境变量。
"""
from __future__ import annotations

import os
from pathlib import Path


# 常量化参数(跟 client 端 v031-half-online-heartbeat memory 对齐)
FRESH_DAYS = 30          # 服务器签发 fresh_until = now + 30d
GRACE_DAYS = 7           # 客户端允许 offline 7 天
HEARTBEAT_INTERVAL_HOURS = 24
HANDSHAKE_TIMEOUT_SEC = 5  # 仅参考(client 端用)


# 服务端 Ed25519 私钥的解密口令。生产环境由 systemd EnvironmentFile 注入。
# 未配置时 KmsAdapter 必须拒绝启动,不得生成或加载明文 fallback。
SERVER_KEY_PASSPHRASE = os.environ.get("DOUSTUDIO_SERVER_KEY_PASSPHRASE", "")


# 数据库
DB_PATH = Path(os.environ.get("DOUSTUDIO_LICENSE_DB", "./doustudio_license.sqlite3"))


# 客户端公钥 (开发者签发激活码用的 Ed25519 公钥,32 bytes hex)
# 与 src/doupool/license/_embedded_pubkey.py 解码后等价。
# 这里单独存一份方便 server 端验签(不 import doupool,避免 PyInstaller bundle 副作用)。
CLIENT_PUBKEY_HEX = os.environ.get(
    "DOUSTUDIO_CLIENT_PUBKEY_HEX",
    "",  # 部署时必须配置;空值或非法值默认拒绝所有心跳请求
)


# 黑名单 HMAC 截断长度(8 字符 = 16 hex = 32 bit,碰撞概率 ~2^-32 忽略)
REVOKED_PREFIX_LEN = 16
