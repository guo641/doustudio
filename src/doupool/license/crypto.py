"""
v0.3.0:Ed25519 签发 + 验签的薄封装。

为什么 Ed25519 而不是 ECDSA / RSA:
  - 签名 / 公钥 / 私钥都是定长 64 / 32 / 32 字节,序列化简单
  - 不依赖随机数(ECDSA 私钥实现缺陷出过 Sony PS3 灾难)
  - verify 速度比 RSA 快 1-2 个数量级,启动时跑一次无感
  - cryptography PyCA 库支持完整,跨平台

verify_token_blob 是 verifer 调的核心:序列化后的 token(明文 payload +
签名) → 返回 bool。verifier 负责把签名部分拆出来,然后调这里 verify。
"""
from __future__ import annotations

import hashlib
import hmac
from typing import Tuple

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.serialization import (
    Encoding,
    NoEncryption,
    PrivateFormat,
    PublicFormat,
    load_pem_private_key,
)


def load_private_pem(pem_bytes: bytes) -> Ed25519PrivateKey:
    """读 PEM 格式私钥 —— 用于签发工具 developer_private.key。"""
    return load_pem_private_key(pem_bytes, password=None)


def private_to_pem(priv: Ed25519PrivateKey) -> bytes:
    """私钥 → PEM 字节 —— 仅签发工具初始化时使用,运行时无调用。"""
    return priv.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def public_raw(priv: Ed25519PrivateKey) -> bytes:
    """从私钥导出 32 字节 raw 公钥 —— 签发工具初始化时跑一次,生成 developer_public.key。"""
    return priv.public_key().public_bytes_raw()


def sign(private_pem_or_key, payload: bytes) -> bytes:
    """对 payload 签名,返 64 字节 raw 签名。

    private_pem_or_key 可以是 PEM 字节(读 .key 文件后直接传入)或
    已经 load 过的 Ed25519PrivateKey 对象(app.py 启动时 load 一次,
    之后走内存复用,避免每个请求都重读 + 重解析 PEM)。
    """
    if isinstance(private_pem_or_key, Ed25519PrivateKey):
        priv = private_pem_or_key
    else:
        priv = load_private_pem(private_pem_or_key)
    return priv.sign(payload)


def verify(public_key_32bytes: bytes, payload: bytes, signature_64bytes: bytes) -> bool:
    """用 32 字节 raw 公钥验签。失败 → InvalidSignature → False(不抛)。"""
    if len(public_key_32bytes) != 32:
        return False
    if len(signature_64bytes) != 64:
        return False
    try:
        pub = Ed25519PublicKey.from_public_bytes(public_key_32bytes)
        pub.verify(signature_64bytes, payload)
        return True
    except (InvalidSignature, ValueError):
        return False


def fingerprint_hmac_hex(public_key_32bytes: bytes, raw_fingerprint: str) -> str:
    """HMAC-SHA256(pubkey, raw_fingerprint) → 64 hex chars。

    防 fingerprint 在公开渠道泄露:签发时绑 HMAC 形式 fingerprint_hex 而不是
    raw fingerprint,拿到 raw 也无法构造有效 HMAC(没有 pubkey)。和
    fingerprint.hmac_hex 的区别:那里用 hex 化公钥作 key,这里直接用 raw 公钥
    —— 同一段 raw 公钥作 key,两段输出 bit-for-bit 等价。
    """
    return hmac.new(public_key_32bytes, raw_fingerprint.encode("utf-8"), hashlib.sha256).hexdigest()