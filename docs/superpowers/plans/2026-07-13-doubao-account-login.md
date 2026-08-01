# 豆包账号登录模块 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 构建一个 Windows/macOS 本地桌面应用，使用 Vue + PyWebView 管理豆包账号，并通过 Playwright 独立浏览器完成扫码登录和登录成功检测。

**Architecture:** Vue 生产构建由回环地址 FastAPI 服务提供，PyWebView 只承载桌面窗口。登录编排器在后台线程中运行 Playwright，每个账号使用独立持久化目录；Peewee 保存账号和登录元数据，SSE 向界面推送状态。

**Tech Stack:** Python 3.12、UV、FastAPI、Uvicorn、PyWebView、Playwright、Peewee、SQLite、Vue 3、TypeScript、Vite、Vitest、Pytest

## Global Constraints

- 首个里程碑只实现账号管理与扫码登录，不实现视频任务调度或免费额度统计。
- 登录必须由用户自行扫码、处理验证码和设备确认，不保存密码，不绕过风控。
- 浏览器会话按账号隔离并仅保存在当前设备。
- Cookie、Set-Cookie、Authorization、访问令牌、手机号、二维码内容和完整响应正文不得进入日志或 SQLite。
- 后端仅监听 `127.0.0.1`，所有 `/api` 请求必须校验启动时生成的随机本地访问令牌。
- `run.sh` 必须先构建 Vue，再通过 `uv run` 启动桌面程序。
- 登录成功必须通过豆包当前用户信息接口返回有效用户标识确认，不能只依赖页面跳转或单个 Cookie。

---

## File Map

```text
frontend/
├── package.json                  # 前端依赖与脚本
├── package-lock.json             # 锁定 npm 依赖
├── vite.config.ts                # 构建与测试配置
├── index.html                    # Vue 入口
└── src/
    ├── api.ts                    # 本地 API、SSE 客户端
    ├── types.ts                  # 前端数据类型
    ├── App.vue                   # 应用壳与路由状态
    ├── main.ts                   # Vue 启动
    ├── styles.css                # Linear 暗色设计变量与全局样式
    ├── components/
    │   ├── AccountTable.vue      # 账号表格
    │   ├── AddAccountModal.vue   # 登录进度弹层
    │   ├── Sidebar.vue           # 左侧导航
    │   └── StatCards.vue         # 账号摘要
    └── __tests__/
        └── App.test.ts           # 关键界面状态测试
src/doupool/
├── __init__.py
├── main.py                       # 桌面程序入口
├── config.py                     # 路径、端口和访问令牌
├── desktop.py                    # Uvicorn 生命周期与 PyWebView
├── api/
│   ├── app.py                    # FastAPI 创建与静态文件
│   ├── dependencies.py           # 令牌校验
│   └── schemas.py                # Pydantic 响应模型
├── db/
│   ├── database.py               # SQLite 初始化和迁移
│   ├── models.py                 # Peewee 模型
│   └── repository.py             # 账号与登录事务
├── login/
│   ├── state.py                  # 登录状态机
│   ├── detector.py               # 豆包响应与用户信息验证
│   ├── browser.py                # Playwright 持久化 Chromium
│   └── service.py                # 登录尝试编排与事件流
└── logging/
    ├── redaction.py              # 敏感信息脱敏
    └── setup.py                  # 轮转文件、终端、数据库事件日志
tests/
├── conftest.py
├── test_database.py
├── test_redaction.py
├── test_login_state.py
├── test_login_detector.py
├── test_login_service.py
└── test_api.py
pyproject.toml                    # Python 元数据与依赖
run.sh                            # 构建前端并用 UV 启动
```

### Task 1: Python foundation and SQLite persistence

**Files:**
- Create: `pyproject.toml`
- Create: `src/doupool/__init__.py`
- Create: `src/doupool/config.py`
- Create: `src/doupool/db/database.py`
- Create: `src/doupool/db/models.py`
- Create: `src/doupool/db/repository.py`
- Create: `tests/conftest.py`
- Create: `tests/test_database.py`

**Interfaces:**
- Produces: `Settings.from_environment() -> Settings`
- Produces: `DatabaseManager.initialize() -> None`
- Produces: `AccountRepository.create_login_attempt(account_id: str | None = None) -> LoginAttempt`
- Produces: `AccountRepository.complete_login(attempt_id: str, identity: Mapping[str, str | None], profile_dir: str) -> Account`

- [ ] **Step 1: Add the project metadata and failing persistence test**

```toml
# pyproject.toml
[project]
name = "doupool"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = [
  "fastapi>=0.116,<1",
  "uvicorn>=0.35,<1",
  "pywebview>=5.4,<7",
  "playwright>=1.54,<2",
  "peewee>=3.18,<4",
  "platformdirs>=4.3,<5",
]

[dependency-groups]
dev = ["pytest>=8.4,<9", "pytest-asyncio>=1.1,<2", "httpx>=0.28,<1"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/doupool"]

[tool.pytest.ini_options]
pythonpath = ["src"]
testpaths = ["tests"]
asyncio_mode = "auto"
```

```python
# tests/test_database.py
from doupool.db.models import Account, LoginAttempt

def test_complete_login_creates_account(repository, temp_profile):
    attempt = repository.create_login_attempt()
    account = repository.complete_login(
        attempt.id,
        {"user_id": "u-1", "nickname": "莲韵"},
        temp_profile,
    )
    assert account.doubao_user_id == "u-1"
    assert account.status == "active"
    assert LoginAttempt.get_by_id(attempt.id).state == "succeeded"
    assert Account.select().count() == 1
```

- [ ] **Step 2: Run the test and verify the missing-module failure**

Run: `uv sync && uv run pytest tests/test_database.py -v`

Expected: FAIL with `ModuleNotFoundError: No module named 'doupool.db'`.

- [ ] **Step 3: Implement settings, Peewee models, migration version 1, and repository transaction**

```python
# src/doupool/db/models.py
class Account(BaseModel):
    id = CharField(primary_key=True)
    display_name = CharField()
    doubao_user_id = CharField(unique=True, null=True)
    doubao_nickname = CharField(null=True)
    profile_dir = CharField()
    status = CharField(default="active")
    enabled = BooleanField(default=True)
    last_verified_at = DateTimeField(null=True)
    last_error = TextField(null=True)
    created_at = DateTimeField(default=utcnow)
    updated_at = DateTimeField(default=utcnow)

class LoginAttempt(BaseModel):
    id = CharField(primary_key=True)
    account = ForeignKeyField(Account, null=True, backref="login_attempts")
    state = CharField(default="created")
    error_code = CharField(null=True)
    error_message = TextField(null=True)
    started_at = DateTimeField(default=utcnow)
    finished_at = DateTimeField(null=True)
```

Implement `DatabaseManager.initialize()` to enable `foreign_keys`, `journal_mode=wal`, `busy_timeout=5000`, create `schema_version`, and apply migration 1. Implement `complete_login()` inside `database.atomic()` and update an existing account when `doubao_user_id` already exists.

- [ ] **Step 4: Run database tests**

Run: `uv run pytest tests/test_database.py -v`

Expected: all tests PASS, including duplicate-user update and WAL pragma assertions.

- [ ] **Step 5: Commit the persistence layer**

```bash
git add pyproject.toml uv.lock src/doupool tests/conftest.py tests/test_database.py
git commit -m "feat: add account persistence layer"
```

### Task 2: Structured logging and redaction

**Files:**
- Create: `src/doupool/logging/redaction.py`
- Create: `src/doupool/logging/setup.py`
- Modify: `src/doupool/db/models.py`
- Create: `tests/test_redaction.py`

**Interfaces:**
- Consumes: initialized Peewee database from Task 1
- Produces: `redact(value: object) -> object`
- Produces: `configure_logging(log_dir: Path, database_enabled: bool = True) -> logging.Logger`

- [ ] **Step 1: Write failing redaction tests**

```python
from doupool.logging.redaction import redact

def test_redacts_nested_sensitive_values():
    value = {"Authorization": "Bearer secret", "phone": "13800138000", "ok": 200}
    assert redact(value) == {
        "Authorization": "[REDACTED]",
        "phone": "138****8000",
        "ok": 200,
    }

def test_redacts_cookie_in_message():
    assert "abc123" not in redact("Cookie: session=abc123")
```

- [ ] **Step 2: Verify the tests fail**

Run: `uv run pytest tests/test_redaction.py -v`

Expected: FAIL because `doupool.logging.redaction` does not exist.

- [ ] **Step 3: Implement recursive redaction and logging handlers**

Use a case-insensitive denylist of `authorization`, `cookie`, `set-cookie`, `token`, `qr`, `password`, and `secret`. Mask mainland mobile numbers with `re.sub(r"(?<!\d)(1\d{2})\d{4}(\d{4})(?!\d)", r"\1****\2", text)`. Configure stderr and `RotatingFileHandler(maxBytes=5_000_000, backupCount=5, encoding="utf-8")`; add an `AppLog` Peewee handler that stores only the redacted rendered message and structured IDs.

- [ ] **Step 4: Run redaction and database-log tests**

Run: `uv run pytest tests/test_redaction.py -v`

Expected: all tests PASS and no test fixture secret appears in captured output or `AppLog.message`.

- [ ] **Step 5: Commit logging**

```bash
git add src/doupool/logging src/doupool/db/models.py tests/test_redaction.py
git commit -m "feat: add redacted structured logging"
```

### Task 3: Login state machine and event stream

**Files:**
- Create: `src/doupool/login/state.py`
- Create: `src/doupool/login/service.py`
- Create: `tests/test_login_state.py`
- Create: `tests/test_login_service.py`

**Interfaces:**
- Consumes: `AccountRepository` from Task 1
- Produces: `LoginStateMachine.transition(next_state: LoginState) -> None`
- Produces: `LoginService.start(account_id: str | None = None) -> LoginAttempt`
- Produces: `LoginService.events(attempt_id: str) -> AsyncIterator[LoginEvent]`
- Expects: browser runner protocol `run(attempt_id, profile_dir, emit, cancel_event) -> VerifiedLogin`

- [ ] **Step 1: Write failing state and concurrency tests**

```python
def test_rejects_invalid_transition():
    machine = LoginStateMachine(LoginState.CREATED)
    with pytest.raises(InvalidLoginTransition):
        machine.transition(LoginState.SUCCEEDED)

@pytest.mark.asyncio
async def test_only_one_interactive_login(login_service):
    first = login_service.start()
    with pytest.raises(LoginAlreadyRunning):
        login_service.start()
    await login_service.cancel(first.id)
```

- [ ] **Step 2: Verify failures**

Run: `uv run pytest tests/test_login_state.py tests/test_login_service.py -v`

Expected: FAIL because login state and service modules do not exist.

- [ ] **Step 3: Implement explicit transitions, cancellation, timeout, and SSE-compatible queues**

Allow only:

```python
ALLOWED = {
    CREATED: {LAUNCHING, CANCELLED},
    LAUNCHING: {WAITING_FOR_SCAN, FAILED, CANCELLED},
    WAITING_FOR_SCAN: {VERIFYING, FAILED, CANCELLED, TIMED_OUT},
    VERIFYING: {SUCCEEDED, WAITING_FOR_SCAN, FAILED, CANCELLED, TIMED_OUT},
}
```

Run the blocking browser runner through `asyncio.to_thread`, use an `asyncio.Lock` to enforce one active login, emit typed events to per-attempt queues, set five-minute total timeout, and persist every terminal transition.

- [ ] **Step 4: Run state/service tests**

Run: `uv run pytest tests/test_login_state.py tests/test_login_service.py -v`

Expected: all tests PASS, including success, runner failure, timeout, cancellation, and stale-attempt recovery.

- [ ] **Step 5: Commit the orchestration layer**

```bash
git add src/doupool/login/state.py src/doupool/login/service.py tests/test_login_state.py tests/test_login_service.py
git commit -m "feat: add login orchestration state machine"
```

### Task 4: Playwright browser and Doubao login detector

**Files:**
- Create: `src/doupool/login/detector.py`
- Create: `src/doupool/login/browser.py`
- Create: `tests/fixtures/doubao_account_info.json`
- Create: `tests/test_login_detector.py`

**Interfaces:**
- Consumes: browser runner protocol from Task 3
- Produces: `DoubaoIdentity(user_id: str, nickname: str | None)`
- Produces: `DoubaoLoginDetector.observe(response_meta: ResponseMeta) -> bool`
- Produces: `DoubaoLoginDetector.verify(page: Page) -> DoubaoIdentity | None`
- Produces: `PlaywrightLoginRunner.run(...) -> VerifiedLogin`

- [ ] **Step 1: Write detector tests using a minimal sanitized fixture**

```json
{"data":{"user":{"user_id":"u-1","name":"莲韵"}},"code":0}
```

```python
def test_login_response_triggers_verification(detector):
    assert detector.observe(ResponseMeta(
        url="https://www.doubao.com/passport/web/login/confirm/",
        status=200,
        method="POST",
    )) is True

def test_identity_requires_nonempty_user_id(detector, account_info_page):
    identity = detector.verify(account_info_page)
    assert identity.user_id == "u-1"
    assert identity.nickname == "莲韵"
```

- [ ] **Step 2: Run detector tests and verify failure**

Run: `uv run pytest tests/test_login_detector.py -v`

Expected: FAIL because the detector module is absent.

- [ ] **Step 3: Implement response hints and authoritative account verification**

Treat successful responses whose normalized path contains `/passport/web/login/`, `/passport/web/scan/`, or `/passport/web/account/` as verification triggers. Authoritatively request `https://www.doubao.com/passport/web/account/info/` from the authenticated browser context, require HTTP 200, JSON code `0`, and a non-empty user ID from the supported mappings `data.user.user_id`, `data.user_id`, or `data.id`. Do not log response bodies.

Launch with `playwright.chromium.launch_persistent_context(user_data_dir=..., headless=False)`, register `context.on("response", ...)`, open `https://www.doubao.com/`, and verify on response hints plus a two-second fallback interval. Emit `waiting_for_scan` only after the page is loaded. On success return identity and close context; on cancellation close context immediately.

- [ ] **Step 4: Run detector and runner unit tests**

Run: `uv run pytest tests/test_login_detector.py tests/test_login_service.py -v`

Expected: all tests PASS; mocked response bodies, headers, and cookies never appear in captured logs.

- [ ] **Step 5: Commit the Doubao adapter**

```bash
git add src/doupool/login/detector.py src/doupool/login/browser.py tests/fixtures tests/test_login_detector.py
git commit -m "feat: detect doubao browser login"
```

### Task 5: Authenticated FastAPI and SSE API

**Files:**
- Create: `src/doupool/api/schemas.py`
- Create: `src/doupool/api/dependencies.py`
- Create: `src/doupool/api/app.py`
- Create: `tests/test_api.py`

**Interfaces:**
- Consumes: repository and `LoginService`
- Produces: `create_app(settings, repository, login_service) -> FastAPI`
- Produces: JSON APIs and `text/event-stream` login events

- [ ] **Step 1: Write failing token and login API tests**

```python
def test_api_requires_local_token(client):
    assert client.get("/api/accounts").status_code == 401

def test_create_login_attempt(client, token):
    response = client.post(
        "/api/accounts/login-attempts",
        headers={"X-DouPool-Token": token},
    )
    assert response.status_code == 202
    assert response.json()["state"] == "created"
```

- [ ] **Step 2: Verify API tests fail**

Run: `uv run pytest tests/test_api.py -v`

Expected: FAIL because `create_app` does not exist.

- [ ] **Step 3: Implement schemas, token dependency, routes, SSE, and SPA fallback**

Use `secrets.compare_digest` for `X-DouPool-Token`. Encode SSE messages as:

```text
event: login_state
data: {"attempt_id":"...","state":"waiting_for_scan","message":"等待扫码"}

```

Mount Vite assets under `/assets`, serve `frontend/dist/index.html` for non-API paths, and return a clear 503 page when the frontend has not been built. Implement list, login, relogin, check, patch-enabled, delete, logs, health, and login-attempt event routes from the spec.

- [ ] **Step 4: Run API tests**

Run: `uv run pytest tests/test_api.py -v`

Expected: all tests PASS, including bad token, SSE terminal event, missing frontend, duplicate login, and account deletion confirmation.

- [ ] **Step 5: Commit the local API**

```bash
git add src/doupool/api tests/test_api.py
git commit -m "feat: expose local account login api"
```

### Task 6: Vue Linear-style account interface

**Files:**
- Create: all files under `frontend/` listed in File Map

**Interfaces:**
- Consumes: FastAPI routes and SSE event shape from Task 5
- Produces: production files in `frontend/dist`

- [ ] **Step 1: Add Vue/Vite dependencies and failing UI tests**

```json
{
  "scripts": {"dev":"vite","build":"vue-tsc -b && vite build","test":"vitest run"},
  "dependencies": {"vue":"^3.5.17"},
  "devDependencies": {"@testing-library/vue":"^8.1.0","@vitejs/plugin-vue":"^6.0.0","jsdom":"^26.1.0","typescript":"^5.8.3","vite":"^7.0.4","vitest":"^3.2.4","vue-tsc":"^3.0.1"}
}
```

```ts
it('starts account login from the add button', async () => {
  render(App)
  await fireEvent.click(screen.getByRole('button', { name: '添加账号' }))
  expect(await screen.findByText('等待扫码')).toBeTruthy()
})
```

- [ ] **Step 2: Install dependencies and verify the UI test fails**

Run: `cd frontend && npm install && npm test`

Expected: FAIL because `App.vue` and account components are absent.

- [ ] **Step 3: Implement the approved UI and API client**

Use CSS variables `--bg:#08090b`, `--panel:#111216`, `--border:#24252b`, `--muted:#777983`, `--accent:#6d5df7`, `--success:#22c55e`, and 6–10 px radii. Reproduce the approved sidebar, status cards, account table, and add-account modal. Read the token from `<meta name="doupool-token">`; create the login attempt, subscribe to SSE, update progress text, close the modal on success, and refresh accounts.

Disable unsupported video-task controls and label them “即将支持”. Require a confirmation dialog before deleting account data.

- [ ] **Step 4: Run frontend tests and production build**

Run: `cd frontend && npm test && npm run build`

Expected: Vitest PASS and `frontend/dist/index.html` exists with hashed JS/CSS assets.

- [ ] **Step 5: Commit the frontend**

```bash
git add frontend
git commit -m "feat: add linear style account manager ui"
```

### Task 7: PyWebView desktop lifecycle and UV launcher

**Files:**
- Create: `src/doupool/desktop.py`
- Create: `src/doupool/main.py`
- Create: `run.sh`
- Modify: `.gitignore`
- Create: `tests/test_desktop.py`

**Interfaces:**
- Consumes: `create_app()` from Task 5 and `frontend/dist` from Task 6
- Produces: `python -m doupool.main`
- Produces: executable `run.sh`

- [ ] **Step 1: Write failing lifecycle tests**

```python
def test_server_binds_loopback_and_injects_token(desktop_runtime):
    runtime = desktop_runtime.start_server()
    assert runtime.host == "127.0.0.1"
    html = httpx.get(runtime.url).text
    assert '<meta name="doupool-token"' in html
    runtime.stop()
```

- [ ] **Step 2: Verify the lifecycle test fails**

Run: `uv run pytest tests/test_desktop.py -v`

Expected: FAIL because the desktop runtime is absent.

- [ ] **Step 3: Implement runtime cleanup and launcher**

Reserve an ephemeral loopback port, generate `secrets.token_urlsafe(32)`, start Uvicorn in a non-daemon thread, wait up to ten seconds for `/api/health`, then call:

```python
webview.create_window("DouPool", runtime.url, width=1280, height=820, min_size=(960, 640))
webview.start(runtime.on_webview_ready, debug=settings.debug)
```

On exit, cancel active login attempts, close Playwright contexts, set `server.should_exit = True`, join the server thread, and close SQLite.

Create executable `run.sh` with `set -euo pipefail`; check `uv`, `node`, and `npm`; run `npm ci` when `package-lock.json` exists; run `npm run build`; run `uv sync`; run a Python import check for Chromium and call `uv run playwright install chromium` only when unavailable; finally `exec uv run python -m doupool.main`.

- [ ] **Step 4: Run Python tests and shell syntax check**

Run: `uv run pytest tests/test_desktop.py -v && bash -n run.sh`

Expected: all tests PASS and shell syntax exits 0.

- [ ] **Step 5: Commit desktop startup**

```bash
git add src/doupool/desktop.py src/doupool/main.py tests/test_desktop.py run.sh .gitignore
git commit -m "feat: launch doupool desktop with uv"
```

### Task 8: Full verification and manual login handoff

**Files:**
- Create: `README.md`
- Modify: only files required by failures discovered in this task

**Interfaces:**
- Consumes: complete application from Tasks 1–7
- Produces: verified developer and user startup instructions

- [ ] **Step 1: Add README with exact setup and privacy behavior**

Document `./run.sh`, the first-run Chromium download, application-data locations on macOS and Windows, how to add/relogin/delete an account, and the rule that browser profiles are local and logs are redacted. Include `uv run pytest` and `cd frontend && npm test && npm run build` as contributor checks.

- [ ] **Step 2: Run the complete automated verification**

Run:

```bash
uv run pytest -v
cd frontend && npm test && npm run build
cd .. && bash -n run.sh
```

Expected: all Pytest and Vitest tests PASS, Vite production build succeeds, and shell syntax exits 0.

- [ ] **Step 3: Start the application for a manual smoke test**

Run: `./run.sh`

Expected: PyWebView shows the approved dark account screen; “添加账号” opens a headed Chromium at `doubao.com`; cancelling the browser updates the modal without crashing the desktop app.

- [ ] **Step 4: Perform one user-authorized scan login and validate persistence**

Ask the user to scan the displayed QR code. Do not interact with CAPTCHA or device verification. Verify that the modal reaches “登录成功” within ten seconds, an account row appears, the desktop app can restart, and “检查会话” returns active. Inspect logs with targeted searches to confirm no Cookie, Authorization, token, phone number, QR value, or response body was stored.

- [ ] **Step 5: Commit documentation and verified fixes**

```bash
git add README.md
git add -u
git commit -m "docs: add doupool setup and verification guide"
```
