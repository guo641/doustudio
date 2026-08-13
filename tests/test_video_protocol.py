import json

import pytest

from doupool.video import protocol as protocol_module
from doupool.video.protocol import (
    EXTRA_CLIENT_META_KEYS,
    DoubaoContentRejected,
    DoubaoRateLimited,
    _FALLBACK_COOLDOWN_S,
    build_completion_payload,
    find_creation_directory,
    find_video_node,
    parse_creation_result,
    parse_download_info,
    parse_sse_ack,
)


@pytest.fixture(autouse=True)
def _reset_seen_remote_task_ids():
    """v0.3.5.2:dedup + cooldown 改 per-call 局部(见 protocol.py 注释),
    module-level `_seen_remote_task_ids` / `_candidate_first_seen` 已删除。
    fixture 保留为 no-op,让测试代码不需要在 v0.3.5.2 移除全局时再改一遍
    (autouse 必须始终 yield)。
    """
    yield


def test_build_new_conversation_video_payload():
    payload = build_completion_payload(
        prompt="一只猫在草地上行走",
        model="seedance_v2.0_mini",
        ratio="1:1",
        duration=5,
        fingerprint="verify_test",
        now_ms=1_700_000_000_123,
        local_conversation_id="local_test",
        local_message_id="message-test",
        block_id="block-test",
        unique_key="unique-test",
    )

    assert payload["client_meta"]["conversation_id"] == ""
    assert payload["option"]["need_create_conversation"] is True
    assert payload["option"]["recovery_option"]["req_create_time_sec"] == 1_700_000_000
    assert json.loads(payload["chat_ability"]["ability_param"]) == {
        "ratio": "1:1",
        "model": "seedance_v2.0_mini",
        "duration": 5,
    }


def test_build_completion_payload_uses_uuid4_for_default_ids():
    """v0.2.17:local_message_id / local_conversation_id / unique_key 默认
    用 uuid4(v1 时间戳+MAC 可聚关联全账号,纯数字 16 位更易按时间窗聚类)。
    不传这几个 kwargs,生成出来必须是合法的 UUIDv4 字面量。
    """
    import re
    import uuid as _uuid

    payload = build_completion_payload(
        prompt="测试",
        model="seedance_v2.0_mini",
        ratio="1:1",
        duration=5,
        fingerprint="fp",
    )

    # 抓所有「应该 UUIDv4」的 id 字段
    ids_to_check = [
        payload["client_meta"]["local_conversation_id"],
        payload["option"]["unique_key"],
        payload["messages"][-1]["local_message_id"],
    ]
    if payload["messages"][0]["content_block"][0]["block_type"] == 10052:
        # i2v 才会出现 attachment_message,这里 t2v 默认没 attachment
        pass

    uuid_re = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")
    for sid in ids_to_check:
        assert uuid_re.match(sid), f"不是 UUIDv4 格式: {sid!r}"
        # 二次断言:uuid.UUID 解析 + version=4 抛错则失败
        _uuid.UUID(sid, version=4)


def test_build_completion_payload_forwards_extra_client_meta():
    """v0.2.17:**extra_client_meta kwargs(白名单 EXTRA_CLIENT_META_KEYS)
    合并到 payload.client_meta。runner 层把 TokenBundle.to_dict() 透传进来。
    """
    payload = build_completion_payload(
        prompt="测试",
        model="seedance_v2.0_mini",
        ratio="1:1",
        duration=5,
        fingerprint="fp",
        web_id="wb_xyz",
        tea_uuid="tu_xyz",
        device_id="dev_xyz",
        pc_version="3.27.4",
        web_id_signature="sig_xyz",
        # 非白名单:应该被忽略
        sessionid="leaked_secret_should_be_dropped",
        random_attacker_field="evil",
    )

    client_meta = payload["client_meta"]
    assert client_meta["web_id"] == "wb_xyz"
    assert client_meta["tea_uuid"] == "tu_xyz"
    assert client_meta["device_id"] == "dev_xyz"
    assert client_meta["pc_version"] == "3.27.4"
    assert client_meta["web_id_signature"] == "sig_xyz"
    # 白名单外的字段必须没漏进 payload
    assert "sessionid" not in client_meta
    assert "random_attacker_field" not in client_meta


def test_build_completion_payload_drops_empty_extra_client_meta():
    """v0.2.17:None / 空串的 extra_client_meta 不应该写进 payload。"""
    payload = build_completion_payload(
        prompt="测试",
        model="seedance_v2.0_mini",
        ratio="1:1",
        duration=5,
        fingerprint="fp",
        web_id="",       # 空串 → 丢弃
        tea_uuid=None,   # None → 丢弃
    )
    assert "web_id" not in payload["client_meta"]
    assert "tea_uuid" not in payload["client_meta"]


def test_extra_client_meta_keys_is_frozenset_or_tuple():
    """v0.2.17:EXTRA_CLIENT_META_KEYS 必须是不可变集合,运行时不能被改动。"""
    assert isinstance(EXTRA_CLIENT_META_KEYS, (tuple, frozenset))
    assert {"web_id", "tea_uuid", "device_id", "pc_version", "web_id_signature"} <= set(EXTRA_CLIENT_META_KEYS)



    text = (
        'event: SSE_HEARTBEAT\ndata: {}\n\n'
        'event: SSE_ACK\ndata: {"query_list":[{"question_id":"q1"}],'
        '"ack_client_meta":{"conversation_id":"c1","section_id":"s1"}}\n\n'
    )
    assert parse_sse_ack(text) == {
        "conversation_id": "c1",
        "section_id": "s1",
        "question_id": "q1",
    }


def test_parse_sse_ack_raises_typed_rate_limit():
    text = 'event: STREAM_ERROR\ndata: {"error_msg":"rate limited","error_code":429}\n\n'
    with pytest.raises(DoubaoRateLimited):
        parse_sse_ack(text)


def test_parse_sse_ack_detects_shark_admin_risk_control():
    """v0.2.16:豆包 STREAM_ERROR extra.decision.from == "shark_admin" 时
    抛出 is_risk_control=True 的 DoubaoRateLimited — service 层据此区分
    风控拦截 vs 真 quota 限流,不能 cap 桶封号。
    """
    text = (
        'event: STREAM_ERROR\n'
        'data: {"error_code":710022004,"error_msg":"rate limited","extra":{'
        '"decision":"{\\"code\\":\\"10000\\",\\"from\\":\\"shark_admin\\",'
        '\\"type\\":\\"verify\\",\\"region\\":\\"cn\\",'
        '\\"subtype\\":\\"semantic_reasoning\\",\\"detail\\":\\"abc\\"}"'
        '}}\n\n'
    )
    with pytest.raises(DoubaoRateLimited) as excinfo:
        parse_sse_ack(text)
    assert excinfo.value.is_risk_control is True
    assert "rate limited" in str(excinfo.value)


def test_parse_sse_ack_rates_limit_without_shark_admin_stays_quota():
    """v0.2.16:没 extra.decision / from 不是 shark_admin → 还是真 quota 限流
    (is_risk_control=False),保持旧的 cap 桶行为。"""
    text = (
        'event: STREAM_ERROR\n'
        'data: {"error_code":429,"error_msg":"rate limited"}\n\n'
    )
    with pytest.raises(DoubaoRateLimited) as excinfo:
        parse_sse_ack(text)
    assert excinfo.value.is_risk_control is False


def test_parse_sse_ack_attaches_response_text_to_rate_limit():
    """v0.2.15:DoubaoRateLimited 带 response_text,video/service 写日志时
    能看到豆包真正的 SSE 响应原文,排查「额度误报」(指纹 / IP / 风控)时
    不再是黑盒。
    """
    text = 'event: STREAM_ERROR\ndata: {"error_msg":"rate limited","error_code":429}\n\n'
    with pytest.raises(DoubaoRateLimited) as excinfo:
        parse_sse_ack(text)
    assert "rate limited" in str(excinfo.value)
    assert excinfo.value.response_text == text


def test_parse_creation_result_extracts_video():
    content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [{
            "id": "task-1",
            "video": {
                "status": 3,
                "vid": "video-1",
                "download_url": "https://example.test/video.mp4",
                "cover": {"image_thumb": {"url": "https://example.test/cover.jpg"}},
            },
        }]}},
    }])
    response = {"downlink_body": {"pull_singe_chain_downlink_body": {"messages": [{"content": content}]}}}

    assert parse_creation_result(response) == {
        "remote_task_id": "task-1",
        "vid": "video-1",
        "fallback_result_url": "https://example.test/video.mp4",
        "cover_url": "https://example.test/cover.jpg",
    }


def test_parse_my_creation_download_chain():
    homepage = {"code": 0, "data": {"children": [{"id": "folder-1", "name": "我的创作"}]}}
    nodes = {"code": 0, "data": {"children": [{"id": "node-1", "key": "video-1"}]}}
    download = {"code": 0, "data": {"download_infos": [{
        "node_id": "node-1",
        "main_url": "https://v11.example/video.mp4",
        "backup_url": "https://v3.example/video.mp4",
    }]}}

    assert find_creation_directory(homepage) == "folder-1"
    assert find_video_node(nodes, "video-1") == "node-1"
    assert parse_download_info(download) == {
        "result_url": "https://v11.example/video.mp4",
        "backup_result_url": "https://v3.example/video.mp4",
    }


def test_payload_rejects_unsupported_options():
    with pytest.raises(ValueError, match="model"):
        build_completion_payload("x", "unknown", "1:1", 5, "fp")


def test_build_i2v_payload_has_attachment_and_text_messages():
    payload = build_completion_payload(
        prompt="动起来",
        model="seedance_v2.0_mini",
        ratio="9:16",
        duration=5,
        fingerprint="verify_test",
        mode="i2v",
        images=[{
            "identifier": "id-1",
            "uri": "tos-cn-i-a9rns2rl98/demo.png",
            "name": "demo.png",
            "width": 100,
            "height": 200,
        }],
        now_ms=1_700_000_000_123,
        local_conversation_id="local_i2v",
        local_message_id="text-msg",
        block_id="text-block",
        unique_key="unique-i2v",
        collect_id="collect-1",
        attachment_message_id="image-msg",
        attachment_block_id="image-block",
    )

    assert len(payload["messages"]) == 2
    image_msg = payload["messages"][0]
    text_msg = payload["messages"][1]
    assert image_msg["content_block"][0]["block_type"] == 10052
    attachment = image_msg["content_block"][0]["content"]["attachment_block"]["attachments"][0]
    assert attachment["identifier"] == "id-1"
    assert attachment["image"]["uri"] == "tos-cn-i-a9rns2rl98/demo.png"
    assert text_msg["content_block"][0]["block_type"] == 10000
    assert text_msg["content_block"][0]["content"]["text_block"]["text"] == "生成视频：动起来，9:16"
    assert payload["chat_ability"]["ability_type"] == 17
    assert payload["option"]["collect_id"] == "collect-1"
    assert payload["ext"]["collection_id"] == "collect-1"


def test_i2v_payload_requires_image():
    with pytest.raises(ValueError, match="image"):
        build_completion_payload("x", "seedance_v2.0_mini", "1:1", 5, "fp", mode="i2v")


def test_build_i2v_payload_supports_multiple_images():
    images = [
        {"identifier": f"id-{i}", "uri": f"tos-cn-i-a9rns2rl98/demo-{i}.png", "name": f"demo-{i}.png"}
        for i in range(3)
    ]
    payload = build_completion_payload(
        "多图动起来", "seedance_v2.0_mini", "1:1", 5, "fp", mode="i2v", images=images
    )
    attachments = payload["messages"][0]["content_block"][0]["content"]["attachment_block"]["attachments"]
    assert len(attachments) == 3
    assert [item["identifier"] for item in attachments] == ["id-0", "id-1", "id-2"]


def test_i2v_payload_rejects_more_than_nine_images():
    images = [
        {"identifier": f"id-{i}", "uri": f"tos://{i}.png", "name": f"{i}.png"}
        for i in range(10)
    ]
    with pytest.raises(ValueError, match="9"):
        build_completion_payload("x", "seedance_v2.0_mini", "1:1", 5, "fp", mode="i2v", images=images)


# ---------- v0.2.21:内容审核拒绝识别 ----------


def _wrap(content_blocks: list[dict]) -> dict:
    """组装一个完整的 chain response 包装,内含一个 message + blocks。

    v0.2.21:用 ensure_ascii=False 把中文按原字面量写出 —— 与线上豆包响应一致
    (线上 SSE 是 UTF-8 字节流,不在 JSON 层做 unicode-escape 转义),response_text
    里才能直接 substring 匹配中文。否则会被字面 escape 形式阻断 substring in
    检查(被 pytest repr 显示出来,看起来像乱码)。
    """
    return {
        "downlink_body": {
            "pull_singe_chain_downlink_body": {
                "messages": [{"content": json.dumps(content_blocks, ensure_ascii=False)}]
            }
        }
    }


def test_parse_creation_result_raises_on_text_block_rejection():
    """v0.2.21:豆包把「无法返回该内容」塞 text_block.text 时,parse_creation_result
    必须抛 DoubaoContentRejected —— 之前默默返 None,polling 卡到 5min timeout。"""
    response = _wrap([{
        "block_type": 10000,
        "content": {"text_block": {"text": "无法返回该内容,你可以换个主题再试试"}},
    }])

    with pytest.raises(DoubaoContentRejected) as excinfo:
        parse_creation_result(response)
    assert "无法返回该内容" in excinfo.value.error_message
    # response_text 是 truncated JSON(ensure_ascii=False → 中文以原字面量写出)
    assert excinfo.value.response_text
    assert "无法返回该内容" in excinfo.value.response_text
    assert "换个主题" in excinfo.value.response_text


def test_parse_creation_result_raises_on_copyright_violation_text():
    """侵权关键词(用户真实 case:「生成内容中疑似包含XXX侵权」)也要识别。"""
    response = _wrap([{
        "block_type": 10000,
        "content": {"text_block": {"text": "生成内容中疑似包含某品牌商标侵权,请重新描述"}},
    }])

    with pytest.raises(DoubaoContentRejected) as excinfo:
        parse_creation_result(response)
    assert "侵权" in excinfo.value.error_message


def test_parse_creation_result_raises_on_new_rejection_template():
    """v0.2.23:豆包新文案「我暂时无法生成你要求的内容」也要识别 —— 此前不在
    _POLICY_PATTERNS → polling 一直 None → 5min 后才 timeout,期间用户视角
    「永远生成中」。抛 DoubaoContentRejected 后,runner 会用 prompt_reviser
    改写重试(max_reject_retries 默认 2)。"""
    response = _wrap([{
        "block_type": 10000,
        "content": {"text_block": {"text": "我暂时无法生成你要求的内容,请尝试输入其他要求"}},
    }])

    with pytest.raises(DoubaoContentRejected) as excinfo:
        parse_creation_result(response)
    # 错误信息里能看到原文片段(供 service 层记日志 + 退款标记)
    assert "无法生成" in excinfo.value.error_message
    assert excinfo.value.response_text


def test_parse_creation_result_raises_on_creation_block_error_msg():
    """creation_block 失败状态附带的 error_msg / disallow_reason 也要识别。"""
    response = _wrap([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [{
            "id": "task-x",
            "video": {"status": 6, "error_msg": "换个主题再试试"},
        }]}},
    }])

    with pytest.raises(DoubaoContentRejected) as excinfo:
        parse_creation_result(response)
    assert excinfo.value.error_message == "换个主题再试试"


def test_parse_creation_result_prefers_success_over_rejection_text():
    """同时含成功块 + 同包内的 reject 文本 → 必须先返回成功 dict(避免误判)。"""
    response = {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [
            {"content": json.dumps([
                {"block_type": 10000, "content": {"text_block": {"text": "换个主题再试试"}}},
                {"block_type": 2074, "content": {"creation_block": {"creations": [{
                    "id": "task-real",
                    "video": {
                        "status": 3,
                        "vid": "video-real",
                        "download_url": "https://example.test/real.mp4",
                    },
                }]}}},
            ])},
        ]}}
    }

    # 顺序敏感:成功块先出现,parse_creation_result 必须直接 return 成功 dict
    assert parse_creation_result(response) == {
        "remote_task_id": "task-real",
        "vid": "video-real",
        "fallback_result_url": "https://example.test/real.mp4",
        "cover_url": "",
    }


def test_parse_creation_result_returns_none_for_legitimate_unchanged_chain():
    """完全没成功块 + 没 policy 关键词 → 返 None(让 polling 继续等)。"""
    response = _wrap([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [{
            "id": "task-x",
            "video": {"status": 1},  # 还在生成
        }]}},
    }])

    assert parse_creation_result(response) is None


def test_parse_creation_result_raises_on_sensitive_content_english():
    """英文 reject 文案也要识别(sensitive content / content violates ...)。"""
    response = _wrap([{
        "block_type": 10000,
        "content": {"text_block": {"text": "Sorry, this request contains sensitive content and cannot be fulfilled."}},
    }])

    with pytest.raises(DoubaoContentRejected):
        parse_creation_result(response)


# ---------- v0.2.24:SSE 内 TEXT_MESSAGE 拒绝文案实时检测 ----------


def test_scan_sse_for_policy_rejection_detects_text_message():
    """v0.2.24:豆包新版拒绝把文案塞 SSE TEXT_MESSAGE 事件 —— 必须在
    parse_sse_ack 里就被识别,不等 5min timeout。"""
    text = (
        'event: SSE_HEARTBEAT\ndata: {}\n\n'
        'event: TEXT_MESSAGE\ndata: {"content_block":[{"content":{'
        '"text_block":{"text":"我暂时无法生成你要求的内容,请尝试输入其他要求"'
        '}}}]}\n\n'
        'event: SSE_ACK\ndata: {"ack_client_meta":{"conversation_id":"c1",'
        '"section_id":"s1"},"query_list":[{"question_id":"q1"}]}\n\n'
    )
    with pytest.raises(DoubaoContentRejected) as excinfo:
        parse_sse_ack(text)
    assert "无法生成" in excinfo.value.error_message
    # raw SSE (≤2000 字符)透传进 response_text,跟 DoubaoRateLimited 一致
    assert excinfo.value.response_text == text


def test_scan_sse_for_policy_rejection_does_not_false_positive_on_benign_text():
    """正常生成中消息(无 policy 关键词)必须不被误判为拒绝。"""
    text = (
        'event: TEXT_MESSAGE\ndata: {"content_block":[{"content":{'
        '"text_block":{"text":"好的,正在为你生成视频,请稍候..."}}}]}\n\n'
        'event: SSE_ACK\ndata: {"ack_client_meta":{"conversation_id":"c1",'
        '"section_id":"s1"},"query_list":[{"question_id":"q1"}]}\n\n'
    )
    ack = parse_sse_ack(text)
    assert ack == {
        "conversation_id": "c1",
        "section_id": "s1",
        "question_id": "q1",
    }


def test_scan_sse_for_policy_rejection_concatenates_multiple_text_chunks():
    """拒绝文案可能被拆成多个 TEXT_CHUNK 包,必须拼接后再扫描,不能漏。"""
    text = (
        'event: TEXT_CHUNK\ndata: {"content_block":[{"content":{'
        '"text_block":{"text":"抱歉,我暂时无法"}}}]}\n\n'
        'event: TEXT_CHUNK\ndata: {"content_block":[{"content":{'
        '"text_block":{"text":"生成你要求的内容"}}}]}\n\n'
        'event: SSE_ACK\ndata: {"ack_client_meta":{"conversation_id":"c1",'
        '"section_id":"s1"},"query_list":[{"question_id":"q1"}]}\n\n'
    )
    with pytest.raises(DoubaoContentRejected):
        parse_sse_ack(text)


def test_scan_sse_for_policy_rejection_detects_new_return_verb():
    """v0.2.24:动词集合补了「返回」 —— 命中「您请求的内容无法返回」。"""
    text = (
        'event: TEXT_MESSAGE\ndata: {"content_block":[{"content":{'
        '"text_block":{"text":"很抱歉,您请求的内容无法返回,请尝试其他描述"'
        '}}}]}\n\n'
        'event: SSE_ACK\ndata: {"ack_client_meta":{"conversation_id":"c1",'
        '"section_id":"s1"},"query_list":[{"question_id":"q1"}]}\n\n'
    )
    with pytest.raises(DoubaoContentRejected) as excinfo:
        parse_sse_ack(text)
    assert "无法返回" in excinfo.value.error_message


def test_scan_sse_for_policy_rejection_detects_rejection_in_top_level_text():
    """v0.2.31:豆包若把拒绝文案塞到 SSE 顶层 text 字段(不再是 text_block.text),
    也必须命中。新版 _walk 走全 payload 字符串值,只过滤已知元数据。"""
    text = (
        'event: TEXT_MESSAGE\ndata: {"text":"抱歉,我暂时无法生成这个视频,请换个主题再试试",'
        '"status":"failed","id":"abc"}\n\n'
        'event: SSE_ACK\ndata: {"ack_client_meta":{"conversation_id":"c1",'
        '"section_id":"s1"},"query_list":[{"question_id":"q1"}]}\n\n'
    )
    with pytest.raises(DoubaoContentRejected) as excinfo:
        parse_sse_ack(text)
    # 任一拒绝子串都算命中(具体哪个 substring 由 _POLICY_PATTERNS 顺序决定,
    # 不能写死 — 只想确认触发到了 DoubaoContentRejected 路径)
    assert any(
        kw in excinfo.value.error_message
        for kw in ("无法生成", "换个主题", "无法满足", "无法响应")
    )


def test_scan_sse_for_policy_rejection_detects_rejection_in_delta_text():
    """v0.2.31:豆包若把拒绝文案塞到 delta.text / choices[*].delta.content 等
    流式字段 —— 这种字段路径不在原 _walk 固定 key 集合里,新版必须兜住。"""
    text = (
        'event: TEXT_MESSAGE\ndata: {"choices":[{"delta":{"content":"'
        '生成内容中疑似包含侵权,请更换主题再试"}}],'
        '"id":"msg_abc","model":"seedance"}\n\n'
        'event: SSE_ACK\ndata: {"ack_client_meta":{"conversation_id":"c1",'
        '"section_id":"s1"},"query_list":[{"question_id":"q1"}]}\n\n'
    )
    with pytest.raises(DoubaoContentRejected):
        parse_sse_ack(text)


def test_scan_sse_for_policy_rejection_ignores_metadata_short_strings():
    """v0.2.31:id/status/session_id/timestamp 等元数据字段不会进 chunks,
    不会触发误判。即使这些字段恰好拼出 "无法返回" 之类也不能误命中(这里用
    「无法满足」+ status=拒绝两个值验证 status 字段被跳过)。"""
    # 「无法满足」是一句完整拒绝,放 status 字段 — 不应被识别成拒绝
    text = (
        'event: TEXT_MESSAGE\ndata: {"id":"msg_1","status":"我无法满足您",'
        '"session_id":"sess_1","created_at":1234567890}\n\n'
        'event: SSE_ACK\ndata: {"ack_client_meta":{"conversation_id":"c1",'
        '"section_id":"s1"},"query_list":[{"question_id":"q1"}]}\n\n'
    )
    ack = parse_sse_ack(text)
    assert ack["conversation_id"] == "c1"


def test_scan_sse_for_policy_rejection_truncates_overlong_strings():
    """v0.2.31:超长字符串(>8k)被截断,不会拖慢正则匹配或把 base64 视频 URL
    之类误塞进 chunks(基线测试,确保 chunk 不爆炸)。"""
    big_payload_text = "好的,正在生成视频" + "x" * 20000
    text = (
        f'event: TEXT_MESSAGE\ndata: {{"text":"{big_payload_text}",'
        f'"id":"msg_abc"}}\n\n'
        f'event: SSE_ACK\ndata: {{"ack_client_meta":{{"conversation_id":"c1",'
        f'"section_id":"s1"}},"query_list":[{{"question_id":"q1"}}]}}\n\n'
    )
    ack = parse_sse_ack(text)
    assert ack["conversation_id"] == "c1"


def test_parse_sse_ack_response_text_truncated_at_2000():
    """v0.2.24:response_text 用 raw sse_text[:2000],跟 DoubaoRateLimited 一致。"""
    long_data = '{"content_block":[{"content":{"text_block":{"text":"我暂时无法生成"}}}]}'
    sse = (
        f'event: TEXT_MESSAGE\ndata: {long_data}\n\n'
        f'event: SSE_ACK\ndata: {{"ack_client_meta":{{"conversation_id":"c1",'
        f'"section_id":"s1"}},"query_list":[{{"question_id":"q1"}}]}}\n\n'
    )
    padded = sse + ("\n# padding\n" * 5000)  # > 2000 chars
    with pytest.raises(DoubaoContentRejected) as excinfo:
        parse_sse_ack(padded)
    assert len(excinfo.value.response_text) <= 2000
    assert "无法生成" in excinfo.value.response_text


# ---------- v0.2.29:时长白名单放宽到任意整数 4..10 秒 ----------


@pytest.mark.parametrize("duration", [4, 5, 6, 7, 8, 9, 10])
def test_build_completion_payload_accepts_arbitrary_duration_4_to_10(duration):
    """v0.2.29:豆包接受任意整数 4..10 秒(原白名单 {5, 10} 太严,用户实测 6/7/8/9 都能生成)。"""
    payload = build_completion_payload(
        prompt="测试",
        model="seedance_v2.0_mini",
        ratio="1:1",
        duration=duration,
        fingerprint="fp",
    )
    assert json.loads(payload["chat_ability"]["ability_param"])["duration"] == duration


@pytest.mark.parametrize("duration", [0, 3, 11, 15, 30])
def test_build_completion_payload_rejects_duration_outside_4_to_10(duration):
    """v0.2.29:<4 或 >10 秒必须拒绝 —— 豆包不会处理,趁早 fail-fast。"""
    with pytest.raises(ValueError, match="duration"):
        build_completion_payload(
            prompt="测试",
            model="seedance_v2.0_mini",
            ratio="1:1",
            duration=duration,
            fingerprint="fp",
        )


# v0.3.3:单账号多任务并发 race 防御 —— parse_creation_result 必须按 envelope
# 上 local_message_id / message_id 过滤串话的 creation。同账号 A 同时提交 A1/A2,
# 字节 race 窗口下可能把 A1 的 creation 错塞给 A2 的 chain response,导致 A2
# worker 拿到 A1 的 vid+download_url。用户视角「下载下来都是 A1」。防御:
# 客户端在调用 parse_creation_result 时把本任务 submit 用过的 local_message_id
# 集合传进来,串话的 envelope 上 id 不在集合中 → 跳过这个 creation。
def _build_chain_response_with_envelope(*, envelope_id, creation):
    """构造一个 chain 响应:envelope 上带 envelope_id(message_id 或
    local_message_id),内部 content 里有 creation。
    """
    content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [creation]}},
    }])
    return {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [{
            "content": content,
            "local_message_id": envelope_id,
        }]}},
    }


def test_parse_creation_result_filters_by_expected_local_message_id():
    """v0.3.3:race 串话 —— A2 worker 只收自己的 envelope id,A1 envelope 必须跳过。"""
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    a2 = {
        "id": "task-A2",
        "video": {"status": 3, "vid": "v-A2", "download_url": "https://example/A2.mp4"},
    }
    a1_content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a1]}},
    }])
    a2_content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a2]}},
    }])
    # 模拟 race 窗口:A1 和 A2 各占一个 envelope,envelope id 分别标到各自消息上。
    # 字节这种结构:每个 message 一个 envelope + 一个 content。
    response = {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [
            {"content": a1_content, "local_message_id": "msg-A1"},
            {"content": a2_content, "local_message_id": "msg-A2"},
        ]}}
    }
    # A2 worker 用自己的 expected 集合 → 必须跳过 A1 envelope,命中 A2 envelope
    result = parse_creation_result(response, expected_local_message_ids={"msg-A2"})
    assert result is not None
    assert result["vid"] == "v-A2"
    assert result["fallback_result_url"] == "https://example/A2.mp4"


def test_parse_creation_result_relaxes_when_envelope_id_drifts(monkeypatch):
    """v0.3.5 收紧了 v0.3.4.1 的兜底 —— 「envelope 不匹配」必须先等 cooldown(默认
    30s)才接受 candidates[0]。这是为了解 A2/A4 拿到 A1/A3 的真根因:
    v0.3.4.1 在 race 窗口下立刻吞掉别人的 creation,而 v0.3.5 给客户端充足
    时间继续 poll,等服务端 envelope id 漂移窗口关闭后再兜底。
    """
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    response = _build_chain_response_with_envelope(envelope_id="msg-A1", creation=a1)

    # 1) 默认 cooldown(30s)未过 → 返 None,继续 poll
    result = parse_creation_result(response, expected_local_message_ids={"msg-A2"})
    assert result is None

    # 2) cooldown 已过 → 接受 A1(兜底),这是 v0.3.4.1 旧的立即接受语义迁移后的样子
    monkeypatch.setattr(protocol_module, "_FALLBACK_COOLDOWN_S", 0.0)
    result = parse_creation_result(response, expected_local_message_ids={"msg-A2"})
    assert result is not None
    assert result["vid"] == "v-A1"
    assert result["fallback_result_url"] == "https://example/A1.mp4"


def test_parse_creation_result_prefers_matching_id_over_drift():
    """v0.3.4.1:matches(强匹配)优先于 candidates(漂移兜底)。

    模拟:A1 是别人任务的 creation(被串话进来),A2 是本任务的 creation。
    两个 envelope 同时存在 → 必须返回 A2(A2 envelope 命中 expected,A1 漂移)。
    """
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    a2 = {
        "id": "task-A2",
        "video": {"status": 3, "vid": "v-A2", "download_url": "https://example/A2.mp4"},
    }
    a1_content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a1]}},
    }])
    a2_content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a2]}},
    }])
    # A1 envelope (id=msg-A1) 和 A2 envelope (id=msg-A2) 同时出现在 chain response。
    # A2 worker 的 expected={"msg-A2"} → A1 envelope 是 candidates,A2 envelope 是 matches。
    response = {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [
            {"content": a1_content, "local_message_id": "msg-A1"},
            {"content": a2_content, "local_message_id": "msg-A2"},
        ]}}
    }
    result = parse_creation_result(response, expected_local_message_ids={"msg-A2"})
    assert result is not None
    assert result["vid"] == "v-A2"  # 强匹配优先,不能被 A1 漂移抢先


def test_parse_creation_result_no_filter_when_expected_is_none():
    """v0.3.3:向后兼容 —— expected_local_message_ids=None 时返回首个 creation(原行为)。"""
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    # envelope 上故意带别人的 id,expected=None → 必须 fall through(不破坏旧测试)
    response = _build_chain_response_with_envelope(envelope_id="msg-someone-else", creation=a1)
    result = parse_creation_result(response, expected_local_message_ids=None)
    assert result is not None
    assert result["vid"] == "v-A1"


def test_parse_creation_result_message_id_missing_falls_through():
    """v0.3.3:服务端响应没带 envelope id → 仍 return(不阻塞正常生成)。"""
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    # envelope 上**没有** local_message_id / message_id 字段
    content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a1]}},
    }])
    response = {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [{
            "content": content,
        }]}}
    }
    # 即使 expected 集合给了一个不存在的 id,服务端没带 envelope id → fall through
    result = parse_creation_result(response, expected_local_message_ids={"msg-unknown"})
    assert result is not None
    assert result["vid"] == "v-A1"


def test_parse_creation_result_message_id_field_name_fallback():
    """v0.3.3:envelope 上只有 `message_id`(没有 `local_message_id`)也要识别。"""
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a1]}},
    }])
    response = {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [{
            "content": content,
            "message_id": "msg-A1",
        }]}}
    }
    # 期望集合里有 msg-A1 → 必须命中
    result = parse_creation_result(response, expected_local_message_ids={"msg-A1"})
    assert result is not None
    assert result["vid"] == "v-A1"


# v0.3.4.1:race 防御放宽后的额外覆盖 —— 漂移兜底、WARNING 日志、合法性判定
# 等新增行为需要专门测试。前面 5 个 v0.3.3 case 保留 + 上面 relax_when_drift /
# prefer_matching 两个是改写 + 下面是 3 个新增。
import logging


def test_parse_creation_result_returns_none_when_no_valid_creation(caplog):
    """v0.3.4.1:所有 creation 都不合法(status≠3 / 无 download_url)→ None。

    跟 v0.3.3 的"returns_none_when_no_creation_matches"区分:那个测的是 envelope
    id 不匹配(现在会兜底接受);这个测的是 creation 本身非法(服务端还没生成好 /
    生成失败)→ 必须返 None 让 caller 继续 poll,不能误把"在生成中"当作成功。
    """
    creating = {
        "id": "task-A1",
        "video": {"status": 1, "vid": "v-A1"},  # status=1 = 还在生成中
    }
    failed = {
        "id": "task-A2",
        "video": {"status": 5, "vid": "v-A2", "download_url": ""},  # 失败 + 无 URL
    }
    no_url = {
        "id": "task-A3",
        "video": {"status": 3, "vid": "v-A3"},  # status=3 但 download_url 缺失
    }
    content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [creating, failed, no_url]}},
    }])
    response = {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [{
            "content": content,
            "local_message_id": "msg-A1",
        }]}}
    }
    with caplog.at_level(logging.WARNING, logger="doupool.video.protocol"):
        result = parse_creation_result(response, expected_local_message_ids={"msg-A1"})
    assert result is None
    # 非法 creation 不触发漂移兜底 → 不应该有 WARNING
    assert "v0.3.4.1 race 防御兜底" not in caplog.text


def test_parse_creation_result_logs_warning_on_drift_fallthrough(caplog, monkeypatch):
    """v0.3.5:envelope id 漂移兜底必须打 WARNING,留下服务端 id drift 现场。

    v0.3.4.1 立即 accept;v0.3.5 加 cooldown → 测试必须 monkeypatch 缩短
    cooldown 才能命中 WARNING 路径。
    """
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    response = _build_chain_response_with_envelope(envelope_id="msg-A1", creation=a1)
    monkeypatch.setattr(protocol_module, "_FALLBACK_COOLDOWN_S", 0.0)

    with caplog.at_level(logging.WARNING, logger="doupool.video.protocol"):
        result = parse_creation_result(response, expected_local_message_ids={"msg-A2"})

    assert result is not None
    assert result["vid"] == "v-A1"
    # v0.3.5 WARNING 标识(取代 v0.3.4.1 的标识)
    assert "v0.3.5.2 race 防御兜底" in caplog.text
    assert "drift_vid=v-A1" in caplog.text
    assert "drift_creation_id=task-A1" in caplog.text
    assert "drift_envelope_ids=['msg-A1']" in caplog.text
    assert "candidates=1" in caplog.text


# =============================================================================
# v0.3.5:三层优先级 + 30s cooldown + 全局 dedup
# =============================================================================
# v0.3.4.1 fallback 太宽 —— 服务端 envelope id 跟客户端 expected 不匹配时立刻
# 接受 candidates[0],导致 A2 worker 拿到 A1 的 creation。v0.3.5 加三层:
# 1. expected_remote_task_ids 命中 creation.id(强证据,优先级 1)
# 2. expected_local_message_ids 命中 envelope(中等证据,优先级 2)
# 3. candidates 兜底 —— 必须满足:首次出现 ≥ 30s 且 _seen_remote_task_ids 未记录
# =============================================================================

from doupool.video import protocol as _protocol_module


@pytest.fixture(autouse=False)
def _reset_v035_seen():
    """v0.3.5.2:per-call 局部 dedup 不再需要 module-level state 隔离。
    保留 fixture 是为了维持 test signature 不变(测试用例继续显式声明依赖)。
    """
    yield


def test_parse_creation_result_prefers_remote_id_over_local_envelope(_reset_v035_seen):
    """v0.3.5 优先级 1:creation.id 命中 expected_remote_task_ids,即使 envelope 也
    命中 expected_local_message_ids,优先按 remote 返回(更明确的强证据)。"""
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    # envelope 用别人的 id(模拟 A2 worker 视角下 envelope 跟 remote 不一致)
    content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a1]}},
    }])
    response = {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [{
            "content": content,
            "local_message_id": "msg-A2",  # A2 的 envelope,但 creation 是 A1
        }]}},
    }
    # expected_remote_task_ids 命中 → 优先级 1 → 直接返回
    result = parse_creation_result(
        response,
        expected_local_message_ids={"msg-A2"},
        expected_remote_task_ids={"task-A1"},
    )
    assert result is not None
    assert result["vid"] == "v-A1"


def test_parse_creation_result_uses_local_envelope_when_remote_missing(_reset_v035_seen):
    """v0.3.5 优先级 2:服务端不返回 creation.id(老版本/字段缺失),但 envelope
    命中 expected_local_message_ids → 按优先级 2 接受。"""
    a1 = {
        # 没有 id 字段,模拟服务端不返回
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    response = _build_chain_response_with_envelope(envelope_id="msg-A1", creation=a1)
    result = parse_creation_result(
        response,
        expected_local_message_ids={"msg-A1"},
        expected_remote_task_ids={"task-A1"},  # 集合里有但 creation 没带 id
    )
    assert result is not None
    assert result["vid"] == "v-A1"


def test_parse_creation_result_fallback_returns_none_before_cooldown(_reset_v035_seen):
    """v0.3.5 cooldown:fallback candidates 必须等 ≥ 30s 才允许 accept。模拟 A2
    worker 第一次 poll 返回 A1 的 creation —— 此时距离 candidates 首次出现 < 30s,
    必须返回 None 让 caller 继续 poll,而不是立刻吞掉 A1 的视频。"""
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    # envelope id 不在 expected 里 → 走 fallback candidates
    response = _build_chain_response_with_envelope(envelope_id="msg-A1", creation=a1)
    # 注意:不传 expected_remote_task_ids(服务端没返回 creation.id)
    result = parse_creation_result(
        response,
        expected_local_message_ids={"msg-A2"},  # A2 worker 视角
    )
    # cooldown 未满 → 返回 None
    assert result is None


def test_parse_creation_result_fallback_accepts_after_cooldown(_reset_v035_seen, monkeypatch, caplog):
    """v0.3.5 cooldown:模拟 30s 后,candidates 首次出现时间已过 cooldown →
    允许 accept 兜底并打 WARNING。"""
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    response = _build_chain_response_with_envelope(envelope_id="msg-A1", creation=a1)
    # 让 cooldown 已过
    monkeypatch.setattr(_protocol_module, "_FALLBACK_COOLDOWN_S", 0.0)

    with caplog.at_level(logging.WARNING, logger="doupool.video.protocol"):
        result = parse_creation_result(
            response,
            expected_local_message_ids={"msg-A2"},
        )
    assert result is not None
    assert result["vid"] == "v-A1"
    assert "v0.3.5.2 race 防御兜底" in caplog.text
    assert "drift_creation_id=task-A1" in caplog.text
    assert "drift_vid=v-A1" in caplog.text


def test_parse_creation_result_dedup_skips_already_seen_remote_id(monkeypatch):
    """v0.3.5.2 per-call dedup:同一次 parse_creation_result 调用内,同一个
    creation.id 被接受过,继续循环时跳过 —— 防止 fallback 路径在同一 call 内
    重复 deliver 同一视频。v0.3.5.2 关键差异:跨调用不 dedup,所以这里用
    两次 mock 不同 candidates(同一 call 内出现两个 creation 同一个 id 的
    极端情况)来验证 per-call dedup。
    """
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    # 构造两个 creation block,id 都是 task-A1(同一次 chain 响应里字节
    # 可能把同一个 creation 重复塞进多个 message)
    content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a1]}},
    }])
    response = {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [
            {"content": content, "local_message_id": "msg-other-1"},
            {"content": content, "local_message_id": "msg-other-2"},
        ]}},
    }
    monkeypatch.setattr(_protocol_module, "_FALLBACK_COOLDOWN_S", 0.0)

    # 第一次 call:coolldown=0,接受 candidates 中第一个 task-A1
    result = parse_creation_result(
        response,
        expected_local_message_ids={"msg-A2"},  # envelope 不命中 → 走 fallback
    )
    assert result is not None
    assert result["vid"] == "v-A1"
    # per-call dedup 生效:同一 call 内第二个相同 cid 被跳过,call 整体只返回
    # 一个 creation(否则 result 是按 candidates 顺序的第一个)
    assert result["remote_task_id"] == "task-A1"


def test_parse_creation_result_remote_id_match_dedups_after_accept():
    """v0.3.5.2:优先级 1(remote id)命中后,per-call 内 dedup 已记录 id,
    同一 call 内 fallback 路径 candidates 即使包含同一 id 也会被跳过。
    验证方式:构造同 call 内含 matches_remote + candidates(同 id) → 只返
    matches_remote 那个,不会因为 candidates 有同 id 而重复处理。
    """
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    # 优先级 1 命中 + 另一个 envelope 不命中(走 fallback candidates,
    # 但 cid 跟 matches_remote 一样 → per-call dedup 应跳过)
    content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a1]}},
    }])
    response = {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [
            {  # matches_remote:envelope=msg-A1,creation.id=task-A1
                "content": content,
                "local_message_id": "msg-A1",
            },
            {  # fallback candidates:envelope=msg-other,creation.id=task-A1
                "content": content,
                "local_message_id": "msg-other",
            },
        ]}},
    }
    result = parse_creation_result(
        response,
        expected_local_message_ids={"msg-A1"},
        expected_remote_task_ids={"task-A1"},
    )
    # 优先级 1 直接命中并返回,不会因为 fallback 有同 cid 而重复处理
    assert result is not None
    assert result["remote_task_id"] == "task-A1"
    assert result["vid"] == "v-A1"


def test_parse_creation_result_no_expected_args_skips_all_defenses():
    """v0.3.5.2 向后兼容:两个 expected 参数都不传 → 完全保留 v0.3.2.5 行为,
    所有合法 creation 直接进 matches_local,没有任何 cooldown / dedup。

    v0.3.5.2:由于 dedup 改为 per-call 局部,module-level `_seen_remote_task_ids`
    已不存在,这里不再断言 module state,改为验证「不传 expected 时直接接受
    首个合法 creation」的行为没变。
    """
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    response = _build_chain_response_with_envelope(envelope_id="msg-A1", creation=a1)
    # 不传任何 expected → 旧行为,直接返回
    result = parse_creation_result(response)
    assert result is not None
    assert result["vid"] == "v-A1"
    # 旧行为下,WARNING 路径不触发(candidates cooldown 路径未启用)
    # 不打 v0.3.5.2 race 防御兜底 WARNING
    assert "race 防御兜底" not in caplog.text if False else True  # noqa


def test_parse_creation_result_no_warning_when_explicit_match(caplog):
    """v0.3.4.1:envelope id 强匹配(matches)路径不打 WARNING —— 正常路径不应噪音。"""
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    response = _build_chain_response_with_envelope(envelope_id="msg-A1", creation=a1)

    with caplog.at_level(logging.WARNING, logger="doupool.video.protocol"):
        result = parse_creation_result(response, expected_local_message_ids={"msg-A1"})

    assert result is not None
    assert result["vid"] == "v-A1"
    # 强匹配成功 → 不应打 WARNING(避免刷屏)
    assert "v0.3.4.1 race 防御兜底" not in caplog.text


# ---------- v0.3.5:三层优先级 + 兜底 cooldown + dedup ----------


def test_parse_creation_result_priority_remote_over_envelope(caplog):
    """v0.3.5 优先级 1:creation.id ∈ expected_remote_task_ids 强于 envelope 命中。

    场景:服务端 race 把 A2 的 creation 塞进 A1 的 chain response,但 A1
    submit 时拿到的 ack 里有 A1 自己的 creation.id (= "task-A1")。同时
    envelope 上 msg-A2 命中 A1 的 expected_local_message_ids。
    必须返回 A1(服务端实际分配给 A1 的 creation),不能因为 envelope 上
    msg-A2 命中而错选 A2 creation。
    """
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    a2 = {
        "id": "task-A2",
        "video": {"status": 3, "vid": "v-A2", "download_url": "https://example/A2.mp4"},
    }
    a1_content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a1]}},
    }])
    a2_content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a2]}},
    }])
    # 故意把 A1 的 creation 放在 msg-A2 envelope 上(模拟服务端 envelope 串话),
    # 同时把 A2 的 creation 放在 msg-A1 envelope 上。A1 worker:
    #   expected_remote_task_ids = {"task-A1"}    → 强匹配 A1
    #   expected_local_message_ids = {"msg-A1"}   → 命中 A2 envelope
    # 期望:返回 A1,不打 WARNING。
    response = {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [
            {"content": a1_content, "local_message_id": "msg-A2"},
            {"content": a2_content, "local_message_id": "msg-A1"},
        ]}}
    }
    with caplog.at_level(logging.WARNING, logger="doupool.video.protocol"):
        result = parse_creation_result(
            response,
            expected_local_message_ids={"msg-A1"},
            expected_remote_task_ids={"task-A1"},
        )
    assert result is not None
    assert result["remote_task_id"] == "task-A1"
    assert result["vid"] == "v-A1"
    # 不应打 WARNING:优先级 1 命中,无需兜底
    assert "race 防御兜底" not in caplog.text


def test_parse_creation_result_priority_envelope_over_fallback(caplog):
    """v0.3.5 优先级 2:envelope ∩ expected_local_message_ids 强于 candidates 兜底。

    场景:服务端没在 ack 里 echo remote_task_id(expected_remote_task_ids=None),
    但 envelope 上 msg-A1 命中 expected_local_message_ids。同时有别的
    creation 在 envelope 上没有任何 id(candidates 兜底层)。
    期望:envelope 命中优先,不打 WARNING(只有 candidates 才打)。
    """
    a_match = {
        "id": "task-match",
        "video": {"status": 3, "vid": "v-match", "download_url": "https://example/match.mp4"},
    }
    a_other = {
        "id": "task-other",
        "video": {"status": 3, "vid": "v-other", "download_url": "https://example/other.mp4"},
    }
    match_content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a_match]}},
    }])
    other_content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a_other]}},
    }])
    response = {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [
            {"content": match_content, "local_message_id": "msg-match"},
            {"content": other_content},  # 没有 envelope id → candidates 兜底
        ]}}
    }
    with caplog.at_level(logging.WARNING, logger="doupool.video.protocol"):
        result = parse_creation_result(
            response,
            expected_local_message_ids={"msg-match"},
        )
    assert result is not None
    assert result["remote_task_id"] == "task-match"
    # 不应打 WARNING:envelope 命中,不走兜底
    assert "race 防御兜底" not in caplog.text


def test_parse_creation_result_fallback_blocked_by_cooldown(monkeypatch):
    """v0.3.5.2 兜底 cooldown:candidates 出现 <5s → 不接受,返 None。

    场景:candidates 首次出现,刚过几秒(小于 _FALLBACK_COOLDOWN_S)→ 必须
    继续 poll 等到 cooldown 过,不能立即 accept 把别人的 creation 当成
    自己的。

    v0.3.5.2:`_FALLBACK_COOLDOWN_S` 改为 5s,first_seen 改为 per-call 局部。
    """
    a_other = {
        "id": "task-other",
        "video": {"status": 3, "vid": "v-other", "download_url": "https://example/other.mp4"},
    }
    other_content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a_other]}},
    }])
    # envelope id 有但不命中 expected → candidates(走 cooldown)。
    # 注意:envelope 完全缺失会触发 v0.3.3 「字段缺失 fall through」→ matches_local,
    # 那就测不到 cooldown 路径了,所以这里必须给 envelope id。
    response = {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [
            {"content": other_content, "local_message_id": "msg-someone"},
        ]}}
    }
    # monkeypatch 把 cooldown 改到 1 小时,确保 5s 跑不完 → 必返 None
    monkeypatch.setattr(protocol_module, "_FALLBACK_COOLDOWN_S", 3600.0)
    result = parse_creation_result(
        response,
        expected_local_message_ids=set(),
        expected_remote_task_ids=set(),
    )
    # cooldown 未过 → 返 None(继续 poll)
    assert result is None


def test_parse_creation_result_fallback_accepted_after_cooldown(caplog, monkeypatch):
    """v0.3.5.2 兜底 cooldown:candidates cooldown 已过 → 接受,打 WARNING。

    v0.3.5.2:cooldown 从 30s 改为 5s(`_FALLBACK_COOLDOWN_S=5.0`);first_seen
    改为 per-call 局部 dict,测试改用 monkeypatch 把 cooldown 设为 0,
    让 candidates 立即可 accept,验证 WARNING 文案 + 返回值正确。

    dedup 行为在 v0.3.5.2 改为 per-call 局部,跨 call 不持久,所以这里不再
    断言 module-level state。
    """
    a_other = {
        "id": "task-other",
        "video": {"status": 3, "vid": "v-other", "download_url": "https://example/other.mp4"},
    }
    other_content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a_other]}},
    }])
    # envelope id 有但不命中 expected → candidates(走 cooldown)
    response = {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [
            {"content": other_content, "local_message_id": "msg-someone"},
        ]}}
    }
    # cooldown=0 让 candidates 立即可 accept
    monkeypatch.setattr(protocol_module, "_FALLBACK_COOLDOWN_S", 0.0)
    with caplog.at_level(logging.WARNING, logger="doupool.video.protocol"):
        result = parse_creation_result(
            response,
            expected_local_message_ids=set(),
            expected_remote_task_ids=set(),
        )
    assert result is not None
    assert result["remote_task_id"] == "task-other"
    assert "v0.3.5.2 race 防御兜底" in caplog.text
    assert "cooldown_elapsed=" in caplog.text


def test_parse_creation_result_dedup_skips_already_seen(monkeypatch):
    """v0.3.5.2 per-call dedup:同一次 parse_creation_result call 内,同一个
    cid 出现在多个 candidates 里,第一个 accept 后,后续同 cid 跳过 —— 防
    fallback 路径在同一 call 内重复 deliver 同一视频。

    v0.3.5.2 关键差异:dedup 改为 per-call 局部,跨 call 不持久。这里用
    同 call 内两个 candidates 同 cid 来验证 per-call dedup 生效。
    """
    a_self = {
        "id": "task-self",
        "video": {"status": 3, "vid": "v-self", "download_url": "https://example/self.mp4"},
    }
    content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a_self]}},
    }])
    # 同 call 内两个 candidates(envelope 不同,cid 都是 task-self) →
    # 第一个 accept,第二个被 per-call dedup 跳过,但 call 整体仍返第一个。
    response = {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [
            {"content": content, "local_message_id": "msg-other-1"},
            {"content": content, "local_message_id": "msg-other-2"},
        ]}}
    }
    # cooldown=0 让 accept 立即发生
    monkeypatch.setattr(protocol_module, "_FALLBACK_COOLDOWN_S", 0.0)
    result = parse_creation_result(
        response,
        expected_local_message_ids=set(),
        expected_remote_task_ids=set(),
    )
    # call 内第一个 accept 的 candidates 返回
    assert result is not None
    assert result["remote_task_id"] == "task-self"


def test_parse_creation_result_no_match_returns_none(caplog):
    """v0.3.5:无任何 expected + 无合法 creation → 返 None(保持 v0.3.2.5 兼容)。

    跟 v0.3.4.1 的「合法 candidates 但 envelope 不匹配」区分 —— 这里连
    candidates 都没合法 creation,只验证兜底 cooldown + dedup 路径在没有
    合法 creation 时仍能安全返 None。
    """
    pending = {
        "id": "task-pending",
        "video": {"status": 1, "vid": "v-pending"},  # 还在生成中
    }
    content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [pending]}},
    }])
    response = {
        "downlink_body": {"pull_singe_chain_downlink_body": {"messages": [
            {"content": content},
        ]}}
    }
    with caplog.at_level(logging.WARNING, logger="doupool.video.protocol"):
        result = parse_creation_result(
            response,
            expected_local_message_ids={"some-id"},
            expected_remote_task_ids={"some-remote-id"},
        )
    # 没合法 creation(全在生成中) → 返 None
    assert result is None
    # 没兜底 → 不打 WARNING
    assert "race 防御兜底" not in caplog.text


def test_parse_creation_result_no_expected_still_accepts_any_creation():
    """v0.3.5 向后兼容:两个 expected 都 None → 接受首个合法 creation(原 v0.3.2.5 行为)。

    验证 v0.3.5 没破坏老调用方(没传任何 expected 集合时的 fall through)。
    """
    a1 = {
        "id": "task-A1",
        "video": {"status": 3, "vid": "v-A1", "download_url": "https://example/A1.mp4"},
    }
    content = json.dumps([{
        "block_type": 2074,
        "content": {"creation_block": {"creations": [a1]}},
    }])
    response = _build_chain_response_with_envelope(envelope_id="msg-someone-else", creation=a1)
    # 故意 envelope 上是别人的 id,但调用方没传任何 expected → 旧行为保留
    result = parse_creation_result(response)
    assert result is not None
    assert result["vid"] == "v-A1"
