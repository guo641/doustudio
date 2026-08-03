import json

import pytest

from doupool.video.protocol import (
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


def test_parse_sse_ack_extracts_new_conversation():
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
