"""
v0.3.1:服务端公钥 XOR 编码嵌入(跟 _embedded_pubkey.py 同样套路)。

server 端签响应(server_sig)用 KMS 里的 Ed25519 私钥,客户端用这里的
公钥验签 → 防 fake server(攻击者自建 server 返回 {ok:true} 客户端也信)。

如果 server_pubkey 没配(用户开发期 / 本地 mock),XOR 解码后为 b"",heartbeat
跳过 server_sig 校验(只发请求不验响应),但仍然记录 fresh_until。

生产部署时由 tools/license_keygen/scripts/embed_server_pubkey.py 把
server_public.key 转成这两段常量入仓。
"""
from __future__ import annotations

# XOR 编码后的 32 字节 server Ed25519 公钥(未配时全零 → 解码后空)
ENCRYPTED_SERVER_PUBKEY: bytes = bytes(32)

# XOR mask(32 字节),跟 ENCRYPTED_SERVER_PUBKEY 配对使用
XOR_SERVER_MASK: bytes = bytes(32)
