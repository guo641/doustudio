# 更新日志

本文件记录 DouStudio 的重要功能变化。

## v0.3.1.2 - 2026-08-12

**video runner 接 aegis solver —— 让图鉴在视频生成路径上真正自动拖。**

### 为什么做

v0.3.0 接了图鉴 + aegis solver,v0.3.1.1 加了 SettingsPage 凭证 UI,
但 **视频任务路径根本没接 solver**。用户加图鉴账号就是希望「提交任务
后不弹拼图,或者弹了也不用手动拖」,但 `runner.run()` 之前不会等 aegis
弹窗,导致:
- 弹窗在 submit 之前没出现 → 豆包风控挡 → STREAM_ERROR → fail
- 弹窗在 submit 中出现 → 拖到一半卡住 → 用户只能手动关 UI

用户原话(2026-08-12):
> 「哪个方向可以让图鉴发挥作用,我加入图鉴不就是为了让用户不用手动
> 拖拽吗?」

### 改动

- `src/doupool/video/browser.py`:
  - 新增模块级 async helper `_try_solve_captcha_in_video`(跑在 video
    异步事件循环里,与 login 的 sync wrapper 互不复用)
  - `run()` 在 `page.goto(doubao.com/chat/)` + `wait_for_timeout(1_500)`
    之后调用 helper(等 4s 让 aegis 弹窗出现,再 detect + solve)
  - `_submit_and_poll()` 的 poll 循环每 3 轮再调一次(防止生成中途
    aegis 偶发;helper 内部 cooldown 检查即时返,不影响轮询性能)
  - 失败/凭证关/solver 异常 → 一律 `mark_cooldown` + `update(error_message=...)`
    进度文案,**不 raise** —— 让 task 继续走原有失败路径,不让图鉴故障
    把已有流程干挂
- `src/doupool/video/service.py`:**不动** shark_admin 失败路径(留给
  v0.3.1.3 —— shark_admin 是服务端拦截,无浏览器 DOM,图鉴截不到图)
- `tests/test_video_captcha_hook.py`(新):13 个 helper 单测覆盖 —
  cooldown 跳过 / 无弹窗 / detect 异常吞 / happy path / wait/poll 时长
  / 凭证缺失 / solver 失败 / mid-run disabled / 任意异常 / update 通道
  异常 / login↔video cooldown 共享 / 模块常量合理区间
- 版本号 0.3.1.1 → 0.3.1.2(`__init__.py` + `config.py` 2 处 +
  `pyproject.toml`)

### cooldown 共享:`account_key = str(profile_dir)`

login keepalive 路径(v0.3.0)与 video runner 路径(v0.3.1.2)用同一个 key
调 `is_in_cooldown / mark_cooldown`,内部就是 `captcha/solver.py` 的模块级
`_captcha_cooldown` dict。**两边自动共享 30 分钟冷却** —— login 刚解完,
video 路径 30 分钟内不会再解,反之亦然。`test_cooldown_shared_with_login_path`
单测盯死这个约定。

### 前端零改动

`update(error_message=...)` 是已有通道(「正在上传图片 1/3」「豆包拒绝
(第 1/2 次改写重试中)」都用它)。前端不需要任何 UI 改动就能看到「正在
通过图鉴打码平台识别拖拽验证」「正在拟人拖拽通过验证」「拖拽验证已通过,
继续提交任务」等进度。

### shark_admin 留给 v0.3.1.3

`extra.decision.from == "shark_admin"` 是服务端 SSE 拒绝,**无浏览器 DOM
弹窗** —— 图鉴截不到图、拖不到,本次维持 v0.2.16 的 fail-fast(标 failed +
退额度 + 「稍后重试或换号」)不变。后续候选方向:
- 「waiting_for_captcha」状态 + 手动换号 UI
- 自动换 IP / 重新拨号
- 调用独立三方服务绕过(shark_admin 拼图)

## v0.3.1.1 - 2026-08-12

**修复 v0.3.0/v0.3.1 漏掉的图鉴打码平台凭证入口。**

### 为什么做

v0.3.0 引入了 aegis 人机验证 solver(对接图鉴 ttshitu 打码平台)与 login
keepalive 集成,但**前端 SettingsPage 没有提供凭证输入入口**。后端
`CaptchaCredentials` / `SettingsService.DEFAULTS` 早就有 `ttshitu_username /
_password / _enabled` 三字段,`SettingsService.update()` 也已接受;只是 UI
从来没暴露。

用户实际场景:豆包弹 aegis 拖拽验证 → 后端 `load_credentials()` 返回空 →
solver 抛 `AegisCaptchaDisabled` → 30min cooldown → 弹窗一直挂着 → 用户手
动关 UI → 误以为软件 bug(反馈原文:"任务浏览打开就关闭了,然后报错")。

### 改动

- `frontend/src/components/SettingsPage.vue` 新增「验证码(图鉴 ttshitu)」卡:
  启用开关 + 图鉴用户名 + 图鉴密码 + 行为说明 hint(放在「去水印」与
  「文件与日志」之间,跟 zhuceka 卡同款模式)
- `frontend/src/__tests__/ManagementPages.test.ts` 加 3 个 mock 字段 +
  `it('enables ttshitu solver from SettingsPage')` UI 行为测试
- 版本号 0.3.1 → 0.3.1.1

### 后端、solver、login keepalive 无改动

solver 也不会主动关 UI(`page.goto/close` 调用 0 处,弹窗会一直挂着等
solver 拖完),所以这次唯一缺的就是 UI 入口。

## v0.3.1 - 2026-08-11

**重大变更:v0.3.0 离线激活升级为「半在线心跳」。** 防离线激活码被截图扩散。

### 为什么做

v0.3.0 完全离线 = 一次性激活码拍照就能复制给任何人。v0.3.1 每次启动 + 每 24h
后台 daemon 必联服务器校时 + 拿新 fresh_until(7 天)。离线 + 修本地时间最多撑 7 天。

### 设计要点

- **启动握手**:`main.py` import-time 后、`create_app` 前同步调
  `bootstrap.run_startup_handshake()`。失败只 log + 用本地 grace 7d 兜底,
  **绝不阻塞 UI**。
- **后台 daemon**:守护线程,默认 24h 一次。`CTRL_C_EVENT` / `CTRL_BREAK_EVENT`
  / `atexit` 三路停。
- **Ed25519 双向验签**:client 用激活码里嵌入的 priv 签请求,server 用 KMS 私钥
  签响应;client 用 XOR 编码嵌入的 server pubkey 验响应(防 fake server)。
- **DSA1 schema**:activated.bin = `[4B magic 'DSA1'][32B priv][32B pub][8B
  fresh_until][4B clock_offset_ms][4B last_server_sync]`。升级读 v0.3.0 旧格式
  失败 → 视为未激活。
- **wire blob 升级**:`[32B priv seed][32B pub][payload][64B dev_sig]`(原 v0.3.0
  是 `<base32(payload)>.<base32(sig)>`)。
- **clock_offset_ms**:用 server_timestamp 校正本地时钟漂移,防用户改本地时间
  绕过 grace。

### 新增/修改

- 新增:`src/doupool/license/bootstrap.py`(启动握手)
- 新增:`src/doupool/license/heartbeat.py`(perform_handshake + 4-tuple verify_token)
- 新增:`src/doupool/license/heartbeat_daemon.py`(后台守护线程)
- 新增:`src/doupool/license/_embedded_server_pubkey.py`(XOR 编码的 server 公钥)
- 扩展:`verifier.pyx` 升级到 v0.3.1 schema(DSA1 持久化 priv + fresh_until)
- 扩展:`storage.py` 支持 read/write_token_v031 + 自动迁移
- 扩展:`verify_at_import.py` 加 fresh_until 检查 → expired 仍允许 grace 7d
- 修改:`src/doupool/main.py` 接入启动握手 + daemon
- 新增:`tests/test_license_e2e_handshake.py`(真 round-trip pytest,5 用例)

### 服务端(独立 `server/` 子项目,KMS 签名)

- `POST /api/heartbeat` — 验签 license_token + 验 client_sig + 签 fresh_until
- Ed25519 KMS 适配器:Aliyun KMS(生产)/ 本地 .pem(开发)
- HMAC-fingerprint 存库,prefix 撤销列表
- 测试用 TestClient in-process ASGI,无网络依赖

### 升级注意

- **所有 v0.3.0 激活码已失效**:v0.3.1 wire 格式改了,旧码验签 schema 不兼容。
  现有用户需重新签发 v0.3.1 license_token(开发者私钥相同,新增 priv seed)。
- **首次握手会改 activated.bin schema**:升级时旧文件仍在
  `DouStudio/DouStudio/license/`,新版启动握手会写新 schema。

## v0.3.0 - 2026-08-10

**重大变更:引入离线激活系统 + 独立签发工具。** 解决软件分发失控问题。

### 强制激活门

没输入有效激活码 → 用户看不到主功能界面(连 sidebar 都不可见)。后台
`/api/accounts` 等所有业务端点 403。激活窗唯一可点出口是「退出软件」。

### 设计要点

- **Ed25519 离线签名**:私钥只在开发者手里,公钥 XOR 编码后嵌入 Cython 编译
  的 `_license_verify.cp312-win_amd64.pyd`。
- **机器指纹 HMAC**:Windows 硬件(SMBIOS UUID + 主板序列号 + CPU ProcessorId)
  → `HMAC-SHA256(embedded_pubkey, raw)` → 64-hex。**签发时绑一个指纹,换主板
  / 换硬盘即失效**。指纹不存明文,防泄露。
- **激活码格式**:base32(payload_json).base32(signature_64),无 padding,
  用户粘贴时前端自动去空白/dash。
- **过期自动退出**:`now > expires_at` 直接 `sys.exit(7)`(无 UI),
  过期态用户在激活窗只能「退出」或「复制机器码找开发者续期」。
- **反调试威慑**:`IsDebuggerPresent` / `CheckRemoteDebuggerPresent` /
  `NtQueryInformationProcess(ProcessDebugPort)` ctypes 调用,与过期统一映射
  为同一响应,无信息泄露。
- **import-time 闸门**:`src/doupool/main.py` 顶部 `import doupool.license.verify_at_import`。
  即便 patch 掉 `main()` 函数体,这行 import 仍触发 `sys.exit(7)`。

### 新增端点

- `GET /api/license/status` — 不需要鉴权,返 `{status, fingerprint, customer, expires_at}`
- `POST /api/license/activate` — 不需要鉴权,body `{code}`,200 ok 或 400 错误消息
- `POST /api/license/quit` — 强杀进程,激活窗「退出」按钮用
- `/api/health` 改为返 `{status: "ok"|"degraded", version, activated: bool}`
- 所有业务端点加 `authorize_with_license` 依赖(替代原 `authorize`)

### 独立签发工具 `LicenseKeygen.exe`

- `python scripts/build_exe.py --keygen` 出独立 exe
- 复用 `doupool.license.crypto` / `doupool.license.fingerprint` 主程序代码
- 三个端点:`POST /api/generate` / `GET /api/fingerprint-self-test` / `GET /api/version`
- 私钥路径通过环境变量 `LICENSE_KEYGEN_PRIVATE_KEY` 配置,默认
  `tools/license_keygen/developer_private.key`,**不入仓**

### 新增文件

- `src/doupool/license/` — `crypto.py` / `fingerprint.py` / `storage.py` /
  `storage_path.py` / `anti_debug.py` / `verifier.pyx` / `__init__.py` /
  `verify_at_import.py` / `_embedded_pubkey.py`(git 提交)
- `tools/license_keygen/` — `main.py` / `app.py` / `index.html` / `keygen.spec` /
  `README.md` / `scripts/embed_pubkey.py` / `developer_private.key`(不入仓)
- `setup.py`(新) — `Cython.Build.cythonize` 编译 `verifier.pyx`
- `packaging/hooks/hook-doupool.license.py` — `collect_dynamic_libs` 把 .pyd 打进 COLLECT

### 修改文件

- `pyproject.toml` 加 `cryptography>=43,<46` 运行时依赖 + `Cython>=3.0,<4` 构建依赖
- `src/doupool/main.py` import-time 闸门 + `--print-fingerprint` CLI 子命令
- `src/doupool/api/app.py` 新增三个端点 + `authorize_with_license` + `/api/health` 改造
- `frontend/src/App.vue` mount 时决定渲染激活窗还是主壳
- `frontend/src/components/ActivationDialog.vue`(新)激活窗组件
- `frontend/src/api.ts` 加 `LicenseState` / `LicenseStatus` 类型 + 三个 export
- `frontend/src/__tests__/App.test.ts` 加 3 个激活闸门测试
- `packaging/doubao_manager.spec` hiddenimports 加 `doupool._license_verify` 等
- `scripts/build_exe.py` 加 `compile_cython_extensions()` + `--keygen` flag
- `src/doupool/__init__.py` + `config.py` 版本号 0.2.37.3 → 0.3.0

### 安全限制(写入 README)

1. Python + PyInstaller 本质可逆向 —— 本方案把成本从「几分钟」提到「几小时」,
   不阻挡高手。**目标:挡住脚本小子。**
2. 虚拟机指纹独立 —— 每台 VM 单独激活。
3. 硬件更换失效 —— 换主板 / 换硬盘 → 用户须联系开发者重发码(激活窗 hint 写明)。
4. `activated.bin` 就是凭证 —— 靠 Ed25519 签名 + HMAC fingerprint 绑定。
5. 反调试只是威慑 —— ScyllaHide 可绕过,已记录限制。
6. `developer_private.key` 单点失效 —— 泄露 = 任何机器都能签码,像 TLS 私钥一样对待。

## v0.2.37.3 - 2026-08-10

两个用户反馈合并到一起修:

### 修复 / 体验

1. **隐藏账号列表里的 token 显示列 + 刷新 token 按钮** 用户截图反馈 token
   列里显示「cookie 里缺少关键字段:['web_id', 'ms_token']」之类的兜底报错
   —— 这是后端 `extract_webmssdk_tokens` 失败时给前端的内部 hint,不该让普通
   用户看到。token 状态本来就是后端内部用的,前端展示出来反而制造焦虑。

   - [frontend/src/components/AccountTable.vue](frontend/src/components/AccountTable.vue)
     删掉 thead 里的 `Token` 列、tbody 里的 token badge / age / hint 三件套、
     行末「🔄 刷新 token」按钮、配套的 `tokenStatus` / `refreshing` /
     `refreshError` refs、`formatTokenAge` / `loadTokenStatus` / `refreshOne`
     函数、以及 onMounted / watch 里的 token reload 调用。
   - 兜底链路保留:cookie 在线但 cookies.json 解析失败 / 文件丢失时,用户仍然
     可以点每行的「🍪 重新导出 cookies」一键修好(v0.2.37.2 已加)。
   - 后端 `/api/accounts/{id}/webmssdk-tokens` 端点保留供内部使用,只是前端
     不再展示数据。

2. **画面描述上限 2000 → 5000 字** 用户反馈 2000 字太短,复杂场景描述经常
   被截断导致生成结果跟预期不符。

   - [frontend/src/App.vue](frontend/src/App.vue) `<DpTextarea>` 的
     `:maxlength="2000"` 提到 5000,底部计数提示 `{{ prompt.length }} / 5000`。
   - [src/doupool/api/app.py](src/doupool/api/app.py)
     `CreateVideoTaskBody.prompt` 从 `str = ""` 改成
     `Field(default="", max_length=5000)`。`prompts: list[str]` 加
     `field_validator`,列表中每个元素也按 5000 字封顶(单段 prompt)。
   - 新增 `tests/test_api.py::test_create_video_task_body_rejects_prompt_over_5000_chars`
     守住 Pydantic 边界:5000 字 ok、5001 字抛 ValidationError。

## v0.2.37.2 - 2026-08-09

v0.2.37 上线当天用户反馈:「只要 cookie 在线账号就没有问题」—— 软件读 cookie
的姿势不对,而且 v0.2.37 加的 keepalive 90s + DPAPI Playwright 兜底 + 18s
web_id 落盘探测太复杂。本次按用户决策简化为三步:

### 修复(按用户原话「1方向对,2不保留,3需要」)

1. **cookie 读取主源改成 cookies.json(首选)** v0.2.37 之前的代码读
   `Default/Cookies` SQLite,Windows + Chromium v100+ 下 SQLite 里 `value` 列
   被 DPAPI 加密成空串 → 拿不到 cookie → 抛 `TokenBundleUnavailable` →
   前端 hint「profile 损坏」误导用户。

   我们的 login 流程早就主动调 `context.cookies()` 把 doubao.com 明文 cookie
   写到 `profile_dir/cookies.json`(见 `login/browser.py:_save_doubao_cookies_to_disk`),
   这份备份就是首选数据源。

   - [video/browser.py](src/doupool/video/browser.py) 新增
     `_read_cookies_from_json` —— 从 `cookies.json` 读明文,文件不存在或
     JSON 解析失败才回退到 SQLite(读 SQLite 即使拿到空 dict 也不再算错误,
     仅当 cookies.json 也没数据时才抛 `TokenBundleUnavailable`)。
   - [video/browser.py](src/doupool/video/browser.py) `extract_webmssdk_tokens`
     改顺序:cookies.json 优先 → SQLite 兜底 → 任一有 doubao.com cookie 即可。

2. **删 v0.2.37 的 DPAPI Playwright 兜底 + keepalive 90s + 18s 探测**(用户
   明确说「不保留」):
   - [video/browser.py](src/doupool/video/browser.py) 删除
     `_BROWSER_FALLBACK_SENTINEL` 常量 + `_read_chromium_cookies_via_browser`
     函数,`_read_chromium_cookies` 简化为只读 SQLite。
   - [login/browser.py](src/doupool/login/browser.py) `keepalive_seconds`
     90 → 30。
   - [login/service.py](src/doupool/login/service.py) 默认 90 → 30。
   - [main.py](src/doupool/main.py) `LoginService(keepalive_seconds=30.0)`。
   - [api/app.py](src/doupool/api/app.py) `refresh-tokens` 删除
     18s `wait_for_function` 探测,改回固定 8s wait,顺便在 cookies 落盘后
     调 `_save_doubao_cookies_to_disk` 重写一份明文备份。

3. **新增「🍪 重新导出 cookies」行级按钮**(用户明确说「需要」):
   - [api/app.py](src/doupool/api/app.py) 新增
     `POST /api/accounts/{id}/re-export-cookies` 端点 —— 打开 Chromium 约 8
     秒,让 WebMSSDK 初始化,然后把当前 doubao.com cookie 明文写到
     `profile_dir/cookies.json`。如果浏览器里读不到 doubao.com cookie(账号
     已掉登录)返回 400 让用户重新扫码。
   - [frontend/src/api.ts](frontend/src/api.ts) 加 `reExportCookies` 包装。
   - [frontend/src/components/AccountTable.vue](frontend/src/components/AccountTable.vue)
     每行加「🍪 重新导出 cookies」按钮 + confirm 二次确认防误触(因为会弹
     浏览器窗口)。

### 顺手修

- **Local Storage 正则 bug** v0.2.17 起读 `leveldb/000003.log` 拿 web_id 时
  regex 是 `__tea_cache_tokens_497858(.+?)</script>`,但 leveldb .log 文件
  不是 HTML,根本不会有 `</script>` → 那个分支从来不会命中 → web_id 永远
  从 storage 拿不到,只能 fall back 到 cookies.samantha_web_web_id(那条也
  不一定存在)。本次改成
  `(\{[^{}]{0,800}?"web_id"[^{}]{0,400}?\})` 抓最近的 JSON object,实测能命中。
  - [video/browser.py](src/doupool/video/browser.py) `_read_chromium_local_storage`。

## v0.2.37 - 2026-08-09

### 修复

- **「打开浏览器 → 刷新 token」依然 500** v0.2.36 把 hint 透传修好了,但只要
  Chromium v100+ 的 DPAPI 加密或 WebMSSDK 没跑完就继续 500。本次把根因一并
  修掉:
  - **登录 keepalive 30s → 90s** 用户扫码成功后,需要在浏览器里访问
    `https://www.doubao.com/chat/` 让 WebMSSDK 跑完整链路(初始 web_id →
    拉 msToken → 写 leveldb)。30s 太紧,用户经常被提前关窗。90s 留足
    buffer。
    - [main.py](src/doupool/main.py) `LoginService(keepalive_seconds=90.0)`
    - [login/browser.py](src/doupool/login/browser.py) `__init__` 默认 30 → 90
    - [login/service.py](src/doupool/login/service.py) 类型注解默认同步
  - **refresh-tokens 等待时长 8s → 18s + 主动探测 WebMSSDK 落盘**
    [api/app.py](src/doupool/api/app.py) `/api/accounts/{id}/refresh-tokens`
    的 Playwright 等待从固定 `page.wait_for_timeout(8_000)` 改为
    `page.wait_for_function(...)` 探 `__tea_cache_tokens_497858` /
    `samantha_web_web_id` 写入 `localStorage`,命中即早退(典型 3-6s),
    最长 18s,失败再等 2s 兜底 → `extract_webmssdk_tokens` 用真实 hint 提示
    用户「profile 损坏」或「web_id 没落地」。
  - **Chromium v100+ DPAPI cookie 加密兼容** Chromium v100 起
    `cookies` 表的 `value` 列在 Windows 下被 DPAPI 加密,SQLite 读出来是
    空字符串,真正值在 `encrypted_value` BLOB 里 —— 我们没有 win32 DPAPI
    key 直接解密,改走「启动一个临时 Playwright persistent context,
    Chromium 进程内用当前用户 DPAPI key 解密 → 调 `context.cookies()`」
    的兜底路径。
    - [video/browser.py](src/doupool/video/browser.py) `_read_chromium_cookies`
      现在会检测 doubao.com 行是否存在 + 全部 `value` 为空 → 触发
      `_read_chromium_cookies_via_browser` Playwright 兜底。
    - 兜底函数走 `headless=True + --no-sandbox`,复用 refresh-tokens
      刚关掉的同一个 profile_dir(避免 SingletonLock 冲突)。
  - **hint 文案分三档** 用户根据提示就能定位问题:
    1. `profile 里完全没有 web_id / device_id / msToken` —— 刚登录没
       让 WebMSSDK 跑过(去浏览器访问 chat 主页停留 10-20 秒)
    2. `localStorage 有部分字段但 web_id/device_id/msToken 全部缺失` ——
       DPAPI / profile 损坏(建议删除 profile 目录后重新登录)
    3. `msToken 缺失` —— 短期过期,直接点刷新就好

### 改进

- 没有任何纯 UI 改动 —— 全在 v0.2.36 已稳定的 token 状态页面透传真实 hint。
- keepalive 计时器事件 `keepalive` / `succeeded` 继续复用,前端无需感知
  keepalive 秒数变化。

## v0.2.36 - 2026-08-09

### 修复

- **「token 状态加载失败」成无信息兜底** 用户扫码
  登录成功后,Token 状态页却显示「token 状态加载
  失败」—— 账号明明已经登录,token 应该有效。根
  因:`/api/accounts/{id}/webmssdk-tokens` 只
  catch 了 `TokenBundleUnavailable`,其他意外
  (Chromium Cookies SQLite 损坏、文件被锁、权限
  不足、`sqlite3.DatabaseError` 等)直接 500,前端
  toast 拿到一个干瘪的「token 状态加载失败」,完全
  看不到根因。
  - 后端[api/app.py](src/doupool/api/app.py)
    `get_webmssdk_tokens` 改为「所有异常都归到
    `available: false` 返 200」,`hint` 字段直接
    带上异常类名 + 原始消息(`DatabaseError: file
    is not a database` / `PermissionError: ... being
    used by another process` 等),前端照常展示
    「不可用」并提示「关闭浏览器窗口后再试」。
  - 后端[browser.py](src/doupool/video/browser.py)
    `_read_chromium_cookies` 把 `OSError` +
    `sqlite3.Error` 统一归到
    `TokenBundleUnavailable`,`_read_chromium_local
    _storage` 全路径包 `try/except Exception` —— 任
    何意外不再冒泡到 endpoint 触发 500。
  - 测试:[test_api.py](tests/test_api.py) 新增
    `test_get_webmssdk_tokens_returns_available
    _false_on_unexpected_exception`(DatabaseError)
    + `test_get_webmssdk_tokens_returns_available
    _false_on_runtime_error`;[test_video_browser.py]
    (tests/test_video_browser.py) 新增
    `test_extract_webmssdk_tokens_wraps_corrupted
    _cookies_sqlite` + `test_extract_webmssdk_tokens
    _wraps_permission_error_on_read`。

### 改进

- **json() 兜底文案带 HTTP 状态 + URL** 之前前端
  `json()` helper 在 response 非 2xx 时只回吐调用
  方传的固定文案(「刷新 token 失败」「任务创建
  失败」...),用户看不到真实根因。现在 helper 自
  动把 HTTP 状态码 + 请求 URL + 服务端响应体(优先
  抽 FastAPI 标准 `detail`,否则贴前 240 字)拼到
  Error message 里,典型输出变成
  `刷新 token 失败 (POST /api/accounts/xxx/
  refresh-tokens → 500): DatabaseError: file is
  not a database`,用户不需要翻后端日志也能定位到
  哪条 API 哪条异常。
  - 实现:[api.ts](frontend/src/api.ts) `json()`
    helper 替换。

## v0.2.35 - 2026-08-09

### 新增

- **跨账号凑余额** 多账号场景下,如果当前活跃账号
  的共享额度已用完,允许同 batch 的剩余 prompt 「跨
  过去」打其他账号 —— 过去是直接返回 `quota
  exhausted` 把整 batch 拒掉,用户得手动拆批再点。
  现在 `repository.choose_and_reserve_account` 失败
  后 `service.start` 递归选下一个账号重试;若整个
  活跃账号集都满了,把失败 prompt 收进
  `partial_rejected` 一起返回 200,前端弹 warning
  Toast 给出明细(「3 条暂无可用账号,稍后自动
  重试:#2「xxx」、#5「yyy」、#8「zzz」」)。失败
  的 prompt 仍以 `status=queued` 落库,等其他账号
  隔天重置后被 `resume_queued` 兜底接住。
  - 实现:[service.py](src/doupool/video/service.py)
    `start()` 增加循环 + `partial_rejected` 累加
    逻辑;[repository.py](src/doupool/db/repository.py)
    `choose_and_reserve_account` 复用 v0.2.33 CAS
    路径(失败 → 选下一个账号)。
  - 端点:[api/app.py](src/doupool/api/app.py)
    `create_video_task` 改返 `{task, partial_rejected}`,
    200 OK + 明细(过去是 503 `quota_exhausted`)。
  - 前端:[api.ts](frontend/src/api.ts) 同步包装 +
    [App.vue](frontend/src/App.vue) `onCreateTask` 显
    示 partial rejected Toast。
  - 测试:[test_api.py](tests/test_api.py) 新增
    `test_create_video_task_partial_rejected_across
    _accounts` —— 两个账号都满 + 3 prompt,期望
    200 + `task` 入队 + `partial_rejected` 长度 1。
- **批量下载命名规范** 用户下载批量任务产物时,所
  有文件都叫 `video.mp4`(浏览器去重 + 后端
  `attachment; filename="video.mp4"` 不带 index)
  → 多个文件覆盖彼此。改成统一格式
  `{group_index:02d}_{HHMMSS}_{prompt前12字符}[-clean].mp4`。
  - 后端:[api/app.py](src/doupool/api/app.py)
    `_video_task_dict` 加 `download_filename` 字段
    给前端备用;[repository.py](src/doupool/db/repository.py)
    `_download_response` 把 Content-Disposition
    的 filename 改成计算后的 `download_filename`(单
    条 + group download 两条链路都覆盖),group 下
    载维持 zip 内部同样命名。
  - 前端:[ResultsTable.vue](frontend/src/components/ResultsTable.vue)
    `sanitizeFilenamePart` 把控制字符 + Windows 非
    法字符 `\\ / : * ? " < > | \r \n \t` 替换成
    `_`,fallback 时用 prompt 切片组装;`download`
    属性透传。
  - 测试:[test_api.py](tests/test_api.py) 新增
    `test_video_task_dict_download_filename` +
    `test_download_response_uses_download_filename`。
- **一键清除任务** 用户场景:跑了一堆实验性任务
  后,视频任务页 + 结果页堆满几十条历史记录,手
  动一行行点删除太累。新增 2 个后端端点 + 4 个
  前端按钮(视频任务页「清已完成」「清排队」/
  结果页「清已下载」「清全部」)。
  - 端点:[api/app.py](src/doupool/api/app.py)
    `POST /api/video-tasks/clear-completed`(删
    `status ∈ {succeeded, failed, cancelled}`) +
    `POST /api/video-tasks/clear-queued`(删
    `status=queued`)。两条都走
    `refund_quota_if_recorded` 退还在途预扣
    (queued 可能从未跑过,`_pre_charged_tasks` 仍
    记录它的预扣值),DB 行物理删除。
  - 结果端点:`POST /api/results/clear-downloaded`
    (按 `clean_video_url || result_url` 筛 —
    有可下载链接的视为「已下载」)+ `POST
    /api/results/clear-all`(全 succeeded)。
  - 鉴权沿用 `authorize()`,前端按钮 `confirm()`
    dialog 二次确认后调,完成后 `showToast` 报
    `deleted_count`。
  - 前端:[api.ts](frontend/src/api.ts) 加
    `clearVideoTasks` / `clearResults` 包装;
    [App.vue](frontend/src/App.vue) 加 4 个 computed
    计数 + 2 个 handler;视频任务页 / 结果页
    `.page-actions` 右侧加按钮组
    (`.action-group`,CSS 在
    [styles.css](frontend/src/styles.css))。
  - 测试:[test_video_service.py](tests/test_video_service.py)
    新增 `test_clear_queued_refunds_pre_charge_and
    _deletes_task`(走真实预扣 → 立刻 clear → 验
    shared 桶回到 0 + DB 行物理删) +
    `test_clear_completed_refunds_pre_charge_for
    _stuck_queued_tasks`(clear-completed 不应碰
    queued 的回归断言)。

### 修复

- 6 个 v0.2.35 之前的 `vue-tsc` typecheck 噪音(与本
  版本无关,顺手清掉):
  - [VideoTaskTable.vue](frontend/src/components/VideoTaskTable.vue)
    `VideoTaskRow` 加 `download_filename?: string;`
    字段(配合 v0.2.28 _video_task_dict 的同名字段)。
  - [api.ts](frontend/src/api.ts) `json<T>()` 不接
    受泛型 → 改为 `await json(...) as T` 风格。
  - [ResultsTable.vue](frontend/src/components/ResultsTable.vue)
    `ch.isPrintable` 不是 JS 字符串方法,改成
    `ch.charCodeAt(0) < 0x20 || === 0x7f` 控制字
    符判断。
  - [App.vue](frontend/src/App.vue) partial
    rejected `map((r) => …)` 给 `r` 显式
    `{ index: number; prompt: string }` 注解。

### 测试

- 后端 `pytest tests/ -q` 458 passed(+ 2 个
  pre-existing `test_login_browser.py` Windows 控制
  台编码失败,与本版本无关,见 v0.2.34 changelog)。
- 前端 `npm run build`(内部含 `vue-tsc --noEmit`)
  0 错误。

### 预防

- `_pre_charged_tasks` + `partial_rejected` 两套
  退款机制覆盖全失败路径:
  - `service._run_inner` 顶层异常 / `CancelledError`
    / runner 抛错 / 风控拒绝 / TokenBundleUnavailable
    全部走 `refund_quota_if_recorded`。
  - clear_tasks(queued) 走相同的预扣退款逻辑,
    让「从未跑过的孤儿 queued」也安全退还,
    避免「清了任务但 shared 桶一直扣着」的脏状态。

## v0.2.34 - 2026-08-08

### 新增

- **任务间隔可调(0-60 秒)** 用户反馈:多账号并发
  时即便有 sticky + 共享额度,豆包侧仍偶发"批量
  提交太快"的风控。现在每个 task 在抢
  `_global_semaphore` 之前先 sleep 一个可调间隔,
  减慢 dispatch 节奏。SettingsPage 新增「任务
  间隔秒」字段(`<DpInput type="number" min=0
  max=60>`),默认 0 = 不间隔(向后兼容)。
  - 实现:[service.py](src/doupool/video/service.py)
    `_run_inner` 在 `assign_video_task` 之后
    读 `settings["task_interval_seconds"]`,>0 时
    跑 `asyncio.to_thread(cancellation.wait,
    timeout=interval)` —— 既能阻塞满 interval 秒,
    又能在 shutdown 触发 `cancellation.set()` 时
    立即返回(避免一个 60s 间隔卡死关停)。
    放在 assign 之后是因为账号绑定已落库,
    cancel 命中后退出不再浪费已经选的账号。
  - 范围校验:[settings/service.py](src/doupool/settings/service.py)
    `_validate` 加 `0 <= int(value) <= 60`,越界
    拒写。DEFAULTS 默认 0,旧 DB 不受影响。
- **新拒绝文案入分类** 用户报「视频生成失败,生成
  额度未扣除。」也想自动重试。
  [prompt_reviser.py](src/doupool/prompt_reviser.py)
  原本 `额度未扣除` 走 RATE_LIMIT 桶,现在从
  `_RATE_LIMIT_PATTERNS` 摘掉让它回落到
  `_GENERATION_FAILED_PATTERNS`(匹配 `视频生成失败`
  子串)→ `revise_prompt=True`,触发改写重试。

### 修复

- **任务间隔串行化(hotfix)** v0.2.34 第一版只把
  `await asyncio.sleep(interval)` 塞进 `_run_inner`,
  但每个 task 各自 sleep 是**并行**的 —— N 个 task
  一起 sleep N 秒一起醒,race 触发豆包风控,用户
  视角「间隔不生效」。改用全局 `asyncio.Lock`
  (`self._interval_lock`) 把 interval 段串行化:
  排队等锁的 task 必须等前一个 task sleep 满
  interval 才能开始自己的 sleep,真正拉开 dispatch
  节奏(3 task + 2s 间隔 → runner 调用于 +2/+4/+6s,
  而非 +2/+2/+2s)。等锁期间通过 100ms polling 检查
  `cancellation.is_set()`,避免一个 60s interval +
  锁队列把 shutdown 卡死。
  - 实现:[service.py](src/doupool/video/service.py)
    `__init__` 加 `self._interval_lock = asyncio.Lock()`;
    `_run_inner` 的 interval 段改为 polling-acquire
    + `try / finally release` 模式。
  - 测试:[test_video_service.py](tests/test_video_service.py)
    新增 `test_service_serializes_task_interval
    _across_concurrent_tasks`:3 task + interval=0.15,
    断言第 2/3 个 runner 调用相对第 1 个至少 +0.15
    / +0.30s。
- `_run_inner` 任务间隔的旧实现 `await asyncio.wait_for(
  cancellation.wait(), timeout=interval)` 永远挂死 —
  `cancellation` 是 `threading.Event`,它的 `wait()`
  是同步阻塞返回 bool,不是 awaitable;`asyncio.wait_for`
  要求 awaitable,所以等不到返回值。本次改成
  `asyncio.to_thread` 在工作线程跑阻塞 wait,既保留
  "cancel 时立即退出"语义又真正能跑完 interval。

### 测试

- `test_settings.py`:新增 3 个用例 — 默认 0、接受
  0..60、拒绝 -1 和 61。
- `test_prompt_reviser.py`:新增 `test_generation_failed
  _v0_2_34_quota_not_deducted` 断言「视频生成失败,
  生成额度未扣除」走 GENERATION_FAILED 桶 + 触发
  revise_prompt。
- `test_video_service.py`:新增 `test_service_respects
  _task_interval_seconds`,0.2s 间隔断言
  `runner_called_at - dispatched_at >= 0.15`。
- 未涉及模块:`test_task_groups.py` / `test_login_browser.py`
  失败为既有问题,与本版本无关。

### 预防

- **`build_exe.py` 自动重建 frontend/dist**(v0.2.34
  「字段不见了」根因 + 预防):之前
  `packaging/doubao_manager.spec` 只读
  `frontend/dist` 不重建,改完 .vue/.ts 没手动
  `npm run build` 就打包 → exe 里仍是旧 UI,
  新字段看不到。新版本在 PyInstaller 之前强制
  跑 `npm ci`(只在 `node_modules` 缺失时)+
  `npm run build`,失败立刻中断打包,绝不允许
  带着陈旧的 dist 出包。`_resolve_npm()` 自动
  从 `PATH` / `C:\Program Files\nodejs\npm.cmd`
  / `%NVM_SYMLINK%\npm.cmd` 等常见位置定位
  Node,Git Bash 子进程 PATH 缺失也能跑。
  - Escape hatch:`--skip-frontend-build`
    跳过重建(用于 CI 已经在前面单独跑过
    `npm run build` 的场景)。
  - 验证:`dist/index.html` 必须存在才算
    成功,vite 静默失败也会被捕获。

## v0.2.33 - 2026-08-08

### 新增

- **并发分散 + 同组粘同账号** 多组提交 6 个任务
  (3 + 3 = 6 prompt) 时,过去每个 prompt 都被同
  一个账号抢走,其他账号空转;现在首条自由选账号,
  同 `group_id` 的后续 prompt 沿用首条账号(sticky
  —— 配合 "同账号多任务并发共享 BrowserContext"
  让命中限流的概率从 ~50% 降到接近 0)。
  - 实现:[repository.py](src/doupool/db/repository.py)
    新增 `choose_and_reserve_account()` —— 把
    `choose_available_account` + `increment_account_quota`
    合在一次 `UPDATE ... WHERE used+by<=limit`(CAS)
    里原子完成"选 + 扣"。`mark_account_limited`
    封号时改为 cap `video_quota_used_shared` 桶
    并写 `video_limited_until`,后续走 `video_quota_date`
    写幂等避免重置回环。
  - [service.py](src/doupool/video/service.py) 的 `start()`
    改为:每个 prompt 先按 cost 走 CAS 预扣,
    成功后 `create_video_task` + 记录到
    `_pre_charged_tasks[task_id] = (acc_id, by, model)`
    再 `_schedule`;首条成功后 `sticky_account_id`
    固化,同组后续复用同一账号(同 `group_id`
    才粘,跨 batch 重新选)。
  - **预扣退还链路**:runner 失败 / 取消 / 风控
    / TokenBundleUnavailable 任何一条出口都走
    `refund_quota_if_recorded()` —— `recorded_cost`
    已记录的按记录退,`recorded_cost=0` 但
    `_pre_charged_tasks` 还有 entry 的兜底按
    预扣值退。`_run` 顶层 `except CancelledError` /
    `except Exception` 加 `_refund_pre_charge_if_present()`
    作为最后兜底,in-memory map 已被 pop 时回退
    到 DB 反推 (account_id, cost, model) 完成退款。
  - **重启 reconciliation**:`lifespan` 启动时
    `reconcile_orphan_pre_charges()` 扫所有
    `status in {queued, starting, generating}` 且
    `account_id != NULL` 的 task,按 `(model, duration)`
    重建 `_pre_charged_tasks` —— 应对「软件崩溃 → 重启
    → 内存失 → 桶里留下孤儿扣款」。幂等:扫到的 task
    已从上次 reconciliation 起就一直 in-flight,本次只
    再记录一次预扣,后续退款路径会按 pre_charged_entry
    的 cost 退。

### 修复

- **`quota_window()` 时区串台 → 限流秒封秒解 bug**
  旧实现 `now.replace(tzinfo=UTC).astimezone(SHANGHAI)`
  假定入参为 UTC,但 `_run_inner` 实际传
  `datetime.now(SHANGHAI)`(local),被错当 UTC 后再转
  Shanghai → local_now 日期会 +8h 漂移到「次日」。
  且 `next_reset` 返 UTC-naive,与 `utcnow()`(SHANGHAI-
  naive,v0.2.16 改名)比较错位 —— `mark_account_limited`
  写的 `video_limited_until` 在下一秒就被 `reset_daily_quotas`
  当成「已过期封号」清零,导致同账号瞬间从「满桶」变回
  「可用」又立刻被另一条 runner 抢走,引发测试 flaky。
  - 修复:`quota_window` 入参 aware datetime 走
    `.astimezone(sh_tz)`(不再 `replace(tzinfo=UTC)`),
    返回 `next_reset` 改为 SHANGHAI-naive(与 `utcnow()`
    同口径)。

### 测试

- `test_video_service.py` 新增 / 修改:
  - `test_start_with_prompts_uses_same_account_for_group`
    断言改成 `{t.account.id for t in group_tasks}`
    单值集合(同组粘同账号)。
  - `test_service_refund_noop_when_quota_was_not_charged` /
    `test_service_cancel_refund_noop_when_quota_not_charged`
    / `test_run_top_level_cancelled_error_writes_failed`
    全部更新断言 + docstring 为 v0.2.33 语义(预扣
    路径任何失败出口都必须退,终态 0)。
- 未涉及:`test_task_groups.py` 8 个失败 +
  `test_login_browser.py` 2 个失败为本会话之前
  已存在,与 v0.2.33 无关。

## v0.2.32 - 2026-08-07

### 修复

- **手动重试任务脱离结果页分组** 用户反馈:批量提交 3
  个任务并打成一组,失败那一条手动重试后,在结果页"按
  组聚合"里看不到了 —— 任务确实跑成功了,但脱离了原组。
  - 根因:[App.vue](frontend/src/App.vue) 的 `retryVideoTask`
    调 `createVideoTask` 时只拷了 `prompt / model / ratio /
    duration / mode` 五个字段,没有传 `group_id`,新任务
    group_id 为 None,[ResultsTable](frontend/src/components/ResultsTable.vue)
    按组折叠时这条孤零零的失败任务就找不到组了。后端
    `CreateVideoTaskBody` / `VideoTaskService.start` 也没
    有 group_id 入参,链路完全断掉。
  - 修复:`CreateVideoTaskBody` 加 `group_id: str | None`,
    `VideoTaskService.start` 加同名参数(显式传则沿用,
    否则按 prompt 数量自决);前端 `createVideoTask` 类型
    + `retryVideoTask` 调用都补上 `group_id: task.group_id`。
  - 测试:[test_video_service.py](tests/test_video_service.py)
    新增 2 个 case —— 显式传 group_id 继承、单条新建
    不传保持 group_id=None 的回归。

## v0.2.31 - 2026-08-06

### 修复

- **第一次改写后豆包再次报错但软件不触发下一轮改写的 bug**
  用户反馈:第一次改写 prompt 发送过去之后,豆包立即拒绝,
  但软件没识别到这个拒绝,没有进入下一轮改写重试,任务
  一直卡在「生成中」直到 timeout。
  - 根因:[protocol.py](src/doupool/video/protocol.py) 里
    `scan_sse_for_policy_rejection` 的 `_walk` 递归只盯着
    `text_block` / `content_block` / `content` / `message`
    几个固定 key。如果豆包把第二次拒绝文案塞到 `delta.text` /
    `reply_message` / 顶层 `text` / `choices[*].delta.content`
    等新字段(版本漂移),scan 直接漏,不会抛
    `DoubaoContentRejected`,retry loop 不触发,任务掉进
    polling loop 等 5-20min timeout。
  - 修复:`_walk` 改成递归走 payload 里**所有 string 值**,
    用 `_NON_TEXT_KEYS`(id/status/session_id/timestamp 等
    已知元数据)+ `_MAX_TEXT_LEN = 8000` 截断避免误命中和
    拖慢正则,兜住豆包未来把拒绝文案塞到任意字段。
  - 配套改进:[browser.py](src/doupool/video/browser.py)
    `_submit_and_poll` 的 polling loop 加 DEBUG 节流日志
    (`poll_log_every = max(5, timeout/6)`,默认 20min 超时
    下每 3min 一行),用户下次再撞「卡生成中」时用
    `LOGURU_LEVEL=DEBUG` 起程序即可看到 chain 还在跑、
    还要等多久 — 不再是完全无输出的黑盒。

### 测试

- `test_video_protocol.py` 加 4 个 v0.2.31 测试:
  - `top_level_text` 命中(原 _walk 不扫)
  - `choices[*].delta.content` 命中(原 _walk 不扫)
  - `_NON_TEXT_KEYS` 元数据字段被跳过(防误命中)
  - 超长字符串被截断到 8000(防正则拖慢 / 误塞 base64)

## v0.2.30 - 2026-08-06

### 修复

- **任务卡在「生成中」状态无法结束的 bug**
  用户反馈:浏览器已显示「视频生成成功」并弹出下载链接,
  但软件里的任务一直停留在「生成中」状态;软件重启后
  仍然卡在「生成中」,**重试无法解决**(DB 层面问题,
  不是前端缓存)。
  - 根因:`VideoTaskService._run` 顶层 `asyncio.CancelledError`
    处理器在应用退出 / 关闭时被 `asyncio.create_task` 抛出
    的 cancel 命中,把任务写回 `status=queued` + error_message
    "应用已停止,等待下次继续"。下一次启动 `resume_queued`
    又把它拉起来跑 → 再次被关闭命中 → 再次写回 queued,
    **死亡循环,任务永远卡 queued**(实测 DB 里
    `updated_at` 每次启动都更新)。
  - 修复 1:`_run` 顶层 `CancelledError` 分支改成写 `failed`
    + `completed_at` + `error_message="应用已停止,任务已取消"`,
    彻底退出死亡循环;真正想恢复的任务重启后会被
    `resume_queued` 重新调度(状态是 failed 不被扫到)。
  - 修复 2:`resume_queued` 启动时新增 `_sanitize_stale_queued_tasks`,
    把"卡 queued 超过 30 分钟"的任务批量标 failed +
    "启动时清理:任务在 queued 状态卡住超过 30 分钟,可能上次
    应用退出时被中断,已自动作废,请重新提交"。覆盖修复 1
    之前已经卡死的历史脏数据(SQL 表达式层过滤,绕开
    peewee DateTimeField 读为 ISO str 的类型问题)。
  - 取消路径额外补强:`test_service_refunds_quota_on_user_cancel`
    现在还断言 `account_id is None` + `error_message` 含
    「已取消」+ `completed_at` 已写,确保用户主动取消走的也是
    failed 路径而不是 queued。

### 行为变化

- **`service.py` 取消语义重定义**:应用关闭 / lifespan shutdown /
  asyncio task cancel 命中的任务一律写 `failed`("已取消"),
  不会再写 `queued`。前端 UI 会显示「失败」状态,
  配 quota 已退 + 文案明确,用户可重提交。如对历史脏数据有疑问,
  启动日志会打印 `video_stale_queued_sanitized count=N`。

## v0.2.29 - 2026-08-06

### 修复

- **额度模型从按模型分桶改为账号共享池**
  豆包官方对单账号每日总配额是共享池(模型不分桶),
  v0.2.9 起的 `mini/v2/std` 三桶扣退与实际不符 —— 用户看到
  「mini 用完 std 还有」的虚假余量。
  - 数据迁移:启动 lifespan 钩子里一次性把
    `shared = sum(mini + v2 + std)` 写入新列 `video_quota_used_shared`,
    幂等(共享池 = 0 且老桶合计 > 0 才迁)。老三桶保留只读,
    供历史数据查询。
  - 扣退/选择/限流清零全部走共享池。共享池扣退内部用 SQL `GREATEST(field - by, 0)`,
    不会出现负值。
  - `_video_task_dict` 之前硬编码 `quotas["mini"]` 取 quota_total,
    std/v2 任务显示的「总额度」错。修复后统一用 `quotas["shared"]`。
  - 进度条 UI 改成单行 `已用/总额`,AccountTable 不再展示 mini/fast
    双堆叠行(老前端不会立刻崩,后端仍 mirror 老字段 = shared 值)。

- **SettingsPage 移除 round_robin 死链 + daily_quota 字段统一**
  - 「调度策略」下拉只剩 `least_used`(老 `round_robin` 自 v0.2.9 后
    没有实现,显示但提交后被拒)。
  - 「每日额度」单输入框绑 `daily_quota_shared`,旧 `daily_quota`
    三字段(`mini/v2/std`)从 UI 隐去。

### 放宽

- **max_concurrency 上限 10 → 50**
  用户实测多机并发需求,默认 1 保留。SettingsService 校验改为
  `1 <= x <= 50`,SettingsPage 输入框 `:max="50"`。

- **视频时长白名单 {5,10} → 任意整数 4..10 秒**
  豆包支持任意整数 4..10 秒(原 `{5,10}` 太严),
  任务表单改 `<input type="number" :min="4" :max="10">`,
  protocol.py 校验改 `set(range(4, 11))`。

### 新增

- **单账号 / 一键全部重置额度**
  软件有跨日重置 + 限流到期自动清,但用户需要一个手动按钮兜底,
  避免软件卡住时无解。
  - 后端:`POST /api/accounts/{id}/reset-quota` 清单账号 shared + 清
    `video_limited_until`;`POST /api/accounts/reset-all-quota`
    遍历所有 enabled 账号统一清,disabled 账号不动。
  - 前端:AccountTable 每行加「🔄 重置」按钮,顶部加「一键重置全部」,
    两次确认 dialog。UI 重置成功后 emit refresh 触发父级刷新。

- **独立重置 cron(双保险)**
  原 `reset_daily_quotas` 只在 worker 主循环跑,worker 不在时不会跨日清。
  现在 `VideoService.start()` 启动一个独立 asyncio task,每 60s tick 一次,
  到 `quota_reset_time` 触发清桶(主循环已有调用保留作双保险)。

## v0.2.28 - 2026-08-06

### 修复

- **视频任务页下载按钮 ERR_INVALID_RESPONSE**
  用户反馈:在「视频任务」页(succeeded 状态)点下载报 `ERR_INVALID_RESPONSE`,
  但同样的任务在「生成结果」页下载正常。
  - 根因:`VideoTaskTable.vue` 用裸 `<a download href=cdnUrl>` 触发下载。
    WebView2 在跨域签名 CDN URL 上对 `<a download>` 静默降级或直接拒绝;
    「生成结果」页早已改用 `DownloadButton.vue` 的三层 fallback(
    `fetch cors → fetch no-cors → window.open`)+ 失败时由 App.vue 兜底
    调用 `/api/results/:id/refresh-url` 重解析签名 URL,所以一直正常。
  - 修复:把「视频任务」页 2 处 `<DpLink :download>` 替换为
    `<DpDownloadButton>`(同 ResultsTable),并把 `@download-failed` 透传到
    App.vue 复用 `onResultDownloadFailed` + `refreshedResultIds` 防刷机制。
  - 收益:两个页面下载路径完全一致;签名 URL 过期时不再静默失败,
    toast「链接已过期,正在重新获取」+ 自动重解析。

### 新增

- **生成结果页按组折叠批量任务**
  用户反馈:一次性提交多段 prompt(单 prompt 用「第一段」「第二段」分隔,
  或传 `prompts: list[str]`)会被后端自动打成同一 `group_id`,但结果页
  完全扁平展示,点下载后视频混在一起。
  - 新增:ResultsTable 按 `group_id` 聚合,有组的折叠展示组头
    「组 #XXXXXXXX · N 个视频」,无组的(老任务)保持扁平。
  - 新增:组级双按钮:
    - **「下载全部」**(前端方案):循环调用三层 fallback + 350ms 间隔,
      filename 拼 `${group_id[:8]}_HHMMSS/doubao-${id}.mp4` 让浏览器下载
      管理器自动建子文件夹(WebView2/Edge 桌面版 Chromium 实测支持)。
    - **「保存到下载目录」**(后端方案):新加 `POST /api/results/group-download`
      端点,后端用 httpx.AsyncClient.stream 把整组视频流式写到
      `settings.download_dir/<group_id[:8]>_HHMMSS/`,完成后 alert 提示
      落盘路径。错误码:404 group_id 不存在 / 409 签名过期 / 500 磁盘满
      或权限不足 / 502 网络错误。
  - 优先用 `clean_video_url`(无水印),fallback 到 `result_url`(原画),
    与单条下载语义一致。

## v0.2.27 - 2026-08-06

### 修复

- **超时任务退还额度**
  用户反馈:同账号一次性提交 5 个任务,3 个 succeeded、2 个 timeout。豆包没
  出结果,本地代码兜底超时抛 `RuntimeError("视频生成超时")` → 走
  `classify_failure` 落到 `GENERATION_FAILED`/`UNKNOWN`,这两个分类不在
  退款白名单 → 用户被误扣 quota。
  - 修复:`src/doupool/prompt_reviser.py`
    - 新增 `FailureKind.TIMEOUT = "timeout"` 枚举值。
    - 新增 `_TIMEOUT_PATTERNS`(放在 `_GENERATION_FAILED_PATTERNS` **之前**,
      因为「视频生成超时」包含「视频生成失败」子串 —— 顺序错就漏退款)。
      模式精确化:`r"视频生成超时"`、`r"生成超时"`、`r"请求超时"` 等。
    - `classify_failure()` 加 TIMEOUT 分支:`retryable=True, revise_prompt=False`。
      超时 ≠ prompt 内容问题,改写 prompt 没意义,正确动作是退款。
  - 修复:`src/doupool/video/service.py` 退款白名单加入 `FailureKind.TIMEOUT`。
  - 行为变更:**用户主动取消也退还额度**(原来 v0.2.19-v0.2.26 取消 = 不退)。
    现在统一「失败 = 退」语义 —— 用户没拿到视频 = 失败,不该扣 quota。任务
    仍回 queued 等下次继续(取消 ≠ 永远放弃)。

### 新增

- **设置 → 调度 → 任务超时(分钟)**
  - 全局默认值,范围 1-20 分钟,默认 7(沿用旧 hardcode 420s)。
  - 提交任务表单不再单独暴露超时,避免 UI 复杂化(全局足够)。
  - 实现:`src/doupool/settings/service.py` DEFAULTS 加 `default_timeout_minutes: 7`,
    `_validate()` 校验 1-20;`main.py` 启动时读 settings 把分钟 × 60 写入
    `PlaywrightVideoRunner.timeout`。
  - **只对下一个 task 生效**:用户在生成途中改设置不影响正在跑的任务(避免
    deadline 突变成更短的值让跑一半的任务立刻超时);runner.timeout 是模块
    属性,下一次 `run()` 调用读 `self.timeout` 时自然拿到新值。
  - UI:「调度」卡片末尾加 `DpField` `任务超时(分钟)`,hint 写明「超时未成功
    将自动退还额度」让用户感知退款行为。

## v0.2.26 - 2026-08-05

### 修复

- **同账号多任务并发时 anchor page 被关闭 → context 崩溃**
  用户反馈:同一个账号(50 quota 桶)同时跑多个任务,只要有一个任务**成功**,
  浏览器界面立刻关闭,其他正在轮询链路的并发任务立刻抛
  `Target page, context or browser has been closed`。
  - 根因:`PlaywrightVideoRunner` 是 per-profile_dir 共享 BrowserContext,
    由 `_get_shared_context` 创建一个 anchor page 留在 `context.pages[0]`,
    防止 context 进入「0 page」状态被 Playwright 自动 close。但 `run()` 和
    `recheck_result()` 之前会**优先复用** `context.pages[0]`,然后 finally
    关掉 → task 拿到的是 anchor → 关掉 anchor → context 0 page → 自动 close
    context → 同账号并发任务在 `wait_for_timeout` 轮询时全部抛错。
  - 修复:`src/doupool/video/browser.py`
    - 新增模块级 helper `_is_context_alive(context)`,try/except 包装
      `context.is_closed()`,统一兜底让 UI 拿到可读提示而非底层异常。
    - `run()` 第 822-840 行改为**始终** `await context.new_page()`,
      不再遍历 `context.pages` 选未关闭的;context 已 close 时清缓存 + 抛
      `RuntimeError("视频浏览器上下文已关闭,请重试")` 或
      `RuntimeError("视频浏览器窗口已关闭,请重新打开后重试:...")`。
    - `recheck_result()` 第 753 行同步改为 `new_page()` 路径。
  - anchor page 仍由 `_get_shared_context` 持有,生命周期跟 context 走,
    `runner.close()` 时统一关闭。task 各自 new 出来的 page 在 finally 正常
    关闭,不再污染 anchor。
  - 调度层 `service.py` 不动:`_global_semaphore` 无 per-account 锁是 v0.2.19
    显式决定(50 quota 账号要能并发),bug 只在 `run()` 误用 anchor。
  - 测试:`tests/test_video_browser.py` 新增 5 个用例覆盖
    - `run()` 自己 new_page 且不动 anchor
    - 两个并发 run() 互不影响
    - context 已 close 时清晰报错
    - `recheck_result()` 同样不碰 anchor
    - 5 轮 run() 后 anchor 仍 `pages[0]` 且未关闭

## v0.2.25 - 2026-08-05

### 变更

- **`revise_prompt` 改写策略:从启发式剥关键词换成单一固定后缀**
  用户新需求:「如果是因为提示词的问题返回错误,应该在原来的提示词后面加上
  这句话发给豆包后,让他重新生成 —— 把这段提示词修改成不违反平台规则的
  提示词,并生成视频。如果还不行就继续加上这句话让他修改」。
  - `src/doupool/prompt_reviser.py:revise_prompt` 重写。`POLICY_VIOLATION`
    和 `GENERATION_FAILED` 不再做关键词剥离 (`_RISKY_KEYWORDS` /
    `_strip_risky_keywords`) 或安全模板兜底,统一返回
    `f"{原 prompt} 把这段提示词修改成不违反平台规则的提示词,并生成视频"`。
  - 浏览器层 `browser.run()` retry 循环已把每次返回的 `new_prompt` 写回
    `prompt_to_send`,所以后缀天然累积:
    - attempt 1: `原 prompt + 指令`
    - attempt 2: `原 prompt + 指令 + 指令`
    - attempt 3: `原 prompt + 指令 + 指令 + 指令`
    不回退到原 prompt,严格按用户「继续加上这句话让他修改」执行。
  - 仍区分 `revise_prompt=True/False`:quota / network / invalid_input
    类失败不触发改写,直接退款重派。
  - 删除 `_RISKY_KEYWORDS` / `_strip_risky_keywords` / `_soften_description`
    三个私有函数,改写策略从「软件启发式 + 兜底模板」简化为「让豆包自己改」。

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
