from __future__ import annotations

import json
import logging
import time
from uuid import uuid4

# v0.2.21:复用 prompt_reviser 已有的 policy 关键词(侵权/违规/换个主题/无法返回该内容
# 等),chain 响应任意 message 文本命中即抛 DoubaoContentRejected,让 service 层
# 立即标 failed + 退还额度,而不是等 5min timeout。
from doupool.prompt_reviser import _POLICY_PATTERNS

# v0.3.4.1:race 防御放宽 — 当服务端 envelope id 不匹配但 creation 合法时,
# 需要打 WARNING 暴露服务端 id 漂移,便于后续观测 / 排查。
_LOGGER = logging.getLogger(__name__)


# v0.3.5.3:在 v0.3.5.2 基础上修复 candidates cooldown 永远等不到的 bug。
#
# v0.3.5.2 把 `_seen_remote_task_ids`(dedup)和 `_candidate_first_seen`
# (cooldown 计时)都改成 per-call 局部。问题:cooldown 是「同一个 cid 在
# chain response 里首次出现后等 5s 才 accept」的语义,first_seen 必须是
# per-cid 跨多次 poll 累计冷却时长。如果 per-call,每轮 first_seen 重置成
# `now`,cooldown 永远等不到 5s → candidates 兜底路径永远不会 accept →
# 4 任务全部 7min timeout(实测 v0.3.5.2 用户反馈「4 个全部卡生成中」)。
#
# v0.3.5.3 设计:dedup 和 first_seen 是两个独立维度,必须解耦:
# - dedup `_seen_remote_task_ids` → **per-call 局部**(同一次 run 内不
#   重复 accept 同一 cid;跨 run 不 dedup)。原因:v0.3.5 module-level
#   dedup 在同账号并发下把 A1 的 cid 永久写入全局 set,A2 永远跳过 →
#   7min timeout。
# - first_seen `_candidate_first_seen` → **保留 module-level dict**
#   (per-cid 跨 run 累计冷却时长)。cooldown 必须用「进程级首次出现」
#   时间戳,否则永远等不到 5s。
# 两者解耦是 v0.3.5.3 的设计核心。

# v0.3.5.3:保留 5s cooldown。v0.3.5.1 验证过 5s 足够 elapsed 12.6s。
_FALLBACK_COOLDOWN_S = 5.0


# v0.3.5.3:保留 module-level `_candidate_first_seen` —— per-cid 累计冷却时长。
# key=cid(str),value=该 cid 在本进程 chain response 里首次出现的 monotonic
# 时间戳。注意:这个 dict 只服务 candidates cooldown,不参与 dedup。
_candidate_first_seen: dict[str, float] = {}

# v0.3.5.4:进程内记录 creation 的 task 归属。与 v0.3.5 的全局 set 不同,
# 同 owner 可幂等重入,只有其他 task 会被跳过,因此不会重现「见过一次就让原
# task 永久超时」的问题。owner=None 的兼容调用不读写此表。
_accepted_remote_ids: dict[str, str] = {}


def _claim_remote_id_for_owner(payload: dict[str, str], owner_task_id: str | None) -> bool:
    """为最终选中的 creation 建立进程内归属;返回当前 task 是否可接受。"""
    if owner_task_id is None:
        return True

    cid = payload.get("remote_task_id", "")
    if not cid:
        _LOGGER.warning(
            "v0.3.5.4 creation 缺少 cid,无法记录归属 owner_task_id=%s",
            owner_task_id,
        )
        return True

    owner = _accepted_remote_ids.get(cid)
    if owner is not None and owner != owner_task_id:
        _LOGGER.warning(
            "v0.3.5.4 creation cid=%s 已归属 task=%s,当前 task=%s 跳过",
            cid,
            owner,
            owner_task_id,
        )
        return False

    _accepted_remote_ids[cid] = owner_task_id
    return True


MODELS = {"seedance_v2.0_std", "seedance_v2.0", "seedance_v2.0_mini"}
RATIOS = {"1:1", "3:4", "4:3", "9:16", "16:9", "21:9"}
# v0.2.29:豆包接受任意整数 4..10 秒时长,放宽白名单(原 {5,10} 太严)。
DURATIONS = set(range(4, 11))
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
    """解析豆包 /chat/completion SSE 响应,返回 conversation / section / question_id。

    v0.2.24:重构为「先把整个 SSE 流扫一遍、再 return」——
    返回 ack 之前先扫所有 TEXT_* 包的 text_block.text,命中 `_POLICY_PATTERNS`
    立即抛 `DoubaoContentRejected`。原因:豆包新版拒绝时,拒绝文案塞在
    SSE `TEXT_MESSAGE` 事件里立刻发出,但后续 `/im/chain/single` 轮询永远
    返 `creation_block.status=1`,`parse_creation_result` 看不见拒绝,
    polling 卡到 5min timeout,用户视角「永远生成中」。在 SSE_ACK 解析之前
    抢先识别 → run() retry loop 接住 → max_reject_retries 次自动改写重试。
    """
    seen_sse_ack = False
    ack_payload: dict | None = None
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
            ack_payload = json.loads(data)
            seen_sse_ack = True

    if not seen_sse_ack or ack_payload is None:
        raise RuntimeError("豆包响应缺少 SSE_ACK")

    # v0.2.24:ack 解析完之后、return 之前,扫一遍拒绝文案。reject 与
    # SSE_ACK 同包到达 → 视为最终结果,直接抛(避免再 poll chain)。
    rejection = scan_sse_for_policy_rejection(text)
    if rejection is not None:
        raise DoubaoContentRejected(rejection, response_text=text[:2000])

    meta = ack_payload.get("ack_client_meta", {})
    queries = ack_payload.get("query_list") or [{}]
    return {
        "conversation_id": str(meta.get("conversation_id", "")),
        "section_id": str(meta.get("section_id", "")),
        "question_id": str(queries[0].get("question_id", "")),
    }


# v0.2.31:已知非文本字段(纯元数据,不会包含拒绝文案),扫到时跳过,
# 既保留针对性又避免误命中。长度阈值防把"id"、"status" 之类短字符串误进 chunks。
_NON_TEXT_KEYS = {
    "id", "ids", "event_id", "session_id", "user_id", "question_id",
    "conversation_id", "section_id", "message_id", "request_id",
    "type", "event_type", "status", "code", "status_code", "err_code",
    "timestamp", "created_at", "updated_at", "time", "role",
    "model", "name", "version", "request_type",
    "is_final", "is_end", "is_error", "final", "end", "done",
    "tool_call_id", "function_call_id",
}
_MAX_TEXT_LEN = 8000  # 防御:豆包偶然回超大 base64 / 视频 URL 也只取前 N 字


def scan_sse_for_policy_rejection(sse_text: str) -> str | None:
    """v0.2.24:扫描豆包 SSE 响应文本里的拒绝文案。

    触发场景:豆包新版拒绝时,`/chat/completion` 的 SSE 流里会立刻发
    `TEXT_MESSAGE` / `TEXT_CHUNK` 事件,正文是「我暂时无法生成你要求的内容,
    请尝试输入其他要求」类拒绝模板。后续 `/im/chain/single` 轮询永远返
    `creation_block.status=1`,`parse_creation_result` 看不见拒绝,
    polling 卡到 5min timeout。

    这里在 SSE_ACK 之前先扫一遍,把拒绝挡在 polling 之前 → run() retry
    loop 接 DoubaoContentRejected → max_reject_retries 次自动改写重试。

    扫描策略(防御性):
    - 拆 `event:` / `data:` 双行包
    - 跳过 SSE_ACK / STREAM_ERROR / SSE_HEARTBEAT(已由 parse_sse_ack 处理或无内容)
    - v0.2.31:递归走 payload 所有 string 值(不再只盯着 text_block.text /
      content_block / content / message 几个固定 key),过滤 _NON_TEXT_KEYS
      元数据 + 长度阈值,避免误命中 id/status/session_id 之类短字符串,
      同时兜住豆包未来把拒绝文案塞到 delta.text / reply_message / 顶层
      text / choices[*].delta.content 等新字段。
    - 拼接所有 text → 跑 _POLICY_PATTERNS(复用,不再写新 regex)
    - 返回首个命中的 match.group(0)[:200],未命中 None
    """
    chunks: list[str] = []

    def _walk(payload: object) -> None:
        """递归收集 payload 里所有可能是文本内容的 string 值。"""
        if isinstance(payload, dict):
            for k, v in payload.items():
                if isinstance(v, str):
                    if k in _NON_TEXT_KEYS:
                        continue
                    if len(v) > _MAX_TEXT_LEN:
                        v = v[:_MAX_TEXT_LEN]
                    if v:
                        chunks.append(v)
                elif isinstance(v, (dict, list)):
                    _walk(v)
        elif isinstance(payload, list):
            for item in payload:
                _walk(item)

    for packet in sse_text.replace("\r\n", "\n").split("\n\n"):
        event = ""
        data = ""
        for line in packet.splitlines():
            if line.startswith("event:"):
                event = line[6:].strip()
            elif line.startswith("data:"):
                data += line[5:].strip()
        if not data or data == "{}":
            continue
        # 已由 parse_sse_ack 处理的事件不要重复扫
        if event in {"SSE_ACK", "STREAM_ERROR", "SSE_HEARTBEAT"}:
            continue
        try:
            payload = json.loads(data)
        except (TypeError, json.JSONDecodeError):
            continue
        _walk(payload)

    if not chunks:
        return None
    combined = "\n".join(chunks)
    for pat in _POLICY_PATTERNS:
        m = pat.search(combined)
        if m:
            return m.group(0)[:200]
    return None


def parse_creation_result(
    response: dict,
    *,
    expected_local_message_ids: set[str] | None = None,
    expected_remote_task_ids: set[str] | None = None,
    owner_task_id: str | None = None,
) -> dict[str, str] | None:
    """从 chain 响应里解析视频生成结果。

    两条返回路径:
    1. 成功 — `creation_block.creations[].video.status == 3` 且有 download_url → dict
    2. v0.2.21:内容审核拒绝 — 任意 message 的 text 含 policy 关键词 → 抛
       DoubaoContentRejected(reason, response_text=...)。

    顺序:**先扫成功块**(若已成功直接 return),再扫拒绝关键词。避免已成功的任务
    被同包里的 reject 文本误判。

    v0.3.3 同账号并发 race 防御:字节 `/im/chain/single` 在 race 窗口下偶尔把别
    人的 creation 塞进我们的 chain response(共用同一 conversation 上下文)。
    防御思路:
    - 每个 message envelope 上有 `local_message_id` / `message_id` 字段(字节
      提交响应里就有,见 docs/doubao-video-api-analysis.md L177)。
    - 调用方传入 `expected_local_message_ids = {本任务 submit 时用过的 id 集合}`
      (t2v 单 id,i2v 双 id:attachment + text)。
    - 命中规则:如果 envelope 有 id 且 id 不在 expected 集合 → 跳过这个
      creation(继续 poll,等我们自己那条出来)。
    - Fall through 条件:
      * `expected_local_message_ids is None`(默认,完全向后兼容 v0.3.2.5)
      * envelope 上 id 字段缺失(字段名或字节版本差异)→ 仍 return 第一个
        status==3,不阻塞正常生成。

    v0.3.4.1 放宽:**两阶段扫描**。
    - 第一遍扫所有 creation,三分类:
      * `matches`:envelope ∩ expected ≠ ∅ 且 creation 合法(强匹配,首选)
      * `candidates`:envelope 与 expected 不相交但 creation 合法(兜底)
      * 非法 creation(status≠3 / 无 download_url)直接丢
    - 决策:`matches` 非空 → 返回 matches[0];否则 `candidates` 非空 → 打
      WARNING 后返回 candidates[0];都没有 → 走兜底扫描(rejected reason)。
    - 触发 WARNING 的场景:服务端 envelope id 与客户端 expected 不匹配但 creation
      已成功。这是字节 SSE_ACK / chain response 之间 id 命名不一致导致的(ACK
      透传浏览器 crypto.randomUUID,chain response 用服务端内部 id)。WARNING 留下
      现场,后续可观测服务端 id 漂移频率。

    v0.3.5 三层优先级 + 兜底 cooldown:
    - 新参数 `expected_remote_task_ids`:submit 时抽到的服务端 creation.id 集合
      (浏览器 UI 路径下从 SSE_ACK payload / fetch 路径下从响应 envelope 上抽)。
      creation.id 命中这一集合是**最强证据**(服务端返回的就是我们 submit 的)。
    - 三层分类:
      * `matches_remote`:creation.id ∈ expected_remote_task_ids —— 优先级 1
      * `matches_local`:envelope ∩ expected_local_message_ids ≠ ∅ —— 优先级 2
      * `candidates`:合法 creation,但既不命中 expected_remote_task_ids,
        envelope 也没匹配 expected_local_message_ids
    - 兜底 cooldown 5s:candidates 必须存在 ≥ 5s 才允许 accept,避免
      早返回把别人刚 submit 的 creation 当成我们自己的。
    - v0.3.5.3:**dedup 改为 per-call 局部** + **first_seen 保留
      module-level**(两个维度解耦):
      * `_seen_remote_task_ids` → per-call 局部,同一次 run 内不重复
        accept 同一 cid,跨 run 不 dedup。原因:v0.3.5 module-level
        dedup 在同账号并发下把 A1 的 cid 永久写入全局 set,A2 永远跳过
        → 7min timeout。
      * `_candidate_first_seen` → 保留 module-level dict,per-cid 跨 run
        累计冷却时长。原因:cooldown 必须用「进程级首次出现」时间戳,
        否则 v0.3.5.2(两个都 per-call)所有 first_seen 每轮重置成 `now`,
        cooldown 永远等不到 5s → 所有 task 全部 7min timeout。
    - 优先级 1 > 优先级 2 > 兜底(cooldown 后)。

    v0.3.5.4 进程内 task-aware dedup:
    - `owner_task_id` 非空时,creation.id 首次被接受即记录归属。
    - 同 owner 可幂等接受;其他 owner 必须跳过并继续检查后续 creation。
    - `owner_task_id=None` 保持旧调用行为,不读写归属表。
    """
    if owner_task_id == "":
        raise ValueError("owner_task_id 不能为空字符串")

    messages = response.get("downlink_body", {}).get("pull_singe_chain_downlink_body", {}).get("messages", [])

    # v0.3.3:同时记录 envelope id 和 decoded blocks,这样成功块扫描可以基于
    # envelope identity 过滤。
    decoded_blocks: list[list[dict]] = []
    envelope_id_sets: list[set[str]] = []
    for message in messages:
        envelope_ids: set[str] = set()
        for key in ("local_message_id", "message_id"):
            value = message.get(key)
            if isinstance(value, str) and value:
                envelope_ids.add(value)
        envelope_id_sets.append(envelope_ids)
        try:
            decoded_blocks.append(json.loads(message.get("content") or "[]"))
        except (TypeError, json.JSONDecodeError):
            decoded_blocks.append([])

    # v0.3.5:成功块扫描 —— 三层分类,优先级 matches_remote > matches_local >
    # candidates(兜底 cooldown + dedup)。
    # 兼容性 fallback 列表:满足 v0.3.3「字段缺失 fall through」或 v0.3.4.1
    # 「envelope drift 立即 accept」语义,直接进 matches_local 不走 cooldown。
    matches_remote: list[dict] = []
    matches_local: list[dict] = []
    candidates: list[tuple[set[str], dict]] = []
    for blocks, envelope_ids in zip(decoded_blocks, envelope_id_sets):
        for block in blocks:
            creations = block.get("content", {}).get("creation_block", {}).get("creations", [])
            for creation in creations:
                video = creation.get("video") or {}
                if not (video.get("status") == 3 and video.get("download_url")):
                    # 非法 creation(还在生成中 / 失败 / 缺 URL)→ 不参与分类
                    continue
                creation_id = str(creation.get("id") or "")
                payload = {
                    "remote_task_id": creation_id,
                    "vid": str(video.get("vid", "")),
                    "fallback_result_url": video["download_url"],
                    "cover_url": video.get("cover", {}).get("image_thumb", {}).get("url", ""),
                }
                # v0.3.5 三层优先级:
                # - 两 expected 都 None(v0.3.2.5 调用方,完全向后兼容):不做 race
                #   防御,所有合法 creation 进 matches_local,旧行为保留。
                # - 优先级 1:creation.id ∈ expected_remote_task_ids → matches_remote
                # - 优先级 2:envelope ∩ expected_local_message_ids ≠ ∅ →
                #   matches_local(只在传了 expected_local_message_ids 时检查)
                # - v0.3.3 「字段缺失」fall through:envelope 上没 id 字段 且
                #   expected_local_message_ids 非空集合(空集合 ≠ None → 不算
                #   「字段缺失兜底」,是调用方明确 opt-in 进 race 防御) →
                #   matches_local。
                # - 兜底 candidates(后续 30s cooldown + dedup 才允许 accept)
                if (
                    expected_remote_task_ids is None
                    and expected_local_message_ids is None
                ):
                    # v0.3.2.5 完全向后兼容:不做 race 防御,直接接受。
                    matches_local.append(payload)
                elif (
                    expected_remote_task_ids is not None
                    and creation_id
                    and creation_id in expected_remote_task_ids
                ):
                    matches_remote.append(payload)
                elif (
                    expected_local_message_ids is not None
                    and envelope_ids
                    and not envelope_ids.isdisjoint(expected_local_message_ids)
                ):
                    matches_local.append(payload)
                elif (
                    expected_local_message_ids is not None
                    and expected_local_message_ids  # 必须是「非空集合」才认作字段缺失兜底
                    and not envelope_ids
                ):
                    # v0.3.3:服务端没带 envelope id → fall through,不强匹配
                    # (字段缺失是字节版本差异,不是 race)。调用方 expected_local
                    # 是非空集合时才 fall through;空集合是显式 opt-in,要走 cooldown。
                    matches_local.append(payload)
                else:
                    candidates.append((envelope_ids, payload))

    # v0.3.5.3 DEBUG:打印三层分类结果,定位"生成完毕但 UI 卡生成中"。
    # 用户报 v0.3.5.1 跑下来 1 个 task 一直显示"生成中",DB 不动;
    # v0.3.5.2 全员卡死(cooldown 永远等不到)。
    # 把三层数 + accepted/candidates cooldown 状态打出来,复现一次即可
    # 知道是优先级 1/2 没命中,还是 candidates cooldown 没到,还是 dedup
    # 把这个 creation.id 永久跳过。
    _LOGGER.info(
        "[v0.3.5.3 DEBUG parse_creation_result] matches_remote=%d matches_local=%d "
        "candidates=%d expected_remote=%s expected_local=%s",
        len(matches_remote),
        len(matches_local),
        len(candidates),
        sorted(expected_remote_task_ids) if expected_remote_task_ids else "None",
        sorted(expected_local_message_ids) if expected_local_message_ids else "None",
    )

    # v0.3.5.4:所有成功返回路径都经过 task owner gate。不能直接取 [0],
    # 因为同一响应的第一项可能已归属其他 task,后面仍可能有本 task 的结果。
    for payload in matches_remote:
        if _claim_remote_id_for_owner(payload, owner_task_id):
            return payload

    # v0.3.5:优先级 2 命中 → 直接 return,不打 WARNING(envelope 匹配足以
    # 证明是我们 submit 的)。
    for payload in matches_local:
        if _claim_remote_id_for_owner(payload, owner_task_id):
            return payload

    # v0.3.5.4:兜底 candidates —— 5s cooldown(per-cid 跨 run 累计计时)
    # + task-aware module owner。first_seen 必须 module-level(否则 cooldown 永远等
    # 不到 5s,这是 v0.3.5.2 出过的问题);owner map 同样是 module-level,
    # 但同 owner 可幂等接受,只阻止其他 task 重复领取同一 cid。
    if candidates and (
        expected_remote_task_ids is not None
        or expected_local_message_ids is not None
    ):
        now = time.monotonic()
        # v0.3.5.3:first_seen 用 module-level `_candidate_first_seen`(per-cid
        # 跨 run 累计冷却时长),不是 per-call 局部 dict。v0.3.5.2 用 per-call
        # 局部导致 first_seen 每轮重置为 `now`,cooldown 永远等不到 5s。
        for envelope_ids, payload in candidates:
            cid = payload["remote_task_id"]
            claimed_owner = (
                _accepted_remote_ids.get(cid)
                if owner_task_id is not None and cid
                else None
            )
            if claimed_owner == owner_task_id and owner_task_id is not None:
                # 此 cid 此前已经通过接受门槛,同 task 重试可幂等返回。
                return payload
            if claimed_owner is not None and claimed_owner != owner_task_id:
                _claim_remote_id_for_owner(payload, owner_task_id)
                continue

            first_seen = _candidate_first_seen.setdefault(cid, now)
            # v0.3.5.4 DEBUG:candidates 遍历时把每个 cid + 当前 owner +
            # cooldown 剩余秒数打出来。cooldown 未到 = 候选存在但 first_seen
            # 还在 5s 窗口内;此时不能提前写入 owner。
            _LOGGER.info(
                "[v0.3.5.4 DEBUG candidates loop] cid=%s claimed_owner=%s "
                "first_seen_age=%.2fs cooldown_left=%.2fs",
                cid,
                claimed_owner,
                now - first_seen,
                max(0.0, _FALLBACK_COOLDOWN_S - (now - first_seen)),
            )
            if now - first_seen < _FALLBACK_COOLDOWN_S:
                # 还没过 5s,等下一轮 poll
                continue
            if not _claim_remote_id_for_owner(payload, owner_task_id):
                continue
            _LOGGER.warning(
                "v0.3.5.4 race 防御兜底:5s cooldown 后仍未命中 expected_remote_task_ids "
                "/ expected_local_message_ids, accept candidate。candidates=%d, "
                "expected_remote=%d, expected_local=%d, drift_envelope_ids=%s, "
                "drift_creation_id=%s, drift_vid=%s, cooldown_elapsed=%.1fs",
                len(candidates),
                len(expected_remote_task_ids) if expected_remote_task_ids else 0,
                len(expected_local_message_ids) if expected_local_message_ids else 0,
                sorted(envelope_ids),
                payload.get("remote_task_id", ""),
                payload.get("vid", ""),
                now - first_seen,
            )
            return payload

    # 2. v0.2.21:policy 关键词兜底扫描 — 任意 block 含「侵权|违规|换个主题|无法
    # 返回该内容|sensitive content」等关键词 → 抛 rejected,service 层立即 failed。
    rejected_reason = _find_policy_rejection(decoded_blocks)
    if rejected_reason is not None:
        raise DoubaoContentRejected(rejected_reason, response_text=_truncate_response(response))

    return None


class DoubaoContentRejected(RuntimeError):
    """v0.2.21:豆包在 chain 响应里给了真人能看到的拒绝文案(常见如「生成内容中
    疑似包含XXX侵权/换个主题再试试/无法返回该内容」等),而不是真正的成功块。

    之前 `parse_creation_result` 看不见这条路径,polling 循环一直返回 None,直到
    `runner.timeout` 触发才标 failed(timeout 阈值 5min,期间用户视角「卡住」)。

    抛出后 service 层会:
    - update_video_task(status="failed", error_message="豆包拒绝: ...")
    - 退还本 runner 已扣的额度
    - 触发 callback
    - return —— 不重试(同 prompt 必拒)
    """

    def __init__(self, message: str, response_text: str = "") -> None:
        super().__init__(message)
        self.error_message = message
        self.response_text = response_text


def _find_policy_rejection(decoded_blocks: list[list[dict]]) -> str | None:
    """扫描已解析的 blocks,返回第一个命中 `_POLICY_PATTERNS` 的原文片段。

    扫描的字段(按经验):
    - block.content.text_block.text        (普通文本块,豆包拒绝常在这里)
    - block.content.creation_block.creations[].* (creation 失败状态有时附原因)
    - block.content.error_block / disallow_reason / reason 等(防御性兜底)

    返回首个命中的 `match.group(0)`(截断到 200 字),未命中返回 None。
    """
    def _scan_text(text: str) -> str | None:
        if not text:
            return None
        for pat in _POLICY_PATTERNS:
            m = pat.search(text)
            if m:
                return m.group(0)[:200]
        return None

    for blocks in decoded_blocks:
        for block in blocks:
            content = block.get("content") or {}
            # text_block.text
            text_block = content.get("text_block") or {}
            hit = _scan_text(str(text_block.get("text") or ""))
            if hit is not None:
                return hit
            # creation_block 失败状态附带的拒因(两种位置都见过:
            # creation.error_msg / creation.video.error_msg,豆包版本可能不一样)
            for creation in content.get("creation_block", {}).get("creations", []):
                video = creation.get("video") or {}
                hit = (
                    _scan_text(str(creation.get("error_msg") or ""))
                    or _scan_text(str(creation.get("disallow_reason") or ""))
                    or _scan_text(str(creation.get("reason") or ""))
                    or _scan_text(str(video.get("error_msg") or ""))
                    or _scan_text(str(video.get("disallow_reason") or ""))
                    or _scan_text(str(video.get("reason") or ""))
                )
                if hit is not None:
                    return hit
            # 顶层 error_block / reason 兜底
            for key in ("error_block", "reason_block", "warning_block"):
                sub = content.get(key) or {}
                hit = _scan_text(str(sub.get("text") or sub.get("reason") or ""))
                if hit is not None:
                    return hit
    return None


def _truncate_response(response: dict, max_chars: int = 2000) -> str:
    """把 chain response 截断到 max_chars,便于日志打印。失败也无副作用。"""
    try:
        return json.dumps(response, ensure_ascii=False)[:max_chars]
    except (TypeError, ValueError):
        return str(response)[:max_chars]


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
