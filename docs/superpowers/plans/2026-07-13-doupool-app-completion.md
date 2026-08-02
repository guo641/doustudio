# DouPool App Completion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete navigation, settings, quota-aware account scheduling, account actions, results, and logs while simplifying every page to a compact table-first layout.

**Architecture:** Persist application settings and per-account quota state in SQLite, expose them through authenticated FastAPI routes, and make the video service assign queued tasks at execution time. Split the Vue UI into focused page components while keeping navigation and task creation orchestration in `App.vue`.

**Tech Stack:** Python 3.12, FastAPI, Peewee/SQLite, Playwright, Vue 3, TypeScript, Lucide Vue, Vitest, pytest.

## Global Constraints

- Work in the current repository as explicitly requested by the user.
- Preserve existing user changes and data through schema migrations.
- Use Asia/Shanghai for daily quota boundaries.
- Keep authentication on every new local API route.
- Render remote strings as text, never HTML.
- Use locally bundled icons with no runtime CDN.

---

### Task 1: Settings and Quota Persistence

**Files:**
- Modify: `src/doupool/db/models.py`
- Modify: `src/doupool/db/database.py`
- Modify: `src/doupool/db/repository.py`
- Create: `src/doupool/settings/service.py`
- Create: `tests/test_settings.py`
- Modify: `tests/test_video_repository.py`

**Interfaces:**
- `SettingsService.get() -> dict`
- `SettingsService.update(values: dict) -> dict`
- `SettingsService.backup() -> Path`
- Repository methods for settings, quota reset, task assignment, log clearing, and successful results.

- [ ] Write failing tests proving defaults, validation, persistence, backup creation, quota reset, nullable queued tasks, and account assignment.
- [ ] Run `uv run pytest tests/test_settings.py tests/test_video_repository.py -q` and confirm failures are caused by missing behavior.
- [ ] Add `AppSetting`, quota fields, and nullable task account to models; migrate schema to version 4 with a safe SQLite table rebuild for `videotask.account_id`.
- [ ] Implement settings JSON persistence, defaults, validation, backup through SQLite's backup API, and repository quota helpers.
- [ ] Re-run focused tests until they pass.

### Task 2: Quota-Aware Scheduler and Rate-Limit Failover

**Files:**
- Modify: `src/doupool/video/protocol.py`
- Modify: `src/doupool/video/service.py`
- Modify: `tests/test_video_protocol.py`
- Modify: `tests/test_video_service.py`

**Interfaces:**
- `DoubaoRateLimited(RuntimeError)` raised only for a server `STREAM_ERROR` with `error_msg == "rate limited"`.
- `VideoTaskService.start(...)` always persists a queued task.
- The worker assigns an eligible account, increments quota after acceptance, cools limited accounts until the next reset, and retries another account.

- [ ] Add failing tests for typed rate-limit parsing, queued task creation without accounts, successful assignment, and two-account failover.
- [ ] Run focused tests and verify RED.
- [ ] Implement typed protocol error, background assignment loop, daily reset checks, quota increments, account cooldown, and safe shutdown.
- [ ] Run focused tests and verify GREEN.

### Task 3: Settings, Logs, Accounts, and Results APIs

**Files:**
- Modify: `src/doupool/api/app.py`
- Modify: `src/doupool/main.py`
- Modify: `src/doupool/logging/setup.py`
- Modify: `tests/test_api.py`

**Interfaces:**
- `GET/PUT /api/settings`
- `POST /api/settings/backup`
- `GET/DELETE /api/logs`
- Existing account APIs return quota fields and reject deletion during active tasks.
- Task JSON allows `account_id` and `account_name` to be null.

- [ ] Write failing API tests for settings round-trip, validation, backup, log clearing, account quota payload, nullable task payload, and protected account deletion.
- [ ] Run `uv run pytest tests/test_api.py -q` and verify RED.
- [ ] Add authenticated routes, structured log payloads, runtime log-level update, and dependency wiring from `main.py`.
- [ ] Run API tests and verify GREEN.

### Task 4: Icon Navigation and Compact Layout

**Files:**
- Modify: `frontend/package.json`
- Modify: `frontend/package-lock.json`
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/styles.css`
- Modify: `frontend/src/__tests__/App.test.ts`

**Interfaces:**
- Page enum: `accounts | videos | results | logs | settings`.
- Lucide icons: `UsersRound`, `Clapperboard`, `Download`, `ScrollText`, `Settings`.

- [ ] Update the App test to expect five working navigation items and the absence of the large account title/stat cards.
- [ ] Run the test and verify RED.
- [ ] Install `lucide-vue-next` with npm, replace text glyphs, remove `.title` and `.stats` markup, and add compact content spacing.
- [ ] Run the focused test and verify GREEN.

### Task 5: Account, Results, Logs, and Settings Pages

**Files:**
- Create: `frontend/src/components/AccountTable.vue`
- Create: `frontend/src/components/ResultsTable.vue`
- Create: `frontend/src/components/LogsPage.vue`
- Create: `frontend/src/components/SettingsPage.vue`
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/App.vue`
- Create: `frontend/src/__tests__/ManagementPages.test.ts`

**Interfaces:**
- API helpers: `updateAccount`, `deleteAccount`, `listLogs`, `clearLogs`, `getSettings`, `saveSettings`, `backupDatabase`.
- Page components emit account mutation completion or display their own operation Toast errors through an `error` event.

- [ ] Write failing component tests for account toggle/delete confirmation, successful-result actions, log filtering/clear, and settings save/backup.
- [ ] Run the focused test and verify RED.
- [ ] Implement typed API helpers and four table/form components.
- [ ] Integrate all components with `App.vue`, passing successful tasks to results.
- [ ] Run all frontend tests and verify GREEN.

### Task 6: Startup Recovery and Full Verification

**Files:**
- Modify: `src/doupool/video/service.py`
- Modify: `src/doupool/main.py`
- Modify: relevant regression tests only if a verified gap is found.

**Interfaces:**
- `VideoTaskService.resume_queued()` starts persisted queued tasks on application startup.

- [ ] Add a failing test for persisted queued-task recovery.
- [ ] Implement recovery after the event loop starts through FastAPI lifespan startup.
- [ ] Run `uv run pytest -q`.
- [ ] Run `npm --prefix frontend test -- --run`.
- [ ] Run `npm --prefix frontend run build`.
- [ ] Run `git diff --check` and inspect `git status --short` without changing unrelated user files.
