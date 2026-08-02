# 豆包网页视频生成接口抓包分析

抓包时间：2026-07-13。以下结论来自已授权账号的一次真实“文生视频”请求。登录凭证、Cookie、指纹和风控参数均不写入本文。

## 1. 请求链路

1. `POST /alice/slot/action_bar_v3/get_item_conf`：下发视频功能的模型、时长和比例选项。
2. `POST /chat/completion`：提交视频生成消息；响应类型是流式响应（SSE）。
3. 页面通过豆包 IM 通道收到任务完成通知。
4. `POST /im/chain/single`：拉取会话消息和最终视频结果。

本次没有观察到独立的“视频任务状态轮询”接口。网页端由 IM 通知触发结果拉取；后台程序也可以低频调用 `/im/chain/single` 检查会话中是否出现完成消息。

## 2. 视频配置来源

配置项位于 `get_item_conf` 返回的 `item_id=45337547893174274`，`instruction_type=18`。接口当前下发的选项如下（选项可能随服务端配置变化，程序不应硬编码为永久值）：

| UI 模型 | `ability_param.model` | 说明 |
| --- | --- | --- |
| Seedance 2.0 | `seedance_v2.0_std` | 进阶画面表现，2 倍消耗 |
| Seedance 2.0 Fast | `seedance_v2.0` | 快速出片 |
| Seedance 2.0 Mini | `seedance_v2.0_mini` | 日常生成 |

- 时长：`5`、`10`（秒）
- 比例：`1:1`、`3:4`、`4:3`、`9:16`、`16:9`、`21:9`
- 视频能力编号：`chat_ability.ability_type = 17`

## 3. 创建请求

```http
POST https://www.doubao.com/chat/completion?<公共环境参数>&<风控参数>
Content-Type: application/json
Cookie: <当前账号浏览器会话>
```

本次请求的业务载荷（标识符替换为占位符）：

```json
{
  "client_meta": {
    "conversation_id": "<conversation_id>",
    "bot_id": "7338286299411103781",
    "last_section_id": "<last_section_id>",
    "last_message_index": 2
  },
  "messages": [
    {
      "local_message_id": "<uuid>",
      "content_block": [
        {
          "block_type": 10000,
          "content": {
            "text_block": {
              "text": "生成视频：小狗在跳舞，9:16",
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
    "create_time_ms": 1783938511460,
    "unique_key": "<uuid>",
    "need_create_conversation": false,
    "sse_recv_event_options": {"support_chunk_delta": true},
    "recovery_option": {
      "is_recovery": false,
      "req_create_time_sec": 1783938511,
      "append_sse_event_scene": 0
    }
  },
  "chat_ability": {
    "ability_type": 17,
    "ability_param": "{\"ratio\":\"9:16\",\"model\":\"seedance_v2.0\",\"duration\":5}"
  },
  "user_context": [],
  "ext": {
    "answer_with_suggest": "0",
    "fp": "<当前浏览器指纹>",
    "collection_id": "",
    "commerce_credit_config_enable": "0"
  }
}
```

`option` 还有一批布尔开关。普通首次提交均为抓包中的默认值；实现时应从当前网页版本捕获模板或完整复用，不建议只发送上面的精简示意载荷。

## 4. 参数如何产生

| 参数 | 来源/计算方式 |
| --- | --- |
| `aid`, `real_aid` | 豆包 Web 应用常量；本次均为 `497858` |
| `version_code` | Web 端协议版本；本次为 `20800` |
| `pc_version`, `doubao_pc_version` | 当前网页包版本；本次为 `3.27.0` |
| `language` | 当前页面语言 |
| `region`, `sys_region` | 页面启动配置和账号区域 |
| `device_id`, `tea_uuid`, `web_id` | 浏览器持久化设备/统计身份，由页面 SDK 和已有会话提供 |
| `web_tab_id` | 当前标签页的 UUID，标签页生命周期内复用 |
| `conversation_id`, `last_section_id`, `last_message_index` | 当前会话状态，来自 `/im/chain/single` 或新建会话流程 |
| `local_message_id`, `block_id`, `option.unique_key` | 前端为本次消息生成的 UUID |
| `create_time_ms` | `Date.now()` 毫秒时间戳 |
| `req_create_time_sec` | `floor(create_time_ms / 1000)` |
| `ability_param` | UI 选择结果序列化成 JSON 字符串，不是嵌套 JSON 对象 |
| `fp` | 页面风控/指纹 SDK 产生，同时出现在查询参数和 `ext.fp` |
| `msToken` | 字节系 Web 风控 SDK 维护的短期令牌 |
| `a_bogus` | 页面风控 SDK 针对当次 URL/请求生成的动态校验值 |

`fp`、`msToken`、`a_bogus` 不应保存为账号固定字段，也不能可靠复用。推荐保留 Playwright 持久化登录上下文，并让豆包页面在正常请求流程中生成它们；不实现或伪造风控签名。

### 4.1 从当前网页代码确认的生成过程

当前网页包为 `pc_version=3.27.0`。对网页自身加载的 JavaScript 模块追踪后，请求的组装顺序如下：

1. 文本消息节点生成基础消息：
   - `local_message_id`：`crypto.randomUUID()`；浏览器不支持时使用 UUID v4 回退实现。
   - `block_id`：同样为 UUID v4。
   - `create_time`：`Date.now()`。
   - `bot_id`：取当前会话，缺失时回退到 `7338286299411103781`。
   - `section_id`：取当前会话的 `last_section_id`。
2. 视频能力写入消息扩展：
   - 外层先构造成 `{ability_type, ability_param}`。
   - `ability_param` 本身先执行一次 `JSON.stringify(abilityParams)`。
   - 外层对象再执行一次 `JSON.stringify` 存入消息扩展。
   - 发送转换器解析外层字符串，所以最终 Payload 中 `chat_ability` 是对象，而 `chat_ability.ability_param` 仍是 JSON 字符串。
3. 发送会话生成独立 UUID v4，作为 `option.unique_key`。
4. 协议转换器把本地消息转换成 `/chat/completion` Payload：
   - `client_meta.last_message_index = Number(conversation.message_index)`。
   - `option.create_time_ms = Number(message.create_time)`。
   - `option.req_create_time_sec = floor(create_time_ms / 1000)`。
   - `option.need_create_conversation = Boolean(local_conversation_id)`。
5. `fp` 由页面延迟加载的验证模块调用 `getFp()` 得到，并在当前页面生命周期内缓存；提交管线将它写入消息 `ext`，SSE 请求层再复制到查询参数。
6. 公共查询参数由页面的统一参数管理器和 Tea/TTWid SDK合并。
7. 页面最后使用 `fetch` 发送 `credentials: "same-origin"` 的 SSE 请求，并固定加入：
   - `Content-Type: application/json`
   - `Agw-Js-Conv: str`
8. BDMS/WebMSSDK 在网络层处理命中的 `/chat/completion` 请求，追加或刷新 `msToken` 和 `a_bogus`。这两个字段不是业务 Payload 组装器产生的。

### 4.2 公共查询参数的准确来源

| 参数 | 当前网页代码中的来源 |
| --- | --- |
| `aid`, `real_aid` | 统一参数 `aid`，网页常量 `497858` |
| `device_id` | TTwid 配置中的 `web_id` |
| `web_id` | Tea SDK `getToken()` 返回的 `web_id` |
| `tea_uuid` | Tea SDK `getToken()` 返回的 `user_unique_id` |
| `pc_version`, `doubao_pc_version` | 构建常量 `3.27.0` |
| `version_code` | 统一参数管理器中的 Web 协议版本 `20800` |
| `language` | `flow_lang` Cookie，其次为 `i18nextLng` 本地存储 |
| `region`, `sys_region` | `flow_user_country` Cookie/本地存储；本次为 `CN` |
| `device_platform`, `doubao_device_platform` | 统一参数管理器的设备平台；网页为 `web` |
| `web_platform` | 固定默认值 `browser` |
| `web_tab_id` | 页面模块初始化时生成一次 UUID v4，同一标签页内复用 |
| `samantha_web` | 固定值 `1` |
| `use-olympus-account` | 固定值 `1` |
| `fp` | 验证模块 `getFp()`；页面生命周期缓存 |
| `msToken` | WebMSSDK 状态和令牌刷新请求维护 |
| `a_bogus` | BDMS/WebMSSDK 在请求发送阶段动态生成 |

### 4.3 主要 Payload 字段的准确来源

| Payload 路径 | 生成规则 |
| --- | --- |
| `client_meta.conversation_id` | 当前服务端会话 ID |
| `client_meta.local_conversation_id` | 尚未创建服务端会话时使用的本地会话 ID；已有会话时不发送 |
| `client_meta.bot_id` | 消息或当前会话的 Bot ID |
| `client_meta.last_section_id` | 当前会话 `last_section_id` |
| `client_meta.last_message_index` | `Number(conversation.message_index)` |
| `messages[0].local_message_id` | UUID v4 |
| `messages[0].content_block[0].block_id` | UUID v4 |
| `messages[0].message_status` | 普通可见消息映射为 `0` |
| `option.create_time_ms` | 消息创建时的 `Date.now()` |
| `option.unique_key` | 本次发送会话的 UUID v4 |
| `option.need_create_conversation` | 是否存在 `local_conversation_id` |
| `option.recovery_option.is_recovery` | 首次请求为 `false`；SSE 重试时改为 `true` |
| `option.recovery_option.req_create_time_sec` | `floor(create_time_ms / 1000)` |
| `option.start_seq` | 首次请求为 `0`；SSE 重试时使用已接收事件序号 |
| `chat_ability.ability_type` | 视频生成为 `17` |
| `chat_ability.ability_param` | `{ratio, model, duration}` 的 JSON 字符串 |
| `ext.fp` | 与查询参数相同的页面指纹 |

### 4.4 对“直接 HTTP”方案的结论

业务 Payload 可以在 Python 中完全重建，但单独使用 `httpx` 不能稳定重建最后的风控层。当前网页明确把 `/chat/completion` 放进 WebMSSDK 的处理路径，`msToken` 和 `a_bogus` 在 Fetch 发送阶段才加入。

可行实现是：Python 负责调度和组装业务参数，在账号对应的 Playwright 页面中执行 `fetch('/chat/completion', ...)`。它不需要点击豆包 UI，仍属于接口调用；同时页面已经加载的安全 SDK会处理 Cookie、`msToken` 和 `a_bogus`。把完整 URL 从一次抓包复制给 `httpx` 只适合作为短时诊断，不适合作为任务系统。

## 新建会话实测（2026-07-13）

普通聊天页点击“新建会话”时不会立即调用服务端创建接口。页面先产生本地 ID，例如 `local_1531671932183319`；首次发送消息时仍调用同一个 `POST /chat/completion`，并通过以下字段让服务端隐式创建会话：

```json
{
  "client_meta": {
    "local_conversation_id": "local_1531671932183319",
    "conversation_id": "",
    "bot_id": "7338286299411103781",
    "last_section_id": "",
    "last_message_index": null
  },
  "option": {
    "need_create_conversation": true,
    "conversation_init_option": {
      "need_ack_conversation": true
    }
  },
  "ext": {
    "conversation_init_option": "{\"need_ack_conversation\":true}"
  }
}
```

这次实测没有出现 `/samantha/thread/create` 请求。首次 `/chat/completion` 返回 200 后，页面用服务端分配的 `conversation_id=38434782802128386` 请求 `/im/conversation/info`；响应的 `extra.local_conversation_id` 与上面的本地 ID 一致，证明二者是同一次创建流程。

因此任务系统不需要先单独调用“新建会话接口”。对于新任务，直接在第一次视频 `/chat/completion` 中传空 `conversation_id`、本地会话 ID，并把 `need_create_conversation` 设为 `true` 即可。`/samantha/thread/create` 虽然仍存在于网页代码中，但不属于当前普通聊天首次发送的实际链路。

## 新会话视频生成冒烟测试（2026-07-13）

使用已登录账号实测提交了一个 5 秒、1:1、Seedance 2.0 Mini 的文生视频任务。首次请求直接使用 `/chat/completion`，关键增量字段为：

```json
{
  "client_meta": {
    "local_conversation_id": "local_1013903016017197",
    "conversation_id": "",
    "bot_id": "7338286299411103781",
    "last_section_id": "",
    "last_message_index": null
  },
  "option": {
    "need_create_conversation": true
  },
  "chat_ability": {
    "ability_type": 17,
    "ability_param": "{\"ratio\":\"1:1\",\"model\":\"seedance_v2.0_mini\",\"duration\":5}"
  }
}
```

接口返回 HTTP 200，SSE `SSE_ACK` 同时返回新建会话信息：

- `conversation_id`: `38434856388590850`
- `section_id`: `38434856388591106`
- `question_id`: `50005186711132162`

页面先显示“视频正在生成中”，随后显示“你的视频生成好了”。这证明新会话不需要额外的创建请求，创建会话与提交视频可以合并为一次 `/chat/completion`。

## 5. 结果查询

```http
POST https://www.doubao.com/im/chain/single?<公共环境参数>
Content-Type: application/json
Cookie: <当前账号浏览器会话>
```

```json
{
  "cmd": 3100,
  "uplink_body": {
    "pull_singe_chain_uplink_body": {
      "conversation_id": "<conversation_id>",
      "anchor_index": 9007199254740991,
      "conversation_type": 3,
      "direction": 1,
      "limit": 20,
      "ext": {"pull_single_chain_scene": "multi_device_red_dot_sync"},
      "filter": {"index_list": []},
      "evaluate_ab_params": "",
      "evaluate_common_params": ""
    }
  },
  "sequence_id": "<uuid>",
  "channel": 2,
  "version": "1"
}
```

注意协议字段确实拼写为 `pull_singe_chain_uplink_body`（`singe`），不能自行改成 `single`。

本次完成消息的关键结果：

- `ext.creativity_scene = "gen_video"`
- `ext.ai_creation_res_code = "0"`
- `ai_creation_tool_list[0].tool_name = "text_to_video"`
- `ai_creation_tool_list[0].req_key = "seedance_v20_fast_flow"`
- `ai_creation_tool_list[0].task_type = 6`
- `ai_creation_tool_list[0].status = 4`（完成）
- 消息 `content` 是 JSON 字符串；解析后在 `block_type=2074` 的 `creation_block.creations[0]` 中取得结果
- `creation.type = 2`、`creation.id = task_id`
- `creation.video.status = 3`
- `creation.video` 包含 `vid`、封面、宽高、实际时长、`download_url` 和字符串化的 `video_model`
- `video_model` 再解析一次后可得到 720p H.264 与 1080p H.265 的媒体信息；其中 `main_url` 是 Base64 编码的临时媒体地址

本次从提交到完成约 126 秒。下载地址带签名和有效期，应在任务完成后及时保存，不作为永久资源地址。

## 6. 软件实现建议

创建任务阶段使用账号对应的 Playwright 持久化上下文，由真实页面生成会话和动态风控参数；结果阶段可以在同一上下文内调用 `/im/chain/single`。应用数据库保存 `conversation_id`、用户消息 ID、`task_id`、模型、比例、时长和状态，不保存 `msToken`、`a_bogus` 或完整 Cookie。
