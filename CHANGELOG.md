# 更新日志

本文件记录 DouStudio 的重要功能变化。

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
