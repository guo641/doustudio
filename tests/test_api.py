from fastapi.testclient import TestClient

from doupool.api.app import create_app
from doupool.login.service import LoginService
from doupool.settings.service import SettingsService


class IdleRunner:
    def run(self, *args):
        raise RuntimeError("not used")


class FakeVideoService:
    def __init__(self, repository):
        self.repository = repository

    def start(self, **values):
        return self.repository.create_video_task(
            values.get("account_id"),
            values["prompt"],
            values["model"],
            values["ratio"],
            values["duration"],
            mode=values.get("mode") or "t2v",
            image_paths=None,
        )


def test_api_requires_local_token(repository, tmp_path):
    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, service))
    assert client.get("/api/accounts").status_code == 401


def test_accounts_list_uses_token(repository, tmp_path):
    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, service))
    response = client.get("/api/accounts", headers={"X-DouPool-Token": "secret"})
    assert response.status_code == 200
    assert response.json() == []


def test_create_login_attempt_runs_inside_event_loop(repository, tmp_path):
    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, service))

    response = client.post(
        "/api/accounts/login-attempts",
        headers={"X-DouPool-Token": "secret"},
    )

    assert response.status_code == 202
    assert response.json()["state"] == "created"


def test_health_is_available_without_token(repository, tmp_path):
    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, service))
    data = client.get("/api/health").json()
    assert data["status"] == "ok"
    assert "version" in data  # 启动时回填的 DouStudio 版本号


def test_spa_injects_token_when_index_has_no_head(repository, tmp_path):
    frontend = tmp_path / "dist"
    frontend.mkdir()
    (frontend / "index.html").write_text('<div id="app"></div>', encoding="utf-8")
    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", frontend, repository, service))

    response = client.get("/")

    # token 不再写 <meta>(会落 HTML 磁盘文件,易泄漏),而是注入到
    # window.__DOUPOOL_TOKEN__ 全局。HTML 里能看到对应的 <script> 注入块。
    assert "window.__DOUPOOL_TOKEN__" in response.text
    assert "'secret'" in response.text or "\"secret\"" in response.text


def test_create_and_list_video_tasks(repository, tmp_path, temp_profile):
    from doupool.db.models import Account

    account = Account.create(
        id="account-1", display_name="账号一", doubao_user_id="user-1", profile_dir=temp_profile
    )
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    videos = FakeVideoService(repository)
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login, videos))

    response = client.post(
        "/api/video-tasks",
        headers={"X-DouPool-Token": "secret"},
        json={
            "account_id": account.id,
            "prompt": "一只猫在草地上行走",
            "model": "seedance_v2.0_mini",
            "ratio": "1:1",
            "duration": 5,
        },
    )

    assert response.status_code == 202
    assert response.json()["status"] == "queued"
    listed = client.get("/api/video-tasks", headers={"X-DouPool-Token": "secret"})
    assert listed.status_code == 200
    assert listed.json()[0]["account_name"] == "账号一"


def test_settings_round_trip_backup_and_validation(repository, database_manager, tmp_path):
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    settings = SettingsService(repository, tmp_path, database_manager.path)
    client = TestClient(create_app(
        "secret", tmp_path / "missing", repository, login,
        settings_service=settings,
    ))
    headers = {"X-DouPool-Token": "secret"}

    initial = client.get("/api/settings", headers=headers).json()
    # v0.2.19:默认桶从 5 改成 50(豆包每天每账号 50 点)
    assert initial["daily_quota"] == 50
    # v0.2.9:三个新桶都在 defaults 里
    assert initial["daily_quota_mini"] == 50
    assert initial["daily_quota_v2"] == 50
    assert initial["daily_quota_std"] == 50
    # 三个桶独立更新
    updated = client.put("/api/settings", headers=headers, json={"daily_quota_mini": 7, "daily_quota_std": 2})
    assert updated.status_code == 200
    assert updated.json()["daily_quota_mini"] == 7
    assert updated.json()["daily_quota_std"] == 2
    assert updated.json()["daily_quota_v2"] == 50  # 未动
    assert client.put("/api/settings", headers=headers, json={"max_concurrency": 0}).status_code == 422
    assert client.put("/api/settings", headers=headers, json={"daily_quota_mini": 200}).status_code == 422
    backup = client.post("/api/settings/backup", headers=headers)
    assert backup.status_code == 201
    assert backup.json()["path"].endswith(".sqlite3")


def test_logs_can_be_listed_and_cleared(repository, tmp_path):
    from doupool.db.models import AppLog

    AppLog.create(level="ERROR", module="doupool.test", event="failed", message="测试错误")
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    listed = client.get("/api/logs", headers=headers).json()
    assert listed[0]["module"] == "doupool.test"
    assert client.delete("/api/logs", headers=headers).status_code == 204
    assert client.get("/api/logs", headers=headers).json() == []


def test_account_payload_has_quota_and_active_task_blocks_delete(repository, tmp_path, temp_profile):
    from datetime import date
    from doupool.db.models import Account

    account = Account.create(
        id="account-quota", display_name="额度账号", doubao_user_id="quota-user",
        profile_dir=temp_profile,
        video_quota_used_mini=3, video_quota_used_v2=2, video_quota_used_std=1,
        video_quota_date=date(2026, 7, 13),
    )
    repository.create_video_task(account.id, "运行中", "seedance_v2.0_mini", "1:1", 5)
    task = repository.list_video_tasks()[0]
    repository.update_video_task(task.id, status="generating")
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    payload = client.get("/api/accounts", headers=headers).json()[0]
    # v0.2.9:旧 video_quota_used alias 到 mini 桶,前端缓存兼容
    assert payload["video_quota_used"] == 3
    # 三桶全部暴露
    assert payload["video_quota_used_mini"] == 3
    assert payload["video_quota_used_v2"] == 2
    assert payload["video_quota_used_std"] == 1
    # total 来自 settings(无 settings_service 时默认 5)
    assert payload["video_quota_total_mini"] == 5
    assert payload["video_quota_total_v2"] == 5
    assert payload["video_quota_total_std"] == 5
    assert client.delete(f"/api/accounts/{account.id}", headers=headers).status_code == 409


def test_unassigned_task_payload_uses_null_account(repository, tmp_path):
    task = repository.create_video_task(None, "排队", "seedance_v2.0_mini", "1:1", 5)
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))

    payload = client.get("/api/video-tasks", headers={"X-DouPool-Token": "secret"}).json()[0]
    assert payload["id"] == task.id
    assert payload["account_id"] is None
    assert payload["account_name"] is None


def test_deleting_account_preserves_completed_video_history(repository, tmp_path, temp_profile):
    from doupool.db.models import Account, VideoTask

    account = Account.create(
        id="account-history", display_name="历史账号", doubao_user_id="history-user",
        profile_dir=temp_profile,
    )
    task = repository.create_video_task(account.id, "历史视频", "seedance_v2.0_mini", "1:1", 5)
    repository.update_video_task(task.id, status="succeeded", result_url="https://example.com/video.mp4")
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    assert client.delete(f"/api/accounts/{account.id}", headers=headers).status_code == 204
    preserved = VideoTask.get_by_id(task.id)
    assert preserved.account_id is None
    assert preserved.result_url == "https://example.com/video.mp4"


# ---------- v0.2.9 Bearer Token 鉴权 ----------
# 主鉴权头从 X-Doupool-Token 升级成同时支持 Authorization: Bearer,
# 保持向后兼容(前端 / 现有集成零改动),并对齐 yaonieyo 默认 key 风格
# 便于本机 curl / 外部脚本直连。下方测试同时覆盖三种来源:旧 header、
# 标准 Bearer(大小写不敏感)、拒绝非法 scheme / 空 token。

def test_bearer_token_authenticates(repository, tmp_path):
    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, service))

    response = client.get("/api/accounts", headers={"Authorization": "Bearer secret"})

    assert response.status_code == 200
    assert response.json() == []


def test_bearer_token_is_case_insensitive(repository, tmp_path):
    """RFC 6750 §2.1:scheme 名大小写不敏感,客户端写 "bearer" / "BEARER" 都得认。"""
    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, service))

    assert client.get("/api/accounts", headers={"Authorization": "bearer secret"}).status_code == 200
    assert client.get("/api/accounts", headers={"Authorization": "BEARER secret"}).status_code == 200
    assert client.get("/api/accounts", headers={"Authorization": "BeArEr secret"}).status_code == 200


def test_bearer_token_trims_whitespace(repository, tmp_path):
    """实操里经常多打空格,容忍一下,避免误判。"""
    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, service))

    assert client.get("/api/accounts", headers={"Authorization": "Bearer    secret"}).status_code == 200


def test_authorization_header_with_wrong_scheme_is_rejected(repository, tmp_path):
    """Basic / Digest / 自定义 scheme 一律不当成 token,避免误把任意头撞 hash。"""
    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, service))

    assert client.get("/api/accounts", headers={"Authorization": "Basic secret"}).status_code == 401
    assert client.get("/api/accounts", headers={"Authorization": "Token secret"}).status_code == 401
    assert client.get("/api/accounts", headers={"Authorization": "secret"}).status_code == 401  # 没 scheme 前缀


def test_bearer_with_empty_token_is_rejected(repository, tmp_path):
    """Bearer 后是空白 / 空串,等同于没传,必须 401。"""
    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, service))

    assert client.get("/api/accounts", headers={"Authorization": "Bearer "}).status_code == 401
    assert client.get("/api/accounts", headers={"Authorization": "Bearer"}).status_code == 401


def test_bearer_with_wrong_value_is_rejected(repository, tmp_path):
    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, service))

    assert client.get("/api/accounts", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_bearer_takes_precedence_over_legacy_header(repository, tmp_path):
    """同时传两个头时,Bearer 优先;但如果 Bearer 错,即使 legacy 对也 401。
    优先级策略明确,免得中间一拨人改 token 后两边撞不上。"""
    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, service))

    ok = {"X-DouPool-Token": "wrong", "Authorization": "Bearer secret"}
    assert client.get("/api/accounts", headers=ok).status_code == 200

    bad = {"X-DouPool-Token": "secret", "Authorization": "Bearer wrong"}
    assert client.get("/api/accounts", headers=bad).status_code == 401


def test_legacy_x_doupool_token_still_works(repository, tmp_path):
    """向后兼容:前端 / 老集成继续用 X-DouPool-Token 头不能被这次升级打断。"""
    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, service))

    assert client.get("/api/accounts", headers={"X-DouPool-Token": "secret"}).status_code == 200


def test_sse_events_accept_bearer_header(repository, tmp_path):
    """SSE 端点同样接受 Authorization: Bearer(EventSource 不能自定义 Header,
    但 curl / 集成测试能用;旧 ?access_token= 保留)。

    测试只断言鉴权层(401 vs 通过):通过鉴权的请求会进入 events() 流,
    attempt_id 不存在会触发 KeyError / 500,这是 login_service 的问题
    而非鉴权问题——所以这里用 not-401 判断鉴权层已放行。
    raise_server_exceptions=False 让 TestClient 不把 SSE 流里的 500 当
    测试异常抛出来,我们只看 HTTP 状态码。
    """
    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(
        create_app("secret", tmp_path / "missing", repository, service),
        raise_server_exceptions=False,
    )

    # query 路径保留(旧约定)
    qs = client.get("/api/login-attempts/whatever/events?access_token=secret")
    assert qs.status_code != 401
    # Bearer 路径新增
    bearer = client.get(
        "/api/login-attempts/whatever/events",
        headers={"Authorization": "Bearer secret"},
    )
    assert bearer.status_code != 401
    # 错 token 都得 401(鉴权层挡住,根本进不到 events())
    assert client.get(
        "/api/login-attempts/whatever/events",
        headers={"Authorization": "Bearer wrong"},
    ).status_code == 401
    assert client.get(
        "/api/login-attempts/whatever/events?access_token=wrong",
    ).status_code == 401


# ---------- v0.2.11:DELETE /api/requests/:task_id ----------

class FakeVideoServiceWithDelete(FakeVideoService):
    """v0.2.11:复刻 service.delete 的最小契约,够 API 层路由测试用。"""

    _RUNNING_STATUSES = ("starting", "generating", "resolving")

    def delete(self, task_id: str) -> None:
        try:
            task = self.repository.get_video_task(task_id)
        except Exception:
            raise ValueError("任务不存在") from None
        if task is None:
            raise ValueError("任务不存在")
        if task.status in self._RUNNING_STATUSES:
            raise RuntimeError("任务正在生成中,请等待结束后再删除")
        self.repository.delete_video_task(task_id)


def test_delete_video_task_returns_204(repository, tmp_path):
    """v0.2.11:删 queued/succeeded 状态的任务,assert 204。"""
    task = repository.create_video_task(None, "可删任务", "seedance_v2.0_mini", "1:1", 5)
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(
        create_app(
            "secret", tmp_path / "missing", repository, login,
            video_service=FakeVideoServiceWithDelete(repository),
        )
    )
    headers = {"X-DouPool-Token": "secret"}

    response = client.delete(f"/api/requests/{task.id}", headers=headers)

    assert response.status_code == 204
    assert repository.list_video_tasks() == []


def test_delete_video_task_running_returns_409(repository, tmp_path):
    """v0.2.11:running 状态(starting/generating/resolving)不能删,409。"""
    task = repository.create_video_task(None, "运行中", "seedance_v2.0_mini", "1:1", 5)
    repository.update_video_task(task.id, status="generating")
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(
        create_app(
            "secret", tmp_path / "missing", repository, login,
            video_service=FakeVideoServiceWithDelete(repository),
        )
    )

    response = client.delete(f"/api/requests/{task.id}", headers={"X-DouPool-Token": "secret"})

    assert response.status_code == 409
    assert "正在生成中" in response.json()["detail"]
    # 任务没被删
    assert repository.get_video_task(task.id).status == "generating"


def test_delete_video_task_missing_returns_404(repository, tmp_path):
    """v0.2.11:不存在的 task_id → 404。"""
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(
        create_app(
            "secret", tmp_path / "missing", repository, login,
            video_service=FakeVideoServiceWithDelete(repository),
        )
    )

    response = client.delete("/api/requests/does-not-exist", headers={"X-DouPool-Token": "secret"})

    assert response.status_code == 404
    assert "任务不存在" in response.json()["detail"]


def test_delete_video_task_requires_auth(repository, tmp_path):
    """v0.2.11:DELETE 也走 authorize 闸门,无 token → 401。"""
    task = repository.create_video_task(None, "鉴权测试", "seedance_v2.0_mini", "1:1", 5)
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(
        create_app(
            "secret", tmp_path / "missing", repository, login,
            video_service=FakeVideoServiceWithDelete(repository),
        )
    )

    response = client.delete(f"/api/requests/{task.id}")

    assert response.status_code == 401
    assert repository.get_video_task(task.id).status == "queued"


def test_delete_video_task_returns_503_when_service_down(repository, tmp_path):
    """v0.2.11:video_service 未启动 → 503(不静默 200)。"""
    task = repository.create_video_task(None, "服务未启动", "seedance_v2.0_mini", "1:1", 5)
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))

    response = client.delete(f"/api/requests/{task.id}", headers={"X-DouPool-Token": "secret"})

    assert response.status_code == 503
    assert repository.get_video_task(task.id).status == "queued"


# ---------- v0.2.17:WebMSSDK / TeaSDK token 状态 + 刷新端点 ----------
# 登录 profile 用同一 Chromium 实例才能让 WebMSSDK 写过 leveldb,GET 端点
# 只读 profile 不开浏览器,POST 端点才会拉起 headless=False Playwright。
# 测试里把 extract_webmssdk_tokens 和 sync_playwright 都 monkeypatch 掉,
# 避免真起浏览器 / 真读 SQLite。

def test_get_webmssdk_tokens_returns_available_bundle(repository, tmp_path, temp_profile, monkeypatch):
    """v0.2.17:profile 里能抽到完整 bundle → 200 + available=True + 字段填齐。"""
    from doupool.db.models import Account
    from doupool.video.browser import TokenBundle

    account = Account.create(
        id="account-token", display_name="token 账号", doubao_user_id="token-user",
        profile_dir=temp_profile,
    )

    def fake_extract(profile_dir):
        return TokenBundle(
            ms_token="ms_abcdef1234567890",
            web_id="wb_x",
            web_id_signature="sig_x",
            device_id="dev_x",
            tea_uuid="tu_x",
            pc_version="3.27.4",
        )

    monkeypatch.setattr("doupool.api.app.extract_webmssdk_tokens", fake_extract)

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    response = client.get(f"/api/accounts/{account.id}/webmssdk-tokens", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is True
    assert payload["hint"] == ""
    assert payload["ms_token_preview"] == "ms_abcdef123..."
    assert payload["web_id"] == "wb_x"
    assert payload["web_id_signature"] == "sig_x"
    assert payload["device_id"] == "dev_x"
    assert payload["tea_uuid"] == "tu_x"
    assert payload["pc_version"] == "3.27.4"
    assert payload["fetched_at"] > 0
    assert payload["age_seconds"] is not None


def test_get_webmssdk_tokens_returns_unavailable_when_bundle_missing(repository, tmp_path, temp_profile, monkeypatch):
    """v0.2.17:抽不到完整 bundle(冷启动 profile)→ 200 + available=False + hint 引导用户去主页。"""
    from doupool.db.models import Account
    from doupool.video.browser import TokenBundleUnavailable

    account = Account.create(
        id="account-cold", display_name="冷启动", doubao_user_id="cold-user",
        profile_dir=temp_profile,
    )

    def fake_extract(profile_dir):
        raise TokenBundleUnavailable(
            "profile 中缺少 web_id/device_id,字段: ['web_id']; "
            "请在浏览器里访问 https://www.doubao.com/chat/ 主页 5-10 秒后点「刷新 token」"
        )

    monkeypatch.setattr("doupool.api.app.extract_webmssdk_tokens", fake_extract)

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    response = client.get(f"/api/accounts/{account.id}/webmssdk-tokens", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert "web_id" in payload["hint"]
    # 字段全空,UI 区分"账号不存在"vs"token 抽不到"
    assert payload["ms_token_preview"] == ""
    assert payload["web_id"] == ""
    assert payload["device_id"] == ""
    assert payload["fetched_at"] == 0.0
    assert payload["age_seconds"] is None


def test_get_webmssdk_tokens_404_when_account_missing(repository, tmp_path, monkeypatch):
    """v0.2.17:账号不存在 → 404(不能返回 available=False 误导用户)。"""
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))

    response = client.get(
        "/api/accounts/does-not-exist/webmssdk-tokens",
        headers={"X-DouPool-Token": "secret"},
    )

    assert response.status_code == 404
    assert "account not found" in response.json()["detail"]


def test_get_webmssdk_tokens_requires_auth(repository, tmp_path):
    """v0.2.17:无 token → 401,跟其他端点一致。"""
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))

    response = client.get("/api/accounts/anything/webmssdk-tokens")

    assert response.status_code == 401


def test_refresh_tokens_returns_new_bundle(repository, tmp_path, temp_profile, monkeypatch):
    """v0.2.17:成功路径 → 202 + available=True + 包含刷后 bundle 的字段 + 耗时 hint。"""
    from doupool.db.models import Account
    from doupool.video.browser import TokenBundle

    account = Account.create(
        id="account-refresh", display_name="可刷新", doubao_user_id="refresh-user",
        profile_dir=temp_profile,
    )

    launched = {"persistent": False}

    class FakeBrowser:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @property
        def chromium(self):
            return self

        def launch_persistent_context(self, profile_dir, **kwargs):
            launched["persistent"] = True
            assert kwargs["headless"] is False
            assert "--window-position=-2400,-2400" in kwargs["args"]

            class _Ctx:
                def __init__(self):
                    self.pages = []

                def new_page(self):
                    class _Page:
                        def goto(self, url, **kw):
                            pass

                        def wait_for_timeout(self, ms):
                            pass

                    p = _Page()
                    self.pages.append(p)
                    return p

                def close(self):
                    pass

            return _Ctx()

    def fake_extract(profile_dir):
        return TokenBundle(
            ms_token="ms_refreshed_zzz",
            web_id="wb_new",
            web_id_signature="sig_new",
            device_id="dev_new",
            tea_uuid="tu_new",
            pc_version="3.27.4",
        )

    monkeypatch.setattr("doupool.api.app.sync_playwright", lambda: FakeBrowser())
    monkeypatch.setattr("doupool.api.app.extract_webmssdk_tokens", fake_extract)

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post(f"/api/accounts/{account.id}/refresh-tokens", headers=headers)

    assert response.status_code == 202
    assert launched["persistent"] is True
    payload = response.json()
    assert payload["available"] is True
    assert "刷新成功" in payload["hint"]
    assert payload["web_id"] == "wb_new"
    assert payload["web_id_signature"] == "sig_new"
    assert payload["device_id"] == "dev_new"
    assert payload["tea_uuid"] == "tu_new"
    assert payload["ms_token_preview"] == "ms_refreshed..."


def test_refresh_tokens_returns_unavailable_when_bundle_still_missing(
    repository, tmp_path, temp_profile, monkeypatch,
):
    """v0.2.17:Playwright 跑过但 leveldb 还是没 web_id → 200 + available=False + hint。"""
    from doupool.db.models import Account
    from doupool.video.browser import TokenBundleUnavailable

    account = Account.create(
        id="account-still", display_name="空 profile", doubao_user_id="still-user",
        profile_dir=temp_profile,
    )

    class FakeBrowser:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @property
        def chromium(self):
            return self

        def launch_persistent_context(self, profile_dir, **kwargs):
            class _Ctx:
                def __init__(self):
                    self.pages = []

                def new_page(self):
                    class _Page:
                        def goto(self, url, **kw):
                            pass

                        def wait_for_timeout(self, ms):
                            pass

                    p = _Page()
                    self.pages.append(p)
                    return p

                def close(self):
                    pass

            return _Ctx()

    def fake_extract(profile_dir):
        raise TokenBundleUnavailable("profile 中缺少 web_id")

    monkeypatch.setattr("doupool.api.app.sync_playwright", lambda: FakeBrowser())
    monkeypatch.setattr("doupool.api.app.extract_webmssdk_tokens", fake_extract)

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post(f"/api/accounts/{account.id}/refresh-tokens", headers=headers)

    assert response.status_code == 202
    payload = response.json()
    assert payload["available"] is False
    assert "web_id" in payload["hint"]
    assert payload["web_id"] == ""


def test_refresh_tokens_503_when_playwright_raises(
    repository, tmp_path, temp_profile, monkeypatch,
):
    """v0.2.17:Playwright 启动失败 / profile lock / Chromium 没装 → 503,不静默 200。"""
    from doupool.db.models import Account

    account = Account.create(
        id="account-broken", display_name="坏 profile", doubao_user_id="broken-user",
        profile_dir=temp_profile,
    )

    def fake_playwright():
        raise RuntimeError("Chromium 没装")

    monkeypatch.setattr("doupool.api.app.sync_playwright", fake_playwright)

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post(f"/api/accounts/{account.id}/refresh-tokens", headers=headers)

    assert response.status_code == 503
    assert "Chromium 没装" in response.json()["detail"]


def test_refresh_tokens_404_when_account_missing(repository, tmp_path):
    """v0.2.17:刷不存在的账号 → 404,不会触发 Playwright。"""
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))

    response = client.post(
        "/api/accounts/does-not-exist/refresh-tokens",
        headers={"X-DouPool-Token": "secret"},
    )

    assert response.status_code == 404


def test_refresh_tokens_requires_auth(repository, tmp_path):
    """v0.2.17:无 token → 401,跟其他端点一致。"""
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))

    response = client.post("/api/accounts/anything/refresh-tokens")

    assert response.status_code == 401
