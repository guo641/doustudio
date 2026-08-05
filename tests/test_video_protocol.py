import json

import pytest

from doupool.video.protocol import (
    EXTRA_CLIENT_META_KEYS,
    DoubaoContentRejected,
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
