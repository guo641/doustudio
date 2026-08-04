import json

import pytest

from doupool.video.protocol import (
    EXTRA_CLIENT_META_KEYS,
    DoubaoRateLimited,
    build_completion_payload,
    find_creation_directory,
    find_video_node,
    parse_creation_result,
    parse_download_info,
    parse_sse_ack,
)


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
