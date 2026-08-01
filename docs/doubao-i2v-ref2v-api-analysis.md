# 豆包图生视频接口抓包分析

抓包时间：2026-07-15。  
来源文件：`/tmp/doupool-i2v-ref2v-network.jsonl`  
本次成功捕获：**图生视频（1 图 + 提示词）** 完整链路，并已生成视频结果。

产品确认：豆包**不支持独立“参考生视频”**，只有图生；图生支持 **多图，最多 9 张**。  
多图时协议应复用同一 `block_type=10052` 的 `attachment_block.attachments[]`，每张图先上传 + `pre_handle`，再一并写入 completion。

敏感字段（Cookie、Token、`fp`、`a_bogus`、`msToken`、上传 Auth）均已脱敏，不写入本文。

---

## 1. 与文生视频的核心差异

| 项目 | 文生视频 (T2V) | 图生视频 (I2V，本次) |
| --- | --- | --- |
| 上传 | 无 | `prepare_upload` → ImageX Apply/Commit → 可选 TOS 二进制上传 |
| 预处理 | 无 | `pre_handle_v2_without_conv` |
| `/chat/completion` messages | **1** 条文本消息 | **2** 条：附件消息 + 文本消息 |
| 文本 `block_type` | `10000` | `10000` |
| 图片 `block_type` | 无 | **`10052`**（`attachment_block`） |
| `chat_ability.ability_type` | `17` | **仍为 `17`** |
| `ability_param` | `{ratio, model, duration}` | **相同结构** |
| 结果拉取 | `/im/chain/single` + creation_block | **相同** |

结论：

1. 图生视频**没有**新的 ability_type，仍是视频 skill `17`。  
2. 差异集中在：**先上传图片拿 URI，再在 completion 里多发一条 attachment 消息**。  
3. 文本格式仍类似：`生成视频：{prompt}，{ratio}`。

---

## 2. 完整请求链路

```
1) POST /alice/resource/prepare_upload
2) GET  /top/v1?Action=ApplyImageUpload   (ImageX 申请上传地址)
3)     [二进制 PUT/POST 到 UploadHosts，本次抓包未记入 fetch/xhr]
4) POST /top/v1?Action=CommitImageUpload  (确认上传)
5) POST /alice/message/pre_handle_v2_without_conv
6) POST /chat/completion                  (SSE)
7) POST /im/chain/single                  (结果，与文生一致)
```

辅助：

- `POST /samantha/skill/pack`，`{"skill_type": 17}`：下发视频 UI 选项（模型/比例/时长等）。

---

## 3. 图片上传

### 3.1 申请上传凭证

```http
POST https://www.doubao.com/alice/resource/prepare_upload
Content-Type: application/json
```

```json
{
  "tenant_id": "5",
  "scene_id": "5",
  "resource_type": 2
}
```

响应：

```json
{
  "code": 0,
  "data": {
    "service_id": "a9rns2rl98",
    "upload_path_prefix": "rc/pc/bot-chat",
    "upload_host": "imagex.bytedanceapi.com",
    "upload_auth_token": "<REDACTED>"
  }
}
```

### 3.2 ImageX ApplyImageUpload

```http
GET https://www.doubao.com/top/v1
  ?Action=ApplyImageUpload
  &Version=2018-08-01
  &ServiceId=a9rns2rl98
  &NeedFallback=true
  &FileSize=<bytes>
  &FileExtension=.png
  &s=<random>
```

响应关键字段：

- `Result.UploadAddress.StoreInfos[0].StoreUri`  
  例如：`tos-cn-i-a9rns2rl98/<uuid>.png`
- `UploadHosts`：如 `tos-lq-x.bytedancevod.com`
- `SessionKey`、`Auth`、`UploadID`：上传鉴权材料（短时有效）

### 3.3 CommitImageUpload

```http
POST https://www.doubao.com/top/v1?Action=CommitImageUpload&Version=2018-08-01&ServiceId=a9rns2rl98
Content-Type: application/json
```

```json
{
  "SessionKey": "<from apply>"
}
```

响应含图片元数据：宽高、md5、size、format，以及最终 URI（与 StoreUri 一致）。

### 3.4 实现建议

- 二进制上传应在 **Playwright 页面上下文** 中走官方 SDK/接口，不要硬编码 Auth/SessionKey。  
- 业务层只需拿到最终 `uri`（`tos-cn-i-a9rns2rl98/...`）以及可选宽高。

---

## 4. 图片预处理

```http
POST https://www.doubao.com/alice/message/pre_handle_v2_without_conv
```

```json
{
  "uplink_entity": {
    "entity_type": 2,
    "entity_content": {
      "image": {
        "key": "tos-cn-i-a9rns2rl98/2239094d0dbb4326908548ba8c69ab89.png"
      }
    },
    "identifier": "<uuid>"
  },
  "bot_id": "7338286299411103781",
  "local_message_id": "<uuid>"
}
```

响应：

```json
{
  "code": 0,
  "data": {
    "pre_generate_id": "50143399879282434"
  }
}
```

注意：`identifier` 会原样出现在后续 completion 的 attachment 里，前后必须一致。

---

## 5. 创建请求（图生）

```http
POST https://www.doubao.com/chat/completion?<公共环境参数>&<风控参数>
Content-Type: application/json
```

业务载荷（脱敏后结构）：

```json
{
  "client_meta": {
    "local_conversation_id": "local_...",
    "conversation_id": "",
    "bot_id": "7338286299411103781",
    "last_section_id": "",
    "last_message_index": null
  },
  "messages": [
    {
      "local_message_id": "<uuid-image-msg>",
      "content_block": [
        {
          "block_type": 10052,
          "content": {
            "attachment_block": {
              "attachments": [
                {
                  "type": 1,
                  "identifier": "<same-as-pre_handle-identifier>",
                  "image": {
                    "name": "已生成图像 1.png",
                    "uri": "tos-cn-i-a9rns2rl98/<id>.png",
                    "image_ori": {
                      "url": "<signed-url>",
                      "width": 1086,
                      "height": 1448
                    }
                  },
                  "parse_state": 0,
                  "review_state": 1,
                  "upload_status": 1,
                  "progress": 100,
                  "src": ""
                }
              ]
            },
            "pc_event_block": ""
          },
          "block_id": "<uuid>",
          "parent_id": "",
          "meta_info": [],
          "append_fields": []
        }
      ],
      "message_status": 0
    },
    {
      "local_message_id": "<uuid-text-msg>",
      "content_block": [
        {
          "block_type": 10000,
          "content": {
            "text_block": {
              "text": "生成视频：动起来，9:16",
              "icon_url": "",
              "icon_url_dark": "",
              "summary": ""
            },
            "pc_event_block": ""
          },
          "block_id": "<uuid>",
          "parent_id": "",
          "meta_info": [],
          "append_fields": []
        }
      ],
      "message_status": 0
    }
  ],
  "option": {
    "create_time_ms": 1784089515736,
    "collect_id": "<uuid>",
    "unique_key": "<uuid>",
    "need_create_conversation": true,
    "conversation_init_option": {"need_ack_conversation": true},
    "sse_recv_event_options": {"support_chunk_delta": true},
    "recovery_option": {
      "is_recovery": false,
      "req_create_time_sec": 1784089515,
      "append_sse_event_scene": 0
    }
  },
  "chat_ability": {
    "ability_type": 17,
    "ability_param": "{\"ratio\":\"9:16\",\"model\":\"seedance_v2.0_mini\",\"duration\":5}"
  },
  "user_context": [],
  "ext": {
    "answer_with_suggest": "0",
    "fp": "<fingerprint>",
    "sub_conv_firstmet_type": "1",
    "collection_id": "<same-as-option.collect_id>",
    "conversation_init_option": "{\"need_ack_conversation\":true}",
    "commerce_credit_config_enable": "0"
  }
}
```

### 5.1 关键字段说明

| 字段 | 含义 |
| --- | --- |
| `messages[0]` | 图片附件消息，`block_type=10052` |
| `messages[1]` | 文本提示，`block_type=10000` |
| `attachments[].type` | `1` = 图片 |
| `attachments[].identifier` | 与 pre_handle 一致的本地标识 |
| `attachments[].image.uri` | ImageX StoreUri |
| `attachments[].review_state` | 本次为 `1`（审核通过/可用） |
| `attachments[].upload_status` | `1`，`progress=100` |
| `option.collect_id` / `ext.collection_id` | 同一次发送的集合 ID（图文绑定） |
| `chat_ability` | 与文生完全同形 |

SSE `SSE_ACK` 与文生相同：返回 `conversation_id` / `section_id` / `question_id`。  
`ext` 侧可观察到：`image_attachment_num=1`、`is_image_related=1`、`attachment_scene=4`、`input_skill.skill_type=17`。

---

## 6. 结果

与文生一致，IM 消息中：

- `block_type=10000`：文案「你的视频生成好了。」  
- `block_type=2074`：`creation_block.creations[]`  
  - `type=2` 视频  
  - `gen_detail.task_type=6`（本次图生样本）  
  - `video.status=3` 且含 `download_url` / `vid` / `cover`

无水印下载链路可继续沿用现有文生实现。

---

## 7. 多图（最多 9 张）

产品侧上限为 **9 张图**。实现约定：

1. 每张图独立走上传 + `pre_handle`，各自生成 `identifier` / `uri`。  
2. completion 的 `messages[0].content_block[0].attachment_block.attachments` 放入 1–9 个 `type=1` 附件。  
3. 文本消息仍只有一条；`chat_ability` 不变。  
4. `image_attachment_num` 等 ext 字段由服务端回写，客户端可不硬编码。

---

## 8. DouPool 实现建议

1. **任务类型**：`t2v | i2v`（无 ref2v）。  
2. **协议层**：  
   - 复用现有 `build_completion_payload` 的 ability / option 骨架。  
   - i2v：`messages = [attachment_message(1–9), text_message]`。  
3. **上传层**（Playwright 页内执行）：  
   对每张图：`prepare_upload` → Apply → 上传 → Commit → `pre_handle`。  
4. **数据模型**：`VideoTask.mode` + `image_paths`（本地路径 JSON 数组）。  
5. **调度与额度**：与文生共用账号池。

---

## 9. 原始产物

| 文件 | 说明 |
| --- | --- |
| `/tmp/doupool-i2v-ref2v-network.jsonl` | 完整脱敏抓包 |
| `/tmp/doupool-i2v-completion-request.json` | 图生 completion 请求体 |
| `docs/doubao-i2v-ref2v-api-analysis.md` | 本文 |
