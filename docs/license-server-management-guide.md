# License Server 管理指南

**服务器**: `124.221.210.12` (腾讯云 CVM Ubuntu 24.04)  
**登录**: `ssh -i "C:/Users/Administrator/.ssh/doustudio_license" ubuntu@124.221.210.12`

---

## 📊 查看用户秘钥状态

### 1. 查看所有激活的 license

```bash
sudo -u doustudio sqlite3 /var/lib/doustudio/license.sqlite3 << 'EOF'
.mode column
.headers on
SELECT 
    substr(license_hmac, 1, 16) || '...' as hmac_prefix,
    substr(fingerprint_hex, 1, 16) || '...' as fingerprint,
    datetime(expires_at, 'unixepoch') as expires_at,
    datetime(first_seen_at, 'unixepoch') as first_seen,
    datetime(last_seen_at, 'unixepoch') as last_seen,
    heartbeat_count
FROM license_activations
ORDER BY last_seen_at DESC;
EOF
```

**输出示例**:
```
hmac_prefix       fingerprint       expires_at           first_seen           last_seen            heartbeat_count
----------------  ----------------  -------------------  -------------------  -------------------  ---------------
a1b2c3d4e5f6...   7890abcdef12...   2027-08-19 12:00:00  2026-08-19 10:00:00  2026-08-19 11:30:00  42
```

**字段说明**:
- `hmac_prefix`: license HMAC 前 16 字符（不是原始 token，无法反推用户）
- `fingerprint`: 硬件指纹 HMAC（前 16 字符）
- `expires_at`: 到期时间（UTC）— **NULL = 永久授权**
- `first_seen`: 首次心跳时间
- `last_seen`: 最近一次心跳时间
- `heartbeat_count`: 累计心跳次数

---

### 2. 查看特定用户的到期时间

**前提**: 你需要用户的 `license_token`（激活码）或 `fingerprint`（硬件指纹）

#### 方法 A: 通过 license_token 查询（推荐）

```bash
# 准备: 把用户的 license_token hex 保存到变量
LICENSE_TOKEN_HEX="<用户的 license_token 完整 hex>"

# SSH 到服务器后执行
sudo -u doustudio env \
  DOUSTUDIO_LICENSE_DB=/var/lib/doustudio/license.sqlite3 \
  /opt/doustudio/license-server/.venv/bin/python << EOF
import sys
sys.path.insert(0, '/opt/doustudio/license-server/server')
from app.crypto.kms_adapter import hmac_fingerprint_hex, license_token_to_pubkey
from app.storage.db import db_connection
import time

license_token = bytes.fromhex("${LICENSE_TOKEN_HEX}")
client_pubkey = license_token_to_pubkey(license_token)
if not client_pubkey:
    print("❌ license_token 格式错误")
    sys.exit(1)

# 查询数据库（需要 fingerprint，但我们只有 license_token）
# 实际上数据库存的是 hmac(client_pubkey, fingerprint)
# 如果不知道 fingerprint，无法直接查询
print("⚠️ 需要用户的 fingerprint 才能查询数据库")
print(f"client_pubkey: {client_pubkey.hex()}")
EOF
```

**问题**: 数据库存的是 `hmac(client_pubkey, fingerprint)`，如果你只有 `license_token` 没有 `fingerprint`，无法直接查询。

#### 方法 B: 查看所有记录，手动匹配（小规模部署）

```bash
sudo -u doustudio sqlite3 /var/lib/doustudio/license.sqlite3 << 'EOF'
.mode list
SELECT 
    license_hmac,
    fingerprint_hex,
    expires_at,
    datetime(expires_at, 'unixepoch') as expires_human,
    (expires_at - strftime('%s', 'now')) / 86400 as days_left
FROM license_activations
ORDER BY last_seen_at DESC;
EOF
```

**输出示例**:
```
a1b2c3d4...|7890abcdef...|1724068800|2024-08-19 12:00:00|365
...
```

---

### 3. 查看即将过期的用户（30 天内）

```bash
sudo -u doustudio sqlite3 /var/lib/doustudio/license.sqlite3 << 'EOF'
.mode column
.headers on
SELECT 
    substr(license_hmac, 1, 16) || '...' as hmac_prefix,
    datetime(expires_at, 'unixepoch') as expires_at,
    CAST((expires_at - strftime('%s', 'now')) / 86400.0 AS INTEGER) as days_left,
    datetime(last_seen_at, 'unixepoch') as last_seen
FROM license_activations
WHERE expires_at IS NOT NULL 
  AND expires_at > strftime('%s', 'now')
  AND (expires_at - strftime('%s', 'now')) < 2592000  -- 30 天
ORDER BY expires_at ASC;
EOF
```

---

### 4. 查看已过期但仍在心跳的用户（grace period 内）

```bash
sudo -u doustudio sqlite3 /var/lib/doustudio/license.sqlite3 << 'EOF'
.mode column
.headers on
SELECT 
    substr(license_hmac, 1, 16) || '...' as hmac_prefix,
    datetime(expires_at, 'unixepoch') as expired_at,
    datetime(last_seen_at, 'unixepoch') as last_seen,
    CAST((strftime('%s', 'now') - last_seen_at) / 3600.0 AS INTEGER) as hours_since_last_seen
FROM license_activations
WHERE expires_at IS NOT NULL 
  AND expires_at < strftime('%s', 'now')  -- 已过期
  AND (strftime('%s', 'now') - last_seen_at) < 604800  -- 最近 7 天仍在心跳
ORDER BY last_seen_at DESC;
EOF
```

---

## 🔧 用户秘钥管理

### 1. 撤销用户秘钥（加入黑名单）

**场景**: 用户退款、违规、或主动注销

**前提**: 你需要用户的 `license_token` 和 `fingerprint`

```bash
# SSH 到服务器
ssh -i "C:/Users/Administrator/.ssh/doustudio_license" ubuntu@124.221.210.12

# 执行撤销命令
sudo -u doustudio env \
  DOUSTUDIO_LICENSE_DB=/var/lib/doustudio/license.sqlite3 \
  /opt/doustudio/license-server/.venv/bin/python \
  /opt/doustudio/license-server/server/scripts/revoke.py \
  --license-token '<用户的 license_token hex>' \
  --fingerprint '<用户的 64-hex fingerprint>' \
  --reason '用户退款'
```

**输出**:
```
✅ 已撤销 license HMAC 前缀: a1b2c3d4e5f6
   完整 HMAC: a1b2c3d4e5f6...
```

**验证撤销**:
```bash
sudo -u doustudio sqlite3 /var/lib/doustudio/license.sqlite3 \
  'SELECT prefix, datetime(revoked_at, "unixepoch"), reason FROM revoked_license_hmac_prefixes ORDER BY revoked_at DESC LIMIT 5;'
```

---

### 2. 查看所有已撤销的 license

```bash
sudo -u doustudio sqlite3 /var/lib/doustudio/license.sqlite3 << 'EOF'
.mode column
.headers on
SELECT 
    prefix,
    datetime(revoked_at, 'unixepoch') as revoked_at,
    reason
FROM revoked_license_hmac_prefixes
ORDER BY revoked_at DESC;
EOF
```

---

### 3. 撤销后用户会发生什么？

1. **下次心跳时**: 服务器返回 `{"ok": false, "revoked_prefixes": ["a1b2c3..."]}`
2. **客户端行为**: 
   - `verifier.pyx` 检测到 revoked prefix
   - `get_activation_status()` 返回 `"expired"`
   - 闸门 `verify_at_import` 拒绝启动
3. **时间**: 最多 24 小时生效（心跳间隔）

---

### 4. 延长用户到期时间（手动修改）

**⚠️ 危险操作** — 直接修改数据库，仅在紧急情况下使用

```bash
# 场景: 将某个 license 的到期时间延长 1 年
sudo -u doustudio sqlite3 /var/lib/doustudio/license.sqlite3 << 'EOF'
UPDATE license_activations
SET expires_at = strftime('%s', 'now') + 31536000  -- 当前时间 + 1 年 (秒)
WHERE license_hmac = '<完整的 license_hmac>';
EOF
```

**验证修改**:
```bash
sudo -u doustudio sqlite3 /var/lib/doustudio/license.sqlite3 \
  "SELECT license_hmac, datetime(expires_at, 'unixepoch') FROM license_activations WHERE license_hmac = '<hmac>';"
```

---

### 5. 删除僵尸激活记录（清理测试数据）

**场景**: 清理超过 90 天未心跳的记录

```bash
sudo -u doustudio sqlite3 /var/lib/doustudio/license.sqlite3 << 'EOF'
DELETE FROM license_activations
WHERE (strftime('%s', 'now') - last_seen_at) > 7776000;  -- 90 天 = 90*24*3600
EOF
```

**验证删除**:
```bash
sudo -u doustudio sqlite3 /var/lib/doustudio/license.sqlite3 \
  "SELECT COUNT(*) as total_activations FROM license_activations;"
```

---

## 📈 统计报表

### 1. 活跃用户数（最近 7 天）

```bash
sudo -u doustudio sqlite3 /var/lib/doustudio/license.sqlite3 << 'EOF'
SELECT COUNT(*) as active_users_7d
FROM license_activations
WHERE (strftime('%s', 'now') - last_seen_at) < 604800;
EOF
```

---

### 2. 总激活数 vs 活跃数

```bash
sudo -u doustudio sqlite3 /var/lib/doustudio/license.sqlite3 << 'EOF'
.mode column
.headers on
SELECT 
    (SELECT COUNT(*) FROM license_activations) as total_activations,
    (SELECT COUNT(*) FROM license_activations WHERE (strftime('%s', 'now') - last_seen_at) < 86400) as active_24h,
    (SELECT COUNT(*) FROM license_activations WHERE (strftime('%s', 'now') - last_seen_at) < 604800) as active_7d,
    (SELECT COUNT(*) FROM revoked_license_hmac_prefixes) as revoked_count;
EOF
```

---

### 3. 心跳频率分布

```bash
sudo -u doustudio sqlite3 /var/lib/doustudio/license.sqlite3 << 'EOF'
.mode column
.headers on
SELECT 
    CASE 
        WHEN (strftime('%s', 'now') - last_seen_at) < 3600 THEN '< 1 hour'
        WHEN (strftime('%s', 'now') - last_seen_at) < 86400 THEN '< 1 day'
        WHEN (strftime('%s', 'now') - last_seen_at) < 604800 THEN '< 7 days'
        WHEN (strftime('%s', 'now') - last_seen_at) < 2592000 THEN '< 30 days'
        ELSE '> 30 days'
    END as last_seen_bucket,
    COUNT(*) as user_count
FROM license_activations
GROUP BY last_seen_bucket
ORDER BY MIN(strftime('%s', 'now') - last_seen_at);
EOF
```

---

## 🚨 问题排查

### 1. 用户说"无法激活"或"心跳失败"

**检查服务器日志**:
```bash
sudo journalctl -u doustudio-license.service -f --since "10 minutes ago"
```

**查看最近的心跳请求**:
```bash
sudo journalctl -u doustudio-license.service --since "1 hour ago" | grep "POST /api/heartbeat"
```

---

### 2. 数据库被锁（concurrent write）

**SQLite 默认不支持高并发写**，但心跳是读多写少（只 upsert `last_seen_at`），2 个 worker 通常够用。

**如果遇到**:
```
sqlite3.OperationalError: database is locked
```

**解决方案**:
1. 减少 uvicorn workers（systemd 里改 `--workers 1`）
2. 或者迁移到 PostgreSQL（需改 `db.py` + `requirements.txt`）

---

### 3. 查看服务器签名私钥状态

```bash
# 检查私钥文件权限
ls -l /opt/doustudio/license-server/server/scripts/server_signing_key.pem

# 验证私钥可解密（需要 passphrase）
sudo -u doustudio env \
  DOUSTUDIO_SERVER_KEY_PASSPHRASE="$(sudo cat /etc/doustudio/license.env | grep PASSPHRASE | cut -d'=' -f2 | tr -d "'")" \
  /opt/doustudio/license-server/.venv/bin/python << 'EOF'
import os
from cryptography.hazmat.primitives.serialization import load_pem_private_key
path = "/opt/doustudio/license-server/server/scripts/server_signing_key.pem"
passphrase = os.environ["DOUSTUDIO_SERVER_KEY_PASSPHRASE"].encode()
with open(path, "rb") as f:
    key = load_pem_private_key(f.read(), password=passphrase)
print(f"✅ 私钥可解密: {key.public_key().public_bytes_raw().hex()[:32]}...")
EOF
```

---

## 🔐 安全建议

1. **定期备份数据库**:
   ```bash
   sudo cp /var/lib/doustudio/license.sqlite3 \
           /var/lib/doustudio/license.sqlite3.backup-$(date +%Y%m%d)
   ```

2. **定期审计撤销记录**:
   - 检查是否有异常撤销（误操作）
   - 确认 `reason` 字段清晰记录原因

3. **监控活跃用户数突降**:
   - 如果 7 天活跃数突然下降 > 50% → 可能服务器异常或客户端 bug

4. **不要在数据库里存敏感信息**:
   - ✅ 存 HMAC（不可逆）
   - ❌ 不存 license_token 明文
   - ❌ 不存 fingerprint 明文

---

## 📝 待实现功能（可选）

如果需要更强的管理能力，可以考虑：

1. **Web 管理后台**（需额外开发）:
   - 查看所有用户、到期时间、心跳状态
   - 一键撤销/延期
   - 统计报表

2. **自动到期提醒**:
   - 定时任务查询即将过期的用户
   - 发邮件/短信通知

3. **审计日志表**:
   - 记录所有撤销操作的操作人、时间、原因
   - 防止误操作或内部滥用

4. **PostgreSQL 迁移**:
   - SQLite 适合小规模（< 1000 用户）
   - 大规模部署建议用 PostgreSQL

---

**总结**: 当前管理方式是 **SSH + SQLite CLI**，适合小规模部署。如需更友好的界面，可以开发 Web 管理后台或集成到现有的后台系统。
