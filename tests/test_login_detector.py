"""v0.2.7:detector.py 只剩 DoubaoIdentity dataclass —— 全模块的 identity 容器契约。

完整 cookie + DOM + localStorage 三件套判定逻辑搬到 browser.py,所有 httpx 路径、
DoubaoLoginDetector、ResponseMeta、ACCOUNT_INFO_URL 都已下线,见 plan 文件
"Round 7: v0.2.7 — 抛弃 aegis 风控,沿 yaonieyo 双轨判定"。
"""
from doupool.login.detector import DoubaoIdentity


def test_doubao_identity_holds_user_id():
    identity = DoubaoIdentity(user_id="u-1")
    assert identity.user_id == "u-1"
    assert identity.nickname is None


def test_doubao_identity_holds_nickname():
    identity = DoubaoIdentity(user_id="u-2", nickname="张三")
    assert identity.user_id == "u-2"
    assert identity.nickname == "张三"


def test_doubao_identity_as_mapping_keys():
    identity = DoubaoIdentity(user_id="u-3", nickname="豆包用户")
    payload = identity.as_mapping()
    assert payload == {"user_id": "u-3", "nickname": "豆包用户"}


def test_doubao_identity_as_mapping_when_nickname_none():
    identity = DoubaoIdentity(user_id="u-4")
    assert identity.as_mapping() == {"user_id": "u-4", "nickname": None}


def test_doubao_identity_is_immutable():
    """repository.complete_login 依赖 identity 是不可变容器 —— frozen dataclass。"""
    identity = DoubaoIdentity(user_id="u-5")
    try:
        identity.user_id = "mutated"  # type: ignore[misc]
    except Exception:
        return
    raise AssertionError("DoubaoIdentity 应该不可变,但允许了属性赋值")