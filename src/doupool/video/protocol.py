from __future__ import annotations

import json
import time
from uuid import uuid4


MODELS = {"seedance_v2.0_std", "seedance_v2.0", "seedance_v2.0_mini"}
RATIOS = {"1:1", "3:4", "4:3", "9:16", "16:9", "21:9"}
DURATIONS = {5, 10}
TASK_MODES = {"t2v", "i2v"}
MAX_I2V_IMAGES = 9

# v0.2.17:payload.client_meta 透传键(从登录 profile 抽出的 WebMSSDK / TeaSDK
# 真实指纹,直接复用而不是逆向生成 msToken / a_bogus)。
# service / runner 层把 TokenBundle.to_dict() 当 **extra_client_meta 透传进来。
EXTRA_CLIENT_META_KEYS = (
    "web_id",
    "tea_uuid",
    "device_id",
    "pc_version",
    "web_id_signature",
)


class DoubaoRateLimited(RuntimeError):
    """豆包返回 STREAM_ERROR:rate limited 时抛。

    v0.2.15:带 response_text 字段,把 SSE 响应原文透传出来,
    `video/service._run_inner` 把它写到日志,方便下次「额度已用完」误报时
    能看到豆包真正的 error_msg(可能是 fingerprint / IP / 风控 等)。

    v0.2.16:is_risk_control 旗标 — `extra.decision.from == "shark_admin"` 时
    表示这是字节风控拦截,不是真 quota 限流。service 层区分两种处理:
    - 真 quota: cap 三桶 + 封号 limited_until
    - 风控: 不动桶,只标 task failed,提示"账号被风控,稍后重试或换号"
    """

    def __init__(self, message: str, response_text: str = "", is_risk_control: bool = False) -> None:
        super().__init__(message)
        self.response_text = response_text
        self.is_risk_control = is_risk_control


def _detect_risk_control(payload: dict) -> bool:
    """v0.2.16:识别豆包 STREAM_ERROR 的风控来源。

    字节内部风控服务 shark_admin 会把 decision 塞在 extra.decision(字符串形式的 JSON),
    `from == "shark_admin"` + `type == "verify"` 就是风控拦截。
    真 quota 限流没这个 decision 字段,或者 `from` 不是 shark_admin。
    """
    decision_raw = payload.get("extra", {}).get("decision") if isinstance(payload, dict) else None
    if not decision_raw:
        return False
    try:
        decision = json.loads(decision_raw)
    except (TypeError, json.JSONDecodeError):
        return False
    return decision.get("from") == "shark_admin"


def _base_option(
    now_ms: int,
    unique_key: str,
    *,
    need_create_conversation: bool = True,
    collect_id: str = "",
) -> dict:
    return {
        "send_message_scene": "",
        "create_time_ms": now_ms,
        "collect_id": collect_id,
        "is_audio": False,
        "answer_with_suggest": False,
        "tts_switch": False,
        "need_deep_think": 0,
        "click_clear_context": False,
        "from_suggest": False,
        "is_regen": False,
        "is_replace": False,
        "is_from_click_option": False,
        "is_from_click_softlink": False,
        "disable_sse_cache": False,
        "select_text_action": "",
        "is_select_text": False,
        "resend_for_regen": False,
        "scene_type": 0,
        "unique_key": unique_key,
        "start_seq": 0,
        "need_create_conversation": need_create_conversation,
        "conversation_init_option": {"need_ack_conversation": True},
        "regen_query_id": [],
        "edit_query_id": [],
        "regen_instruction": "",
        "no_replace_for_regen": False,
        "message_from": 0,
        "shared_app_name": "",
        "shared_app_id": "",
        "sse_recv_event_options": {"support_chunk_delta": True},
        "is_ai_playground": False,
        "is_old_user": True,
        "recovery_option": {
            "is_recovery": False,
            "req_create_time_sec": now_ms // 1000,
            "append_sse_event_scene": 0,
        },
        "message_storage_type": 0,
    }


def _text_message(
    prompt: str,
    ratio: str,
    *,
    local_message_id: str | None = None,
    block_id: str | None = None,
) -> dict:
    message_text = f"生成视频：{prompt}，{ratio}"
    return {
        "local_message_id": local_message_id or str(uuid4()),
        "content_block": [{
            "block_type": 10000,
            "content": {
                "text_block": {
                    "text": message_text,
                    "icon_url": "",
                    "icon_url_dark": "",
                    "summary": "",
                },
                "pc_event_block": "",
            },
            "block_id": block_id or str(uuid4()),
            "parent_id": "",
            "meta_info": [],
            "append_fields": [],
        }],
        "message_status": 0,
    }


def _attachment_message(
    images: list[dict],
    *,
    local_message_id: str | None = None,
    block_id: str | None = None,
) -> dict:
    attachments = []
    for image in images:
        width = image.get("width")
        height = image.get("height")
        image_ori: dict = {}
        if image.get("url"):
            image_ori["url"] = image["url"]
        if width is not None:
            image_ori["width"] = width
        if height is not None:
            image_ori["height"] = height
        attachments.append({
            "type": 1,
            "identifier": image["identifier"],
            "image": {
                "name": image.get("name") or "image.png",
                "uri": image["uri"],
                "image_ori": image_ori,
            },
            "parse_state": 0,
            "review_state": 1,
            "upload_status": 1,
            "progress": 100,
            "src": "",
        })
    return {
        "local_message_id": local_message_id or str(uuid4()),
        "content_block": [{
            "block_type": 10052,
            "content": {
                "attachment_block": {"attachments": attachments},
                "pc_event_block": "",
            },
            "block_id": block_id or str(uuid4()),
            "parent_id": "",
            "meta_info": [],
            "append_fields": [],
        }],
        "message_status": 0,
    }


def build_completion_payload(
    prompt: str,
    model: str,
    ratio: str,
    duration: int,
    fingerprint: str,
    *,
    mode: str = "t2v",
    images: list[dict] | None = None,
    now_ms: int | None = None,
    local_conversation_id: str | None = None,
    local_message_id: str | None = None,
    block_id: str | None = None,
    unique_key: str | None = None,
    collect_id: str | None = None,
    attachment_message_id: str | None = None,
    attachment_block_id: str | None = None,
    **extra_client_meta: str,
) -> dict:
    """组装豆包视频生成请求的 payload。

    v0.2.17:**extra_client_meta 透传 WebMSSDK / TeaSDK 真实指纹字段,runner 层
    把 TokenBundle.to_dict() 当 kwargs 传入(只接受 EXTRA_CLIENT_META_KEYS
    白名单内的键,避免乱塞字段污染 payload)。
    """
    prompt = prompt.strip()
    if not prompt:
        raise ValueError("prompt is required")
    if mode not in TASK_MODES:
        raise ValueError("unsupported mode")
    if model not in MODELS:
        raise ValueError("unsupported model")
    if ratio not in RATIOS:
        raise ValueError("unsupported ratio")
    if duration not in DURATIONS:
        raise ValueError("unsupported duration")

    images = list(images or [])
    if mode == "i2v":
        if not images:
            raise ValueError("image is required for i2v")
        if len(images) > MAX_I2V_IMAGES:
            raise ValueError(f"i2v supports at most {MAX_I2V_IMAGES} images")
        for image in images:
            if not image.get("uri") or not image.get("identifier"):
                raise ValueError("image uri and identifier are required")
    elif images:
        raise ValueError("images are only supported for i2v")

    now_ms = now_ms or int(time.time() * 1000)
    # v0.2.17:local_conversation_id 用 UUIDv4 — 之前是 16 位纯数字,风控可按
    # 数字模式聚关联账号 + 时间窗。UUIDv4 无任何语义信息,跟真人浏览器一致。
    local_conversation_id = local_conversation_id or str(uuid4())
    unique_key = unique_key or str(uuid4())
    collect_id = collect_id or (str(uuid4()) if images else "")

    messages: list[dict] = []
    if images:
        messages.append(
            _attachment_message(
                images,
                local_message_id=attachment_message_id,
                block_id=attachment_block_id,
            )
        )
    messages.append(
        _text_message(
            prompt,
            ratio,
            local_message_id=local_message_id,
            block_id=block_id,
        )
    )

    ext = {
        "answer_with_suggest": "0",
        "fp": fingerprint,
        "sub_conv_firstmet_type": "1",
        "collection_id": collect_id,
        "conversation_init_option": '{"need_ack_conversation":true}',
        "commerce_credit_config_enable": "0",
    }

    return {
        "client_meta": {
            "local_conversation_id": local_conversation_id,
            "conversation_id": "",
            "bot_id": "7338286299411103781",
            "last_section_id": "",
            "last_message_index": None,
            # v0.2.17:WebMSSDK / TeaSDK 真实指纹(从登录 profile 抽)。
            # 只接受 EXTRA_CLIENT_META_KEYS 白名单内的键,过滤掉杂七杂八 kwargs。
            **{
                k: v
                for k, v in extra_client_meta.items()
                if k in EXTRA_CLIENT_META_KEYS and v
            },
        },
        "messages": messages,
        "option": _base_option(
            now_ms,
            unique_key,
            need_create_conversation=True,
            collect_id=collect_id,
        ),
        "chat_ability": {
            "ability_type": 17,
            "ability_param": json.dumps(
                {"ratio": ratio, "model": model, "duration": duration},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        },
        "user_context": [],
        "ext": ext,
    }


def parse_sse_ack(text: str) -> dict[str, str]:
    for packet in text.replace("\r\n", "\n").split("\n\n"):
        event = ""
        data = ""
        for line in packet.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if event == "STREAM_ERROR":
            error = json.loads(data or "{}")
            message = error.get("error_msg") or "豆包接口返回错误"
            if message == "rate limited":
                # v0.2.16:区分真 quota 限流 vs 风控拦截。豆包把所有拦截都报
                # "rate limited",但 extra.decision.from == "shark_admin" 时
                # 是字节风控(verify),不该 cap 桶(rate limit 经常很快就放)。
                is_risk = _detect_risk_control(error)
                raise DoubaoRateLimited(message, text[:2000], is_risk_control=is_risk)
            raise RuntimeError(message)
        if event == "SSE_ACK":
            payload = json.loads(data)
            meta = payload.get("ack_client_meta", {})
            queries = payload.get("query_list") or [{}]
            return {
                "conversation_id": str(meta.get("conversation_id", "")),
                "section_id": str(meta.get("section_id", "")),
                "question_id": str(queries[0].get("question_id", "")),
            }
    raise RuntimeError("豆包响应缺少 SSE_ACK")


def parse_creation_result(response: dict) -> dict[str, str] | None:
    messages = response.get("downlink_body", {}).get("pull_singe_chain_downlink_body", {}).get("messages", [])
    for message in messages:
        try:
            blocks = json.loads(message.get("content") or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        for block in blocks:
            creations = block.get("content", {}).get("creation_block", {}).get("creations", [])
            for creation in creations:
                video = creation.get("video") or {}
                if video.get("status") == 3 and video.get("download_url"):
                    return {
                        "remote_task_id": str(creation.get("id", "")),
                        "vid": str(video.get("vid", "")),
                        "fallback_result_url": video["download_url"],
                        "cover_url": video.get("cover", {}).get("image_thumb", {}).get("url", ""),
                    }
    return None


def find_creation_directory(response: dict) -> str | None:
    if response.get("code") != 0:
        return None
    for child in response.get("data", {}).get("children", []):
        if child.get("name") == "我的创作":
            return str(child.get("id", "")) or None
    return None


def find_video_node(response: dict, vid: str) -> str | None:
    if response.get("code") != 0:
        return None
    for child in response.get("data", {}).get("children", []):
        if str(child.get("key", "")) == str(vid):
            return str(child.get("id", "")) or None
    return None


def parse_download_info(response: dict) -> dict[str, str] | None:
    if response.get("code") != 0:
        return None
    infos = response.get("data", {}).get("download_infos") or []
    if not infos or not infos[0].get("main_url"):
        return None
    return {
        "result_url": infos[0]["main_url"],
        "backup_result_url": infos[0].get("backup_url", ""),
    }
