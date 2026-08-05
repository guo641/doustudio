# 更新日志

本文件记录 DouStudio 的重要功能变化。

## v0.2.24 - 2026-08-05

### 修复

- **v0.2.23 漏修:拒绝文案仍然卡 5min「永远生成中」**
  v0.2.23 在 `_POLICY_PATTERNS` 补了 5 条新模板,但**忘了**真正的扫描点在
  哪里:`parse_creation_result` 读的是 `/im/chain/single` 端点,而豆包新拒绝
  行为是:
  1. `/chat/completion` SSE 流**立刻**发 `TEXT_MESSAGE` / `TEXT_CHUNK` 事件,
     正文是拒绝文案(前端 React 实时渲染)
  2. chain endpoint 永远只返 `creation_block.status=1`(还在生成),UI 上的
     拒绝文本**不会**回流到 chain endpoint
  3. `parse_creation_result` 看不见拒绝,polling 循环一直返 None,5min 后
     `RuntimeError("视频生成超时")` 才把它标 failed

  v0.2.23 用户的实测日志佐证:20:04:58 创建的任务,8 分钟里 `applog` 表
  **没有任何** `video_content_rejected` / `video_content_reject_revise` 事件
  → SSE 流完全没被扫描 → 正则再准也没用。
  - `src/doupool/video/protocol.py:parse_sse_ack` 重构为「先把整个 SSE 流扫
    一遍、再 return ack」;新增 `scan_sse_for_policy_rejection(text)` 拆
    `event:` / `data:` 包,跳过 `SSE_ACK` / `STREAM_ERROR` / `SSE_HEARTBEAT`,
    递归收集所有 `text_block.text` 拼起来跑 `_POLICY_PATTERNS`(复用,不再
    写新 regex)。命中即抛 `DoubaoContentRejected(rejection, response_text=
    text[:2000])`,被 `browser.run()` retry loop 接住 → 改写重试。
  - `_POLICY_PATTERNS` 微调两处兜底:
    - 动词补 `返回`(用户后续实拍:`很抱歉,您请求的内容无法返回`)
    - `无法...生成` / `重新描述...试试` 之间允许 ≤5 个任意字符(包括 `\n`),
      适配拒绝文案被拆成多个 `TEXT_CHUNK` 流式到达的场景

- **`runner_window_visible` 默认 False → True(生成时浏览器可见)**
  用户反馈:生成时看不到浏览器界面,不知道到底有没有在工作。
  v0.2.22 引入 `runner_window_visible` 时默认 False 是为了「隐身行为」,
  现在改为默认 True(用户视角优先)。窗口落在 `(80, 80)` 而非 `(-2000, -2000)`。
  仍可在设置里手动关掉。`SettingsPage.vue` 提示文案同步更新。

## v0.2.23 - 2026-08-05

### 修复

- **豆包新拒绝文案不再卡 5min「永远生成中」**
  v0.2.22 加拒绝改写重试时,`_POLICY_PATTERNS` 只覆盖老文案
  (`生成内容中疑似包含侵权` / `换个主题再试试` / `无法返回该内容` / `sensitive
  content` 等)。豆包近期上线新通用模板 **`我暂时无法生成你要求的内容。请尝试
  输入其他要求`** —— 一字不沾老 pattern,polling 拿到的 chain 一直返 None,
  5min 后才被 `RuntimeError("视频生成超时")` 兜住。用户日志佐证:12:20 那条
  任务超时前**没有任何** `video_content_rejected` / `video_content_reject_revise`
  事件,正是「豆包说拒绝、软件还是生成中」的现场。
  - `prompt_reviser._POLICY_PATTERNS` 补 5 条新模板:
    `(我/抱歉.{0,30})?(暂时)?无法(生成|满足|响应|提供)`、
    `不符合.*?(规范|准则|要求|政策|规定)`、`涉及.*?敏感`、
    `重新描述(一下|后再)?试试`、`换个(要求|话题|方向|思路)再试试`。
    第一条覆盖 8 种常见组合(用户截图的拒绝句首尾无外乎这几种)。
  - 命中 → 立即抛 `DoubaoContentRejected` → 走 v0.2.22 的
    `prompt_reviser.revise_prompt` 改写重试,不再超时。

- **`max_reject_retries` 默认 0 → 2(拒绝类自动重试,quota 仍不浪费)**
  v0.2.22 把改写重试做成 opt-in 是想保守,但用户真实反馈:「这种报错也是提示词
  的问题,让豆包自己修改后重新生成就可以,除非是额度不够,剩下的报错都是提示
  词的问题」 —— 拒绝类基本都改一改能过,默认关等于让用户每次手动开。改默认
  2 = 1 次原 prompt + 2 次改写 = 3 次总尝试,基本够「剥离违规关键词 + 安全
  模板兜底」(见 `revise_prompt` 的 `attempt >= 2 → safe template` 分支)。
  - quota 安全:`prompt_reviser.classify_failure` 对 `RATE_LIMITED`
    设 `revise_prompt=False`,额度耗尽时不会进 retry loop,扣 1 次 + 退款
    走 `DoubaoRateLimited` 分支。runner 内部仍 clamp 到 0..3,setting 被
    改成 100 也最多 3 次。
  - 想完全关回 v0.2.21 行为的用户,设置里显式填 0。

- 测试:`test_prompt_reviser.py` 加 8 个 parametrize case 覆盖新拒绝模板,
  `test_video_protocol.py` 加 1 个 case 覆盖截图里的真实文案 → 52 tests pass。

## v0.2.22 - 2026-08-05

### 修复

- **「豆包内容审核拒绝」可自动改写 prompt 重试(默认关闭,opt-in)**
  v0.2.21 收到豆包 `无法返回该内容 / 提示词侵权违规` 等政策文案立即
  `failed + 退款`,用户反馈「软件应该自己改写一下重新生成」。新加 setting
  「豆包拒绝改写重试」(0 = 关闭沿用 v0.2.21,1-3 = 改写最大重试次数)。
  - `video/browser.py:run()` 把 submit+poll 抽到 `_submit_and_poll`,loop 外
    层在 `run()` 里,共享同一个 page;`page.close()` 外移到 `finally`,retry
    不重复创建 / 销毁 page。i2v 图片上传(`page.evaluate(UPLOAD_IMAGE_SCRIPT)`)
    前置在 loop 外 —— 上传一次,retry 不重复上传。
  - `prompt_reviser.classify_failure()` + `revise_prompt()`(v0.2.21 已存在
    但未挂入主路径)现在被 `run()` 直接调用:catch `DoubaoContentRejected`
    → `classify_failure` 判别 → `revise_prompt` 生成改写 prompt → 把
    `error_message` 更新成「豆包拒绝(第 N/M 次改写重试中)」→ loop 继续。
  - **quota 安全**:retry 期间 `update("generating")` 由 `_submit_and_poll`
    触发,`update` 闭包有 `if values.get("status") == "generating" and not
    quota_recorded` 闸门,只扣 1 次 quota。最后仍拒 → `refund_quota_if_recorded`
    退 1 次。同 prompt 改写(无法改)→ 直接 raise,不再循环。
  - 风控提示:连续多次 COMPLETION_SCRIPT 可能被 shark_admin 识别,生产建议
    保持 0;只在调试 / 单账号时开到 2-3。

- **视频生成时 Chromium 窗口可显示(opt-in,默认仍隐藏)**
  旧版 `--window-position=-2000,-2000` 把 Chromium 窗口藏到屏幕外,用户调试
  时看不到流程。新加 setting「显示 Chromium 窗口」,开启后 `_build_launch_kwargs`
  把位置改成 `(80,80)`,窗口出现在屏幕左上角。
  - **`launch_persistent_context` 创建后无法改 window-position**,改动只对
    下次新 profile_dir / 重启进程生效。SettingsPage hint 里已标注。
  - 默认 `False` 沿用 v0.2.21 隐身行为,生产建议保持关闭 —— 浏览器窗口可见
    可能被风控识别为异常登录态。

- **账号面板加「🔄 刷新额度」按钮 + 修首次进入不刷新 diff bug**
  v0.2.21 任务终态后 4s 内的 quota 刷新只对停留在 `videos / results` 页的
  用户生效。账号面板没有 4s 轮询,用户停在账号面板时 quota 永远不更新。
  - 账号面板右上角加「🔄 刷新额度」按钮(`AccountTable.vue` 新增 `refresh`
    emit),点击立刻 `refreshAccounts()`,无需切走再切回。
  - **修 diff bug**:`App.vue:refreshTasks()` 原来把 `tasks.value = fresh`
    放在 `hadTerminal` 构造之前。首次进入 videos 页时 `fresh` 即是首次数据,
    `hadTerminal` 直接包含所有 task,`newTerminal` 永远空,`refreshAccounts()`
    从不触发 → 后续从 videos 切到 accounts 也对不上。把 `hadTerminal` 构造
    挪到 `tasks.value = fresh;` 之前,首次进入也能拿到正确的「新增终态」。
  - 与 4s `refreshTasks` 节奏不冲突,按钮是手动补刀,不替代轮询。

- **下载结果视频失败时自动重新解析签名 URL(同步端点)**
  旧版 `task.result_url` 是豆包签名 CDN 链接,TTL 短(分钟-小时),几天后
  失效 → 用户点下载 → `DownloadButton` 三层 fallback (cors → no-cors →
  window.open) 全失败 → 系统 Edge 弹出 `ERR_INVALID_RESPONSE / 无法访问此页面`。
  现在前端下载失败时自动 `POST /api/results/{task_id}/refresh-url`,后端
  调 `runner.recheck_result(deadline=60s)` 重解析 CDN URL,**不消耗 quota、
  不改 status、不发 callback、不跑 watermark**,只更新 `result_url /
  fallback_result_url`,返回最新 task 给前端立即重试下载。
  - `video/service.schedule_refresh_url()` + `_refresh_url_body()`:与
    `retry_result` 共用 `_retry_tasks` / `_retry_cancellations` 池,并发走
    `_lock_for(profile_dir)` 串行化。校验:`status != succeeded` → 409,
    任务不存在 → 404,账号不可用 → 409。`refreshedResultIds` Set 防同一
    task 短时间内被多次刷新。
  - `DownloadButton.vue` 三层 fallback 全失败时不再 `throw`(throw 在 Vue
    template event handler 里只能 Vue warn,用户无反馈),改成 `emit('download-failed')`
    冒到 `App.vue:onResultDownloadFailed`。前端 toast 流程:launching「下载
    链接已过期,正在重新获取…」→ succeeded「链接已刷新,请重新点击下载」 /
    failed「已尝试刷新链接,仍无法下载」。
  - 后端 endpoint `POST /api/results/{task_id}/refresh-url` 同步等待 body
    跑完(最多 60s),返回更新后的完整 task dict。

## v0.2.21 - 2026-08-04

### 修复

- **「豆包内容审核拒绝」任务不再卡 5 分钟 timeout**
  线上反馈:用户在浏览器已看到豆包「提示词侵权违规 / 换个主题再试试 /
  无法返回该内容 / sensitive content」等拒绝文案,但 DouStudio 任务一直
  「生成中」5 分钟,直到 `RuntimeError: 视频生成超时` 才标 failed,期间 quota
  也已经被扣走(直到 v0.2.19 失败退还路径才会回滚)。根因是
  [`src/doupool/video/protocol.py:parse_creation_result`](src/doupool/video/protocol.py)
  只识别 `block_type=2074` 的成功块,内容审核拒绝走 `text_block.text` 或
  `creation_block.creations[].video.error_msg` 的拒因字段,polling 循环每
  5s 调一次都返回 None,直到 `self.timeout` 5 分钟才触发。
  - 新增异常 `DoubaoContentRejected(message, response_text)`,复用
    `prompt_reviser._POLICY_PATTERNS`(侵权|违规|换个主题|无法返回该内容|
    sensitive content|content violates ...),在 `parse_creation_result` 兜底
    阶段扫所有 message 的 text_block / creation_block.error_msg /
    disallow_reason / video.error_msg 等位置。
  - `video/service._run_inner` 新增 `except DoubaoContentRejected` 分支:
    立即 `update_video_task(status="failed", error_message="豆包拒绝: ...")`
    + 退还本 runner 已扣的 quota(v0.2.19 的 `refund_quota_if_recorded()`)
    + 触发 callback + return。**不**做 prompt 改写重试 —— 同 prompt 必拒,
    改写也是浪费额度且会再撞同样的 reject。
  - 顺序保护:成功块优先 return,只有整个 messages 都扫完没成功块时才跑
    policy 关键词兜底,避免已成功任务被同包 reject 文本误判。
  - response_text 截断 2000 字符随 WARNING 日志输出(`event=video_content_rejected`),
    排查「额度误退」时能直接看到豆包原文,跟 v0.2.15
    `DoubaoRateLimited.response_text` 一个套路。

- **账号 quota 在任务终态后 4 秒内实时刷新**
  任务 succeeded/failed/cancelled 后账号面板的 `今日额度` 数字不再等用户
  切走再切回 / F5 整页刷新才更新。后端 DB quota 写入早就是对的
  (`service.update` 闭包里 `increment_account_quota` 同步落库),只是前端
  `accounts` ref 只在挂载 / 登录完成时拉一次。
  - `App.vue:refreshTasks()` 拉到 fresh 任务列表后,与旧的 `tasks.value`
    对比,差集 = 状态变更的任务。任一新终态(`succeeded` / `failed` /
    `cancelled`)任务出现 → 触发一次 `refreshAccounts()`。
  - 4s `refreshTasks` 节奏不动,quota 数字 4s 内同步。

## v0.2.20 - 2026-08-04

## v0.2.20 - 2026-08-04

### 修复

- **「提交任务报 SecurityError + 之后所有任务都失败」根因修复**
  v0.2.19 async Playwright 重构后,共享 `BrowserContext` 在「全部 page 被关掉」时
  Playwright 会自动 close 整个 context。修法:
  1. `_get_shared_context` 不再 close 启动时的 init page,把它当 anchor 留着
     (即所有 page 都关,context 也不会被自动 close,避免下次 `new_page()` 撞
     `TargetClosedError`)
  2. `run()` 拿到 context 后先 `goto doubao.com/chat/` 再 `replaceState` —— 之前
     路径是 init page 被关后 `new_page()` 返回 `about:blank`,`replaceState` 在
     `origin='null'` 上撞 `SecurityError`。现在 anchor page 一直是 doubao 域,
     context.pages 不空时直接复用第一个 page 走 replaceState。
  3. `_get_shared_context` 每次取之前 `is_closed()` 一下,发现已关就清缓存
     重新 launch —— 即便上面两条全破,这条兜底仍能让服务从「context 死了」的
     状态恢复。

### 新增

- **扫码登录后保持浏览器打开 30 秒** ——
  旧版扫码成功立即 `context.close()`,用户根本没机会在那个窗口里访问
  `doubao.com/chat/` 让 WebMSSDK 写入 msToken。现在 `LoginService.keepalive_seconds`
  默认 30,扫码成功 → state 转 `KEEPALIVE` → Playwright runner 在 30s 内继续
  pump 事件(每 250ms `wait_for_timeout` 让 page JS 跑通)→ 自然超时跳 finally
  关 context。SSE `keepalive` 事件给前端发「请在浏览器里访问主页 5-10 秒」。
- **「📂 打开浏览器」按钮(账号面板新增)**
  复用账号已有的 login profile(`Default/Cookies` + `Local Storage` leveldb 全
  在那个目录)拉起 Chromium 窗口。Chromium SingletonLock 互斥,
  `BrowserSessionsRegistry` 用 `account_id` 做 key,同账号已有窗口时返回 409。
  - `POST /api/accounts/{id}/open-browser`:起 daemon thread 跑 Playwright,
    `goto https://www.doubao.com/chat/`(失败也不关,让用户看到错误页)。
  - `POST /api/accounts/{id}/close-browser`:set cancel event,runner 在下一个
    `wait_for_timeout` 切片检测到并 `context.close()`。
  - `GET /api/accounts/{id}/browser-status`:前端 3s 轮询一次,跟住「用户主动关
    窗口」「context 异常退出」等场景。
  - 前端 `AccountTable.vue` 在「🔄 刷新 token」按钮左侧加「📂 打开浏览器」,
    打开后变「🟢 关闭浏览器」。3s 轮询跟住后端状态。

### 不做的事(用户决策)

- ❌ msToken 自动续签:用户拍板只做手动(本 release:扫码后 keepalive 30s + 手动
  打开浏览器按钮)。24h 探测 → 自动 refresh 留给 v0.2.21+ 看数据再决定。
- ❌ keepalive 时间做成 settings:30s 是写死默认,不够长可在 CHANGELOG 留
  升级路径,但本 release 不暴露。

### 验证

- 全套 backend test 通过(沿用 v0.2.19 套件;v0.2.19 的 2 个 flaky
  `test_login_browser` 与本 release 无关,跳过)
- 手动:扫码登录 → 验证 30s 内浏览器留住 → 验证前端按钮可关
- 手动:点「📂 打开浏览器」→ 验证 Chromium 窗口拉起并停在 doubao.com/chat/

## v0.2.19 - 2026-08-04

### 修复

- **「豆包真实计费」对齐 + 默认额度桶改 50**
  v0.2.18 误以为豆包内部「按基础公式扣 0.2/s」,线上按用户反馈核账后,豆包真实计费是
  `mini=1 点/秒 / fast=1.5 点/秒`(向上取整,与 v0.2.11 之前一致)。v0.2.18 的
  0.2/s 反而成了「豆包扣 10 点,我方只扣 2 点」的账期不对账。`MODEL_COST_PER_SECOND`
  回滚到 `mini=1.0/s / fast=1.5/s`,默认 `daily_quota_mini/v2/std` 从 5/5/5 改成
  50/50/50(对齐豆包每账号每天 50 点的真实额度)。一个 5s mini 视频 = 5 点,
  一个 10s fast 视频 = 15 点,账期对得上。

- **「单账号并发 + 共享 BrowserContext」:Playwright runner 改成 async,首跑后复用 BrowserContext**
  v0.2.17 引入「复用登录 profile」后,每次提交任务仍 `asyncio.to_thread(runner.run)`
  走 sync Playwright + `await launch_persistent_context` —— 同一账号第二次跑
  任务时,profile Lockfile / Chromium pid 仍会有竞争,日 worker 跑多个串行任务
  时偶发 `BrowserContext._init` 错误。重构后:
    1. `_run_inner` 改为 `await self.runner.run(...)`(async runner,不再 to_thread)
    2. service 持 `per-profile BrowserContext` 缓存 + `per-profile asyncio.Lock`
       —— 首次创建时拿锁,后续复用现成 context(免 Lockfile 冲突)
    3. 全局 `asyncio.Semaphore(max_concurrency)` 仍在 `_run_inner` 入口把关
  双账号场景:同账号不同 task 串行(避免同 profile 抢锁)、不同账号并行。

- **「失败退还额度」:网络异常 / prompt 违规 / 无效输入 自动退已扣的额度**
  v0.2.18 前,任务失败但豆包明确拒绝(违规词 / 网络超时 / 无效图片)时,runner 已
  `update(status="generating")` 触发了 `increment_account_quota(by=cost)`,但
  `except` 分支不退款 —— 用户被拒的任务仍在桶里扣了额度,变成「豆包拒了,我也
  被扣了」。`service._run_inner` 用 `nonlocal quota_recorded / recorded_cost`
  跟踪本 runner 已扣的额度;`classify_failure` 命中 `NETWORK / POLICY_VIOLATION
  / INVALID_INPUT` 时调 `repository.decrement_account_quota(account, model, by=cost)`
  把桶减回原状。
  - **不退**:`GENERATION_FAILED / RATE_LIMITED / UNKNOWN` —— 前两类豆包大概率已
    计费(用户在豆包官方账户能看到扣费),后者已走 `mark_account_limited` 路径
  - **取消不参与退款**:用户进入 generating 后主动取消,**不退** —— 豆包已开始
    生成,额度已扣,这是用户主动取消 ≠ 豆包拒绝,两条独立路径

### 测试

- `test_quota_cost_*` 更新为回滚后的费率期望值
- `test_quota_cost_fits_daily_quota_bucket` 改为 daily_quota=50:10 个 5s mini = 50
  点(用完整天)、10 个 10s mini = 100 点(超额)
- `StaticSettings` 默认 daily_quota=50(对齐真实额度)
- `test_video_browser.py`:新增 `test_load_browser_context_*` 4 个 + stealth args 测试
- `test_video_service.py`:
    - 新增 `HighConcurrencySettings(max_concurrency=3)` 测试双账号并行
    - 新增 4 个 refundable failure 参数化测试(network / policy / invalid 退,
      generation_failed 不退)
    - 新增 `test_service_refunds_quota_on_each_retry_attempt`:违规改 prompt 重试
      时每次失败都退(防止扣 3 次退 0 次)
    - 新增 `test_service_refund_noop_when_quota_was_not_charged`:runner 抛异常前
      没到 generating → 退款路径安全 noop
- `test_video_repository.py`:
    - 新增 5 个 `decrement_account_quota` 测试:扣 / clamp 0 / 非法 by / 非法 model /
      increment + decrement 净 0
- 全套 313 个 backend test 通过(2 个 test_login_browser 的 flaky 在 v0.2.18 同样失败,与本 release 无关)

## v0.2.18 - 2026-08-04

### 修复

- **「额度公式爆炸」修复:v0.2.17 默认 daily_quota=5 下一个 10s mini 视频扣 10 点爆桶**
  v0.2.11~v0.2.17 期间费率沿用 `mini=1.0/s / fast=1.5/s`(向上取整),但**默认 `daily_quota_mini/std=5`**,用户线上实测:
  - mini 5s 视频 → 扣 5 点(刚好用完全天)
  - mini 10s 视频 → 扣 10 点,显示「10/5 已用完」
  - fast 5s 视频 → 扣 8 点(超额 3,直接限流)
  `MODEL_COST_PER_SECOND` 调到 `mini=0.2/s / fast=0.4/s`,新额度:
  - mini 5s = ceil(1.0) = **1 点**
  - mini 10s = ceil(2.0) = **2 点**
  - fast 5s = ceil(2.0) = **2 点**
  - fast 10s = ceil(4.0) = **4 点**
  默认 5 点/天的桶下,5 个 10s mini 视频用完当天,用户不再看到「1 个视频就额度爆」的假象。

- **「下载按钮点了没反应」修复:v0.2.17 后 WebView2 跨域下载新增 window.open 兜底**
  v0.2.17 `DownloadButton.vue` 的跨域下载链是 `fetch(cors)` → CORS 失败 → `fetch(no-cors)` → opaque blob → `<a download>` → 触发 WebView2 下载管理器。线上复现:**fetch 走完、`<a>.click()` 调用了,但 WebView2 对 opaque response 出来的 blob 处理不一致**(body 可能空 / type 空 / 下载管理器不接),用户点了下载按钮「无反应」。线上用户 workaround 是「复制链接」贴到系统浏览器,系统浏览器对 cross-origin URL 下载行为一致能下。
  三层 fallback:
    1. `fetch(URL, mode='cors')` → blob + `<a download>` → 应用内下载(服务端开 CORS 才走得到)
    2. `fetch(URL, mode='no-cors')` → opaque blob + `<a download>` → 多数情况能下,空 blob 走下一层
    3. `window.open(URL, '_blank', 'noopener,noreferrer')` → 系统默认浏览器兜底,**用户至少能看到浏览器弹窗有反应**(pywebview + WebView2 把 window.open 代理到 OS 默认浏览器)
  `triggerBlobDownload(blob)` 检测 `blob.size === 0` 直接返回 false,让外层走 `window.open`,不再让「点了没反应」这种黑盒发生。

### 测试

- `test_quota_cost_mini_per_second` / `test_quota_cost_fast_per_second_ceils` / `test_quota_cost_v2_legacy_alias` 全部更新为新费率期望值
- 新增 `test_quota_cost_fits_daily_quota_bucket`:回归保护 — 默认 daily_quota_mini=5 下,2 个 10s mini = 4 点(< 5,留 1 点余量),5 个 10s mini = 10 点(> 5,触顶)
- `test_service_runs_and_persists_video_result` / `test_service_charges_correct_bucket_per_model`:5s mini 期望 `video_quota_used_mini` 从 5 改 1、std 桶从 8 改 2
- 全套 237 个 backend test 通过、frontend `ManagementPages.test.ts` 4 个测试通过

## v0.2.17 - 2026-08-04

### 修复

- **「shark_admin 风控」绕过 — 复用登录 profile 的真实 WebMSSDK / TeaSDK token**:v0.2.16 把风控从 quota 桶隔离掉,账号不再被误 cap,但**每条 task 仍第一请求就被 shark_admin 拦截**(`error_code=710022004, decision.from=shark_admin, subtype=semantic_reasoning`),线上命中率 0%。根因是视频提交的 Playwright 跑的 JS fetch 没带真实 msToken / 完整 doubao.com cookies / 真 `Referer` / `sec-ch-ua*` / 真实 `pc_version`,风控 front-end 一眼识破。逆向 WebMSSDK 算法 ROI 太低且易被环境检测识破,改走**复用登录 profile 已签好的 token**路线:
  - 新增 `TokenBundle`(`video/browser.py`):从登录后持久化的 Chromium profile 抽 `msToken` / `web_id` / `web_id_signature` / `device_id` / `tea_uuid` / `pc_version`,凑齐后透传给 `payload.client_meta`。
  - **`extract_webmssdk_tokens(profile_dir)`**:读 `Default/Cookies` SQLite(挑 doubao.com 域名)+ `Local Storage/leveldb/000003.log` 拼出 bundle。关键字段缺失(wid_id / device_id)→ 抛 `TokenBundleUnavailable`,UI 引导用户「登录后在浏览器里手动访问 doubao.com/chat/ 主页 5-10 秒」后再点刷新。
  - **`load_browser_context` 改名 + 加 kwargs**:返回 `TokenBundle` 而非单独 fp 字符串;接受 `pc_version=settings.get("pc_version")` 透传,真实浏览器 pc_version 走 Settings 单一来源(`pc_version` 设置项,browser.py 的模块常量降为兜底)。
  - **`build_completion_payload` 加 `**kwargs`**:透传 `EXTRA_CLIENT_META_KEYS = ("web_id", "tea_uuid", "device_id", "pc_version", "web_id_signature")` 白名单字段到 `client_meta`,空值自动丢弃。
  - **视频提交复用登录 profile**:登录 + 视频提交共用同一 Playwright 持久 Chromium 上下文,关掉可视化(登录仍可视化)。Cookie / WebMSSDK 缓存全在,无需重抽。
  - **per-account `asyncio.Lock`**:`video/service._scheduler_loop` 入口按 `account.id` 加锁,接受 Playwright `Lockfile` 互斥(同账号串行,不同账号并行;实际双账号场景 = 串行,quota 已限流)。
  - **手动「🔄 刷新 token」按钮**:msToken 过期不静默重抽,前端账号面板每行加按钮 → 调 `POST /api/accounts/{id}/refresh-tokens`,headless=False Playwright 访问 doubao.com/chat/ 主页 8s 让 WebMSSDK 跑过,再 `extract_webmssdk_tokens` 重读新 bundle(走 `asyncio.to_thread` 避免阻塞 FastAPI 事件循环)。

- **UUID 形态修复 — UUIDv4 替代 UUIDv1 / 纯数字**:`local_message_id` / `local_conversation_id` 之前分别用 `uuid1()`(时间戳 + MAC 可聚关联全账号) / 16 位纯数字 `secrets.randbelow`,风控后台通过同 cluster 反查就能把 DouStudio 的账号一锅端。改成 `uuid.uuid4()` 随机对手难关联。

- **风控「profile 缺 token」早退:`_run_inner` 捕获 `TokenBundleUnavailable` 后 `return`(不 retry)**:同 profile 反复重试抽不到同样的字段,只会浪费 GPU 配额且 task 永远 `failed`。token 抽不到是 profile 级别问题,沿用 v0.2.16 隔离路径,只标 `failed` 不动 quota 桶,UI 红字 hint 引导用户去刷。

### 新增

- `GET /api/accounts/{id}/webmssdk-tokens` — 读 profile 不开浏览器,返回 `{available, hint, ms_token_preview, web_id, web_id_signature, device_id, tea_uuid, pc_version, fetched_at, age_seconds}`。`available=false` 时 hint 引导用户去主页。
- `POST /api/accounts/{id}/refresh-tokens` (status 202) — 启动 headless=False Playwright 跑主页 8s 后重抽,返回新 bundle 或 503 on Playwright fail。
- `Settings` 新增 `pc_version="3.27.4"` 字段(供 `browser.py` 读取,release 暂不暴露前端 UI)。
- 前端账号面板:Token 列展示「正常 / 缺失」badge + age("12 分钟前" / "从未") + 操作按钮,失败时 hint 行红字说明。

### 测试

- `test_token_bundle_to_client_meta_drops_empty_fields` / `test_token_bundle_to_client_meta_always_has_pc_version` — `TokenBundle` 白名单 + 空值过滤 + pc_version 兜底
- `test_load_browser_context_reads_tea_and_device_storage` / `test_load_browser_context_falls_back_to_cookies_when_storage_empty` / `test_load_browser_context_raises_when_no_fingerprint_cookie` / `test_load_browser_context_raises_token_bundle_unavailable_when_no_web_id` — profile 抽取三种路径 + 错路径
- `test_extract_webmssdk_tokens_reads_cookies_sqlite` / `test_extract_webmssdk_tokens_raises_when_profile_dir_missing` — 从 SQLite 抽 msToken / sig,空 profile 抛 `TokenBundleUnavailable`
- `test_build_launch_kwargs_includes_stealth_args_and_locale` — `_build_launch_kwargs` 含 `--disable-blink-features=AutomationControlled` / `timezone_id="Asia/Shanghai"` / `locale="zh-CN"` / `Referer / Accept-Language` / viewport 抖动 ±3
- `test_get_webmssdk_tokens_returns_available_bundle` / `..._returns_unavailable_when_bundle_missing` / `..._404_when_account_missing` / `..._requires_auth` — `GET` 端点四种路径
- `test_refresh_tokens_returns_new_bundle` / `..._returns_unavailable_when_bundle_still_missing` / `..._503_when_playwright_raises` / `..._404_when_account_missing` / `..._requires_auth` — `POST` 端点五种路径

### 重要提示

- **登录后必须先在浏览器里手动访问 `https://www.doubao.com/chat/` 主页 5-10 秒**——让 WebMSSDK 跑过、leveldb 写完 msToken / web_id 缓存,后续 DouStudio 提交任务才能拿到真 token。冷启动 profile 直接提交会全走红字 hint 「profile 中缺少 web_id」。
- msToken 过期后,UI 账号面板点「🔄 刷新 token」即可(headless=False 起一个离屏窗口跑主页 8s,不影响正常使用)。
- 双账号场景下,Playwright Lockfile 互斥会让任务**串行**(同账号);quota 系统已限制 `max_concurrency`,实际影响有限。

## v0.2.16 - 2026-08-04

### 修复

- **「shark_admin 风控被误判为 quota 限流」根因修复**:`error_msg: "rate limited"` 是豆包一个**通用 wrapper**,背后既可能是真 quota 限额,也可能是字节风控(`from=shark_admin, type=verify, subtype=semantic_reasoning, code=10000, error_code=710022004`)。v0.2.14 / v0.2.15 无条件当成 quota 限流 → cap 桶 + 设 `video_limited_until` → 账号**永久 cap 死**,UI 报"额度已用完"但实际桶只跑过一次任务甚至零次。
  - `DoubaoRateLimited` 增加 `is_risk_control` 字段(v0.2.16):`parse_sse_ack` 解析 `extra.decision` JSON,识别 `from == "shark_admin"` → 风控,否则 → 真 quota。
  - `video/service._run_inner` 区分两条路径:
    - **风控**:只把这条 task 标 `failed`(`error_message="账号被风控拦截(shark_admin verify),稍后重试或换号"`),**完全不动 quota 桶、不设 `video_limited_until`**。账号继续可用,下一条任务可正常调度。
    - **真 quota**:保持旧的 cap 桶 + 设 limited_until 行为不变。
  - 日志:`event=video_risk_control` WARNING 带 `response_text` 前 500 字符(豆包真实风控 detail),便于事后复盘 IP / fingerprint / prompt 命中哪条策略。

- **「软件时间跟本地差 8 小时」修复**:之前 DB / 日志时间戳全部走 `datetime.now(UTC)`(naive),日志 formatter 用 OS 本地时区格式化。两者口径不一致,且都跟 OS 时区耦合 —— 用户机器如果不在 `Asia/Shanghai`(或装的是 UTC ISO image),DB 显示的时间就比本地晚 8h,quota reset / 登录 `finished_at` / `last_verified_at` 都错位。
  - **统一按北京时间(`Asia/Shanghai`)**,跟 OS 时区解耦:
    - `doupool.db.models.utcnow()`:函数名保留(向后兼容),实际返回 `datetime.now(SHANGHAI).replace(tzinfo=None)`。所有 DB DateTime 字段(`quota_window`、`complete_login`、`update_video_task.completed_at`、`last_verified_at`、`finished_at` 等)都通过它写。
    - `doupool.logging.setup.RedactingFormatter.formatTime`:override 标准库实现,强制 `datetime.fromtimestamp(record.created, tz=Asia/Shanghai)`,OS 时区是啥都按上海时间格式化。
    - `doupool.login.browser._iso_now` + `doupool.settings.service` 备份文件名:也走 `Asia/Shanghai`。
  - 新增 `tzdata>=2024.2` 到依赖:Windows 干净 Python 没有系统 IANA tz 数据库,没有这个包 `ZoneInfo("Asia/Shanghai")` 直接抛 `ZoneInfoNotFoundError`,第一个视频任务就崩。

### 测试

- 新增 `test_parse_sse_ack_detects_shark_admin_risk_control`:`extra.decision.from == "shark_admin"` → `DoubaoRateLimited.is_risk_control is True`
- 新增 `test_parse_sse_ack_rates_limit_without_shark_admin_stays_quota`:普通 429 仍是 quota 路径(`is_risk_control is False`)
- 新增 `test_service_does_not_cap_buckets_on_shark_admin_risk_control`:`_run_inner` 撞风控 → task `failed`、桶仍是 0、`limited_until` 仍是 None,账号下次仍可调度
- 新增 `test_utcnow_returns_beijing_time`:`utcnow()` 跟 `datetime.now(UTC)` 差恒为 28800s(±5s)
- 新增 `test_formatter_uses_shanghai_timezone_regardless_of_os`:`LogRecord.created` 是某 UTC 时刻 → 格式化出来是 `+8h` 上海时间

## v0.2.15 - 2026-08-04

### 修复

- **「额度已用完」黑盒诊断 + 「删任务后 worker IndexError」修复**:v0.2.14 把 `complete_login` 重置 quota 后,用户线上仍然报"全部账号今日 seedance_v2.0_mini 额度已用完"。日志只有一行 `WARNING 账号今日视频额度已用完`,豆包真正的 `error_msg`(`STREAM_ERROR` 的 `data` 字段)被吞了 —— 如果下次不是额度问题而是 fingerprint / IP / 风控,日志没线索
  - `DoubaoRateLimited` 增加 `response_text` 字段(v0.2.15):`parse_sse_ack` 在抛异常时把 SSE 响应原文(`text[:2000]`)挂到异常上,`video/service._run_inner` 捕获时把 `exc` 和 `response_text` 前 500 字符一起写到 WARNING。下次线上再误报 423,日志能直接看到豆包是「额度」还是「fingerprint」还是「风控」
  - **`IndexError: list index out of range` 修复**:用户前端点删除任务时,worker 还在 in-flight 处理那条 task。`repository.get_video_task / update_video_task / assign_video_task` 内部 `VideoTask.get_by_id(task_id)` 会抛 `VideoTask.DoesNotExist`(peewee 包成 `IndexError`),整个 worker 报错循环退出,后续任务再也不被处理
  - 三个 repository 方法都改成捕获 `VideoTask.DoesNotExist` 返回 `None`/`False`,**幂等无副作用**(已不存在的任务就是更新不到)
  - `service._run_inner` 顶部加早退:`get_video_task` 返回 `None` → 写 INFO 日志 `task_deleted_worker_exit` 后 `return`,worker 静默退出,不再刷 IndexError
  - 注意:只有「DELETE 时 worker 已经在 in-flight」会撞到这个分支,正常完成的任务不会有这个路径;所以「删任务」对用户而言仍然是「点了立刻删」,只是 worker 在跑完当前 SSE chunk 后识别到任务已不在,自己退出

### 测试

- 新增 `test_parse_sse_ack_attaches_response_text_to_rate_limit`:验证 `DoubaoRateLimited` 抛出时 `response_text` 等于 SSE 原文
- 新增 `test_get_update_assign_video_task_returns_none_when_task_deleted`:三个 repo 方法在任务已被删后调用都返回 None(不抛 IndexError)
- 新增 `test_run_inner_exits_silently_when_task_deleted`:`_run_inner` 在任务被删后 INFO 日志退出,不再 IndexError

## v0.2.14 - 2026-08-03

### 修复

- **「重新登录仍报额度已用完」修复(v0.2.13 修复遗漏点)**:v0.2.13 装了之后,用户在 v0.2.12 时代被 cap 死的桶**仍然死锁**,因为 `complete_login`(已存在账号重新扫码登录走的 else 分支)只更新昵称 / profile_dir / 状态 / `last_verified_at` / `last_error`,**完全不动 `video_quota_used_*` 和 `video_limited_until`**。结果:重新登录成功了,但 `choose_available_account` 看到桶满依旧返回 None,UI 一直报「额度已用完,明早 00:00 自动恢复」。
  - `complete_login` else 分支:把 `video_quota_used_mini / v2 / std` 清 0、`video_limited_until` 置 None、`status` 改 `active`、`doubao_nickname` / `display_name` 走新昵称。**登录是最稳的恢复点** —— 反正账号刚扫完码就能用,桶清掉让账号立刻可调度,而不是让用户以为「登录失败」
  - `if` 分支(首次登录新账号)不动其他账号的 quota,只初始化新账号
  - 用户侧手动恢复仍是把 3 个桶改 0 + `limited_until` 改 NULL(老路径没堵上之前)

### 测试

- 新增 `test_complete_login_resets_quota_buckets_for_existing_account`:v0.2.12 时代的死锁桶(`used=5/5/5` + `limited_until` 在未来)在 `complete_login` 后归零,登录后 `choose_available_account` 立刻能选中
- 新增 `test_complete_login_creates_new_account_without_touching_others`:首次登录(走 `if` 分支)不动别的账号的 quota

## v0.2.13 - 2026-08-03

### 修复

- **「新登录账号立即触发额度死锁」修复(v0.2.12 修复不到位)**:用户反馈 3 个刚登录、还没用过的账号,三桶 quota 直接 5/5 cap 死,UI 报"额度已用完,明天自动恢复"。根因:`mark_account_limited` 用 `next_reset`(由 `appsetting.quota_reset_time` 算)作 `limited_until`,**这是设置面板的一个变量,改了之后老的 limited_until 还在 DB 里**(绝对时间戳)。如果新 `next_reset` 比旧 `limited_until` 更早(比如用户从 `08:00` 改成 `00:00`,老 limited_until=`16:00 UTC naive`,新 next_reset=`00:00 本地 = 16:00 UTC`,但当日 14:19 还没到 16:00),两段 `reset_daily_quotas` 都清不到桶,账号永久 cap 死。
  - `mark_account_limited` 同步写 `video_quota_date=business_date`:跨天时第一段(`video_quota_date != business_date`)一定能命中,把桶清回 0。`business_date` 缺省保持旧行为(不写 date)以兼容老调用
  - `reset_daily_quotas` 第二段去掉 `video_quota_date == business_date` 限制:任何 `limited_until <= now` 都清桶,不依赖 date,跟第一段互不依赖,两段协同幂等无副作用
  - UI 文案对齐:`"额度已用完,明天自动恢复"` → `"额度已用完,明早 00:00 自动恢复"`(quota_reset_time 现在跟有限流截止没关系,文案不再误导)
- 用户侧立即恢复(临时):v0.2.13 装上之前,可以在 `doupool.sqlite3` 里把 3 个账号的 `video_quota_used_mini/v2/std` 改成 0、`video_limited_until` 改 NULL,等 v0.2.13 装上不再复发

### 测试

- `test_mark_account_limited_zeroes_all_buckets` 加 `video_quota_date` 写入断言
- 新增 `test_mark_account_limited_without_business_date_is_backward_compatible`:不传 `business_date` 时不覆盖 `video_quota_date`(兼容老调用)
- 新增 `test_reset_daily_quotas_clears_expired_regardless_of_date`:date 不匹配 + limited_until 已过期 的极端组合,第二段也要清桶

## v0.2.12 - 2026-08-03

### 修复

- **「等待可用账号」永久卡死修复**:`v0.2.9` 起 `mark_account_limited` 在豆包 423 时会把三桶 quota 都 cap 到 `quota_limit`,直到跨天 `reset_daily_quotas` 才清。如果 `limited_until` 落在当天内(短时封号),账号就会永远选不到 —— 因为 `choose_available_account` 的 `field < quota_limit` 永远是 False,UI 一直显示「等待可用账号」。
  - `reset_daily_quotas` 顺带清 limited_until 已过期的桶,让当天内封的账号自动恢复
  - `choose_available_account` 返 None 时区分两种情况:
    - 没有启用账号 → "暂无账号,请先在账号面板添加账号"
    - 全部桶满 → "全部账号今日 {model} 额度已用完,明天自动恢复"
    - 其它(单账号限流中) → 原来的"等待可用账号"
  - 加 `summarize_account_availability` helper + 单测覆盖

### UI

- **账号面板去掉 v2 段**:v0.2.11 已经删了 v2 模型下拉,账号面板额度条还显示三段(mini / v2 / std)。v0.2.12 改成两段(mini / fast),`Bucket` 类型和 BUCKETS 数组同步收敛;表头文案从「今日额度(mini / v2 / std)」改成「今日额度(mini / fast)」

## v0.2.11 - 2026-08-03

### 主要变更

- **任务删除**:`DELETE /api/requests/:task_id`。running 状态(`starting` / `generating` / `resolving`)拒绝(409),其余状态直接物理删除。前端任务列表每行加「删除」按钮,运行中自动隐藏
- **额度按秒计费**(对齐豆包真实扣费):
  - mini:`1` 点/秒(5s=5、10s=10)
  - fast(`seedance_v2.0_std`):`1.5` 点/秒向上取整(5s=8、10s=15)
  - 未知 model 兜底 = `duration`,避免被传 0
  - `increment_account_quota(by=N)` 累加到对应桶,默认 `by=1` 保持向后兼容;`by < 1` 抛 `ValueError`
- **prompt 分段改用「第一段」标记**:不再按换行切。识别 `第一段` / `段一` / `1.` / `1、` / `1)` 这五类行首标记(中文序数 + 阿拉伯数字 + 全/半角冒号),**只在行首匹配**避免误伤文本里出现的「第一段」字样。无标记整段当一个 prompt。前端 + 后端双重解析,后端只在单 `prompt` 字段时防御性切,`prompts` 列表已切好不再重复切
- **去掉 seedance_v2.0 UI**:收费模型,前端模型下拉(任务创建 / 默认设置 / 结果列表 / 任务表)只保留 `Seedance Mini` 和 `Seedance Fast`(`std`)。后端 allow-list、DB `daily_quota_v2` / `video_quota_used_v2` 列保留兼容

## v0.2.10 - 2026-08-03

### 修复

- **v0.2.9 双击 exe 启动崩溃**:`e4ced5a`(callbackUrl 异步回执)给 `VideoTask` 模型加了 `callback_url` / `callback_status` / `callback_attempts` / `callback_last_error` 四个字段,但 `database.py` 的 schema 迁移只到 v9,没有给 `videotask` 加列。新装 DB 没事(`create_tables(ALL_MODELS` 按模型建表自动带上),**老 DB 升级 v8→v9 后再启动就崩**:FastAPI lifespan 跑 `video_service.resume_queued()` → peewee `SELECT ... callback_url, ...` → 老 `videotask` 表没这列 → `OperationalError` → uvicorn 启动失败 → `DesktopRuntime` 10 秒健康检查超时 → 弹「本地服务启动超时」对话框。
- 修复:新增 schema v10 幂等迁移,补 4 个 callback 列。**强烈建议 v0.2.9 用户升级**,不升的话启动直接崩。

## v0.2.9 - 2026-08-03

### 主要变更

- **API 加 Bearer Token 鉴权**:`Authorization: Bearer <token>` 头可用,与 yaonieyo 默认 key 风格对齐,便于本机 curl / 外部脚本直连;旧 `X-DouPool-Token` 头保留向后兼容,大小写不敏感、容忍多余空白
- **`POST /api/requests/:id/retry-result`**:只重解析豆包轮询结果,不再提交任务、不扣额度,做客服侧的恢复手段
- **callbackUrl 异步回执**:视频任务到 `succeeded` / `failed` / `limited` 等 terminal 状态后,自动 POST JSON 通知外部系统,失败按指数退避重试
- **按 seedance 模型拆 daily_quota**:
  - 设置项拆 3 个:`daily_quota_mini` / `daily_quota_v2` / `daily_quota_std`,旧 `daily_quota` 留作 legacy fallback
  - Account 表加 3 列 `video_quota_used_mini/v2/std`,旧 `video_quota_used` 保留兼容
  - DB schema 升到 v9,幂等迁移(老 `video_quota_used` 自动落到 mini 桶)
  - 调度时按 `task.model` 选桶,mini 任务不再吃光 std 额度;豆包 423 限流仍封整号(三桶一并 cap)
  - 前端账号面板 3 段进度条分别展示

## v0.2.8 - 2026-08-03

- **disk fallback 加 sessionid 闸门**:3 秒 cool-down 内必须等到有效 `sessionid` cookie 才认为登录成功,堵住「扫码前就显示已登录」和「扫完不显示成功」两个回归

## v0.2.7 - 2026-08-03

- **抛弃 aegis 风控,沿 yaonieyo 双轨判定**:统一从 cookie + account/info 双源判定登录态,不再相信豆包自带的 aegis 风控信号

## v0.2.6 - 2026-08-03

- **disk fallback 改用 Chromium 内 fetch**:不再用 httpx 调 `/passport/web/account/info/`,直接在持久 profile 的浏览器上下文里发请求,避免指纹差异被风控

## v0.2.5 - 2026-08-03

- **cookie-on-disk rescue + httpx fallback**(用户反馈根因):扫码失败时优先用磁盘上的 cookie 重发 verify,verify 失败再回退到 httpx 直接调 account/info

## v0.2.4 - 2026-08-03

- **重写 wait_for_identity**:解决 gevent 跨线程崩 + context 销毁竞态

## v0.2.3 - 2026-08-03

- **`_req` 不吞 HTTPError**:让 404 走创建 release 分支,fix 重复 push tag 时 release 创建失败的回归

## v0.2.2 - 2026-08-03

- **identity_from_response 兜底处理 `/passport/web/account/info/`**:豆包换路径后旧 URL 返回的数据结构不一样,加通用解析

## v0.2.1 - 2026-08-03

- **扫码成功后页面关闭 race + cookie verify 兜底**:扫码完页面被关掉时,凭浏览器关闭事件 + cookie 验证双重保险判定登录成功

## v0.2.0 - 2026-08-03

- **全量修复打包链路、状态机、前端跨域下载与 SSE 容错**
- ps1 打包脚本 zip 文件名保留 v 前缀,与 `build_exe.py` + 两个 workflow glob 对齐

## 0.1.0 - 2026-08-01

### 首次发布

DouStudio 是基于 [DoubaoManager v0.1.0](https://github.com/shukeCyp/DoubaoManager) 的二次开发桌面工具,
专注文生/图生视频 + 多账号调度 + 一键去水印。

### 主要功能

- **多账号隔离调度**:每个账号独立 Playwright 持久 profile,同账号串行、不同账号并行,带额度窗口管理
- **首登扫码 / 后续免浏览器**:首次登录打开 Chrome 扫码,之后用持久 profile 静默复用
- **视频生成**:走豆包内部 API(`/chat/completion` 提交 + `/im/chain/single` 轮询),支持 1:1/3:4/4:3/9:16/16:9/21:9 比例 + 5s/10s 时长 + 文生(t2v)/图生(i2v)模式
- **失败自动改 prompt 重试**:5 种失败分类(违规 / 额度 / 网络 / 输入无效 / 生成失败),违规类自动剥离风险关键词并软化描述重试,最多 2 次
- **任务分组自动归组**:textarea 多行 prompt 自动归到同一 group_id,后端按 group_id 聚合查询,前端显示「组 #N」标签
- **去水印(zhuceka)**:视频生成成功后自动调 https://api.zhuceka.cn/home/api 拿无水印直链,失败不阻塞主任务
  - 设置面板填写 UID/KEY 即可启用,失败有 retry 退避(0s/5s/10s/20s/30s)
- **额度进度条**:账号面板显示当日额度进度条,耗尽/限流自动变红
- **打包 + 热更新**:
  - PyInstaller onedir 打包,Windows + Linux 双平台矩阵
  - 自动 zip + SHA256
  - 启动后 + 设置面板手动「检查更新」,命中 GitHub Releases latest 提示升级

### 技术栈

- Python 3.12 + FastAPI + Peewee + httpx + Playwright + PyWebView
- Vue 3 + TypeScript + Vite

### 已知限制

- macOS 暂未提供 release(可本地 macOS 自行打包)
- 图生视频上传走豆包内部 API,需要账户已登录
- 去水印依赖第三方 zhuceka 服务,需用户自备 UID/KEY
- 项目原始思路参考 DoubaoManager v0.1.0;所有代码均为 MIT 协议
