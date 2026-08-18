from fastapi.testclient import TestClient
from pathlib import Path

import pytest

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
        # v0.2.35:start 现在返 (first_task, partial_rejected);stub 也返同形状
        task = self.repository.create_video_task(
            values.get("account_id"),
            values["prompt"],
            values["model"],
            values["ratio"],
            values["duration"],
            mode=values.get("mode") or "t2v",
            image_paths=None,
            group_id=values.get("group_id"),
            group_name=values.get("group_name"),
        )
        return task, []


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
    import pytest

    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, service))
    # v0.3.0:这条测试检查「无激活」分支,需要绕过 conftest 的自动 mock。
    from doupool import license as _lic
    real_status = _lic.get_activation_status
    _lic.get_activation_status = lambda: "missing"
    try:
        data = client.get("/api/health").json()
    finally:
        _lic.get_activation_status = real_status
    assert data["status"] == "degraded"
    assert "version" in data  # 启动时回填的 DouStudio 版本号
    assert data["activated"] is False
    assert "license_status" in data


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

    # v0.2.35:跨账号凑余额 — 200 OK + {task, partial_rejected} 包装
    assert response.status_code == 200
    body = response.json()
    assert body["task"]["status"] == "queued"
    assert body["task"]["duration"] == 10
    assert body["partial_rejected"] == []
    listed = client.get("/api/video-tasks", headers={"X-DouPool-Token": "secret"})
    assert listed.status_code == 200
    assert listed.json()[0]["account_name"] == "账号一"


def test_create_video_task_round_trips_group_name(repository, tmp_path, temp_profile):
    from doupool.db.models import Account

    account = Account.create(
        id="account-group-name-api", display_name="账号", doubao_user_id="user-group-name-api",
        profile_dir=temp_profile,
    )
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app(
        "secret", tmp_path / "missing", repository, login, FakeVideoService(repository),
    ))

    response = client.post(
        "/api/video-tasks",
        headers={"X-DouPool-Token": "secret"},
        json={
            "account_id": account.id,
            "prompt": "一只猫",
            "model": "seedance_v2.0_mini",
            "ratio": "1:1",
            "duration": 5,
            "group_name": "美女蛇",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["task"]["group_name"] == "美女蛇"


def test_create_video_task_normalizes_malformed_duration_before_service(
    repository, tmp_path, temp_profile,
):
    from doupool.db.models import Account

    Account.create(
        id="account-duration",
        display_name="账号",
        doubao_user_id="user-duration",
        profile_dir=temp_profile,
    )
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app(
        "secret", tmp_path / "missing", repository, login, FakeVideoService(repository)
    ))

    response = client.post(
        "/api/video-tasks",
        headers={"X-DouPool-Token": "secret"},
        json={
            "prompt": "固定十秒",
            "model": "seedance_v2.0_mini",
            "ratio": "1:1",
            "duration": {"malformed": True},
        },
    )

    assert response.status_code == 200
    assert response.json()["task"]["duration"] == 10


@pytest.mark.parametrize(
    "payload",
    [
        {"mode": "i2v", "images": []},
        {
            "mode": "t2v",
            "images": [{"name": "legacy.png", "data_base64": "eA=="}],
        },
    ],
)
def test_create_video_task_rejects_i2v_and_image_inputs(
    repository, tmp_path, payload,
):
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app(
        "secret", tmp_path / "missing", repository, login, FakeVideoService(repository)
    ))
    body = {
        "prompt": "不允许图生",
        "model": "seedance_v2.0_mini",
        "ratio": "1:1",
        **payload,
    }

    response = client.post(
        "/api/video-tasks",
        headers={"X-DouPool-Token": "secret"},
        json=body,
    )

    assert response.status_code == 422
    assert "当前版本仅支持文生视频" in response.text
    assert repository.list_video_tasks() == []


class FakeVideoServicePartialRejected(FakeVideoService):
    """v0.2.35:测试 partial_rejected 响应字段——first task 成功,
    第二/三条触发 partial_rejected 列表(模拟全账号满)。"""
    def __init__(self, repository, *, rejected_indices=(2, 3)):
        super().__init__(repository)
        self._rejected_indices = rejected_indices
        self._created = 0

    def start(self, **values):
        # 给 prompts 模式用:多 prompt 时按 index 决定 partial_rejected
        prompts = values.get("prompts") or [values.get("prompt", "")]
        first_task = None
        partial = []
        for idx, p in enumerate(prompts, start=1):
            t = self.repository.create_video_task(
                values.get("account_id"),
                p,
                values["model"],
                values["ratio"],
                values["duration"],
                mode=values.get("mode") or "t2v",
                image_paths=None,
            )
            if first_task is None:
                first_task = t
            if idx in self._rejected_indices:
                partial.append({"index": idx, "prompt": p, "reason": "stub 拒"})
        return first_task, partial


def test_create_video_task_body_rejects_prompt_over_5000_chars():
    """v0.2.37.3:画面描述 2000→5000 字,单值 + 列表元素都按 5000 封顶。
    直接构造 CreateVideoTaskBody 触发 Pydantic 校验,不走 HTTP。
    """
    from pydantic import ValidationError

    from doupool.api.app import CreateVideoTaskBody

    boundary_ok = CreateVideoTaskBody(prompt="a" * 5000)
    assert len(boundary_ok.prompt) == 5000

    over_by_one = {"prompt": "a" * 5001}
    try:
        CreateVideoTaskBody(**over_by_one)
    except ValidationError as exc:
        assert "prompt" in str(exc)
    else:
        raise AssertionError("5001-char prompt should fail validation")

    # v0.2.37.3:`prompts` 列表中每个元素也按 5000 字封顶(field_validator 兜底)
    CreateVideoTaskBody(prompts=["x" * 5000, "short"])
    over_prompts = {"prompts": ["x" * 5001]}
    try:
        CreateVideoTaskBody(**over_prompts)
    except ValidationError as exc:
        assert "prompts" in str(exc)
    else:
        raise AssertionError("prompts element over 5000 chars should fail validation")

    assert CreateVideoTaskBody(group_name="组" * 40).group_name == "组" * 40
    with pytest.raises(ValidationError):
        CreateVideoTaskBody(group_name="组" * 41)


def test_create_video_task_returns_partial_rejected_when_accounts_full(repository, tmp_path, temp_profile):
    """v0.2.35:跨账号凑余额 —— 200 OK + partial_rejected 包含被拒 prompt 信息。"""
    from doupool.db.models import Account
    Account.create(
        id="account-1", display_name="账号一", doubao_user_id="user-1", profile_dir=temp_profile,
    )
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    # 前 2 个 prompt 成功,第 3 个进 partial_rejected
    videos = FakeVideoServicePartialRejected(
        repository, rejected_indices=(3,),
    )
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login, videos))

    response = client.post(
        "/api/video-tasks",
        headers={"X-DouPool-Token": "secret"},
        json={
            "account_id": None,  # 走跨账号凑余额分支
            "prompts": ["p1", "p2", "p3"],
            "model": "seedance_v2.0_mini",
            "ratio": "1:1",
            "duration": 5,
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["task"]["status"] == "queued"
    # partial_rejected 透传前端,Toast 展示
    assert len(body["partial_rejected"]) == 1
    assert body["partial_rejected"][0]["index"] == 3
    assert body["partial_rejected"][0]["prompt"] == "p3"
    assert "stub 拒" in body["partial_rejected"][0]["reason"]


def test_settings_round_trip_backup_and_validation(repository, database_manager, tmp_path):
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    settings = SettingsService(repository, tmp_path, database_manager.path)
    client = TestClient(create_app(
        "secret", tmp_path / "missing", repository, login,
        settings_service=settings,
    ))
    headers = {"X-DouPool-Token": "secret"}

    initial = client.get("/api/settings", headers=headers).json()
    # v0.2.29:共享额度池(豆包每天每账号 50 点,不按模型分桶)。
    assert initial["daily_quota_shared"] == 50
    # max_concurrency 默认 1
    assert initial["max_concurrency"] == 1
    assert initial["default_duration"] == 10
    # 单独更新共享池
    updated = client.put("/api/settings", headers=headers, json={"daily_quota_shared": 7})
    assert updated.status_code == 200
    assert updated.json()["daily_quota_shared"] == 7
    # 并发上限 51 拒
    assert client.put("/api/settings", headers=headers, json={"max_concurrency": 0}).status_code == 422
    assert client.put("/api/settings", headers=headers, json={"max_concurrency": 51}).status_code == 422
    # v0.3.6:任意输入值都规整为固定 10 秒。
    for value in (3, 11, "bad", None):
        normalized = client.put(
            "/api/settings", headers=headers, json={"default_duration": value}
        )
        assert normalized.status_code == 200
        assert normalized.json()["default_duration"] == 10
    # daily_quota_shared 200 拒(范围 1..100)
    assert client.put("/api/settings", headers=headers, json={"daily_quota_shared": 200}).status_code == 422
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


def test_account_payload_has_shared_quota_and_active_task_blocks_delete(repository, tmp_path, temp_profile):
    """v0.2.29:共享池 —— _account_dict 主字段是 video_quota_used_shared/total_shared。

    旧 mini/v2/std 字段 alias 到 shared(给老前端缓存兜底),值相同。
    共享池下,任意 task 模型的额度显示都用 shared 一桶。
    """
    from datetime import date
    from doupool.db.models import Account

    # v0.2.29:直接写 shared 桶,不走 mini/v2/std 迁移路径,值确定。
    account = Account.create(
        id="account-quota", display_name="额度账号", doubao_user_id="quota-user",
        profile_dir=temp_profile,
        video_quota_used_shared=3,
        video_quota_date=date(2026, 7, 13),
    )
    repository.create_video_task(account.id, "运行中", "seedance_v2.0_mini", "1:1", 5)
    task = repository.list_video_tasks()[0]
    repository.update_video_task(task.id, status="generating")
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    payload = client.get("/api/accounts", headers=headers).json()[0]
    # v0.2.29 共享池主字段
    assert payload["video_quota_used_shared"] == 3
    assert payload["video_quota_total_shared"] == 50  # 无 settings_service 时默认 50
    # legacy alias
    assert payload["video_quota_used"] == 3
    # 旧 mini/v2/std 镜像到 shared
    assert payload["video_quota_used_mini"] == 3
    assert payload["video_quota_used_v2"] == 3
    assert payload["video_quota_used_std"] == 3
    # total 来自 settings(无 settings_service 时默认 50)
    assert payload["video_quota_total_mini"] == 50
    assert payload["video_quota_total_v2"] == 50
    assert payload["video_quota_total_std"] == 50
    assert client.delete(f"/api/accounts/{account.id}", headers=headers).status_code == 409


# --- v0.2.29:手动重置额度端点 + legacy 迁移在 lifespan 自动跑 ---


def test_account_payload_after_legacy_migration_sums_three_buckets(repository, tmp_path, temp_profile):
    """v0.2.29:lifespan 自动跑 migrate_legacy_quota_buckets。

    老账号(shared=0 但 mini+v2+std>0)在 TestClient lifespan 启动后被自动迁移:
    旧三桶累加进 shared。`_account_dict` 显示 shared_used = 45,旧字段 alias 全部 45。
    """
    from datetime import date
    from doupool.db.models import Account

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")

    # 进入 TestClient 上下文(触发生命周期 → migrate_legacy_quota_buckets 跑一次,此时 DB 空)
    with TestClient(create_app("secret", tmp_path / "missing", repository, login)) as _client:
        # lifespan 跑完后再创建老账号(模拟真实场景:用户重启后老 DB 已迁移完,
        # 这条 create 不会再次触发迁移,因为 lifespan 只跑一次)
        # 显式调一次模拟「lifespan 期间老 DB 还没写入」的场景 —— 在 lifespan 之外调用
        pass
    # 现在 DB 是 lifespan 跑过后的状态。手动模拟「lifespan 期间有老桶残留」:
    # 写一个老桶残留账号,再显式调迁移(等于用户升级前 DB 状态被运维脚本触发迁移)。
    Account.create(
        id="account-legacy", display_name="老账号", doubao_user_id="user-legacy",
        profile_dir=temp_profile,
        video_quota_used_mini=10, video_quota_used_v2=20, video_quota_used_std=15,
        video_quota_date=date(2026, 7, 13),
    )
    migrated = repository.migrate_legacy_quota_buckets()
    assert migrated == 1

    # 重新进 TestClient(新 lifespan 跑迁移,会把这条已经迁过的跳过)
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    payload = client.get("/api/accounts", headers=headers).json()[0]
    # 旧三桶被累加进 shared,值是 10+20+15=45
    assert payload["video_quota_used_shared"] == 45
    # 旧字段 alias 到 shared → 都是 45(共享池不分模型)
    assert payload["video_quota_used_mini"] == 45
    assert payload["video_quota_used_v2"] == 45
    assert payload["video_quota_used_std"] == 45


def test_lifespan_auto_migrates_legacy_buckets_on_startup(repository, tmp_path, temp_profile):
    """v0.2.29:TestClient lifespan 进入时自动跑迁移。

    模拟真实启动流程 —— lifespan 进入前账号已存在(老 DB 写入),
    lifespan 钩子里自动调迁移,_account_dict 读出来 shared=45。
    用 `with TestClient(...)` 走上下文,触发 starlette 的 lifespan.startup。
    """
    from datetime import date
    from doupool.db.models import Account

    # 在 TestClient 上下文之外写入老账号(共享池 + 老三桶残留)
    Account.create(
        id="account-legacy-auto", display_name="lifespan 自动迁", doubao_user_id="u-auto",
        profile_dir=temp_profile,
        video_quota_used_mini=10, video_quota_used_v2=20, video_quota_used_std=15,
        video_quota_date=date(2026, 7, 13),
    )

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    headers = {"X-DouPool-Token": "secret"}
    # 必须用 `with` —— starlette 的 TestClient 只有走 __enter__/__exit__
    # 才会真的发 lifespan.startup 事件。直接 TestClient(...) 不会触发。
    with TestClient(create_app("secret", tmp_path / "missing", repository, login)) as client:
        payload = client.get("/api/accounts", headers=headers).json()[0]
        assert payload["video_quota_used_shared"] == 45


def test_reset_account_quota_endpoint_clears_shared_bucket(repository, tmp_path, temp_profile):
    """v0.2.29:POST /api/accounts/{id}/reset-quota 清 shared 桶 + limited_until。"""
    from datetime import date, datetime
    from doupool.db.models import Account

    account = Account.create(
        id="account-reset", display_name="被限流", doubao_user_id="u-reset",
        profile_dir=temp_profile,
        video_quota_used_shared=42,
        video_limited_until=datetime(2026, 7, 13, 16, 0),
        video_quota_date=date(2026, 7, 12),
    )
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post(
        f"/api/accounts/{account.id}/reset-quota",
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reset_count"] == 1
    assert body["account_id"] == account.id
    assert "reset_at" in body

    # 验证 DB 字段真的被清了
    refreshed = Account.get_by_id(account.id)
    assert refreshed.video_quota_used_shared == 0
    assert refreshed.video_limited_until is None
    # video_quota_date 跟其他 reset 端点一致,被推到 today(business_date)


def test_reset_account_quota_endpoint_returns_404_for_missing_account(repository, tmp_path):
    """v0.2.29:重置不存在的账号 → 404。"""
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post(
        "/api/accounts/missing-id/reset-quota",
        headers=headers,
    )
    assert response.status_code == 404
    assert "not found" in response.json()["detail"].lower()


def test_reset_all_quota_endpoint_clears_enabled_accounts_only(repository, tmp_path, temp_profile):
    """v0.2.29:一键重置只清 enabled 账号 —— disabled 是用户显式关的。"""
    from datetime import datetime
    from doupool.db.models import Account

    enabled = Account.create(
        id="account-enabled", display_name="enabled", doubao_user_id="u-e",
        profile_dir=temp_profile, enabled=True,
        video_quota_used_shared=40, video_limited_until=datetime(2026, 7, 13, 16, 0),
    )
    disabled = Account.create(
        id="account-disabled", display_name="disabled", doubao_user_id="u-d",
        profile_dir=temp_profile, enabled=False,
        video_quota_used_shared=40, video_limited_until=datetime(2026, 7, 13, 16, 0),
    )
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post(
        "/api/accounts/reset-all-quota",
        headers=headers,
    )
    assert response.status_code == 200
    body = response.json()
    assert body["reset_count"] == 1  # 只清了 enabled

    enabled_refreshed = Account.get_by_id(enabled.id)
    disabled_refreshed = Account.get_by_id(disabled.id)
    assert enabled_refreshed.video_quota_used_shared == 0
    assert enabled_refreshed.video_limited_until is None
    # disabled 不动
    assert disabled_refreshed.video_quota_used_shared == 40
    assert disabled_refreshed.video_limited_until == datetime(2026, 7, 13, 16, 0)


def test_reset_all_quota_endpoint_returns_zero_when_no_enabled_accounts(repository, tmp_path, temp_profile):
    """v0.2.29:没有 enabled 账号 → reset_count=0(不是 404,前端按成功处理)。"""
    from doupool.db.models import Account
    Account.create(
        id="account-only-disabled", display_name="d", doubao_user_id="u",
        profile_dir=temp_profile, enabled=False,
        video_quota_used_shared=50,
    )
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post("/api/accounts/reset-all-quota", headers=headers)
    assert response.status_code == 200
    assert response.json()["reset_count"] == 0


def test_reset_quota_endpoints_require_token(repository, tmp_path):
    """v0.2.29:重置端点必须鉴权,不能匿名调。"""
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))

    # 不带 token → 401
    assert client.post("/api/accounts/x/reset-quota").status_code == 401
    assert client.post("/api/accounts/reset-all-quota").status_code == 401


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


# ---------- v0.2.35:一键清除(任务 + 结果) ----------
class _FakeVideoServiceWithClear(FakeVideoService):
    """v0.2.35:接入 clear_tasks / clear_results —— 直接代理 repository 方法。"""

    def clear_tasks(self, target: str) -> int:
        # 测试仅校验端点签名 + 退额度 + 删除,具体 _pre_charged_tasks 走
        # 真实 service.start 路径不便测,所以这里直接调 repository 的批量
        # 删除 + 退额度。pre-charge 由 service 内部按 in-memory 状态管理,
        # 我们让 _pre_charged_tasks 留空(单测 case 不需要模拟预扣)。
        statuses = ("succeeded", "failed", "cancelled") if target == "completed" else ("queued",)
        tasks = self.repository.list_video_tasks_by_statuses(statuses)
        # 模拟退额度(只调 decrement_account_quota,by=0 边界由 _pre_charged 守门)
        # 此处 _FakeVideoServiceWithClear 不持有 _pre_charged,跳过退额度
        # (测过端点 + repository.list/delete 已 OK)。
        return self.repository.delete_video_tasks_by_ids([t.id for t in tasks])

    def clear_results(self, *, downloaded_only: bool = False) -> int:
        tasks = self.repository.list_succeeded_results(
            with_download_url=True if downloaded_only else None,
        )
        return self.repository.delete_video_tasks_by_ids([t.id for t in tasks])


def _seed_clear_tasks(repository, profile_dir):
    """铺 4 条任务:1 queued + 1 succeeded + 1 failed + 1 cancelled + 1 running(不动)"""
    from doupool.db.models import Account, VideoTask
    account = Account.create(
        id="acc-clear", display_name="clear 账号", doubao_user_id="u-clear",
        profile_dir=profile_dir, enabled=True, status="active",
    )
    queued = repository.create_video_task(None, "排队的", "seedance_v2.0_mini", "1:1", 5)
    succ = repository.create_video_task(account.id, "成功的", "seedance_v2.0_mini", "1:1", 5)
    repository.update_video_task(succ.id, status="succeeded", result_url="https://x.test/succ.mp4")
    failed = repository.create_video_task(account.id, "失败的", "seedance_v2.0_mini", "1:1", 5)
    repository.update_video_task(failed.id, status="failed", error="x")
    cancelled = repository.create_video_task(account.id, "取消的", "seedance_v2.0_mini", "1:1", 5)
    repository.update_video_task(cancelled.id, status="cancelled")
    running = repository.create_video_task(account.id, "运行中", "seedance_v2.0_mini", "1:1", 5)
    repository.update_video_task(running.id, status="generating")
    return {
        "queued": queued.id,
        "succeeded": succ.id,
        "failed": failed.id,
        "cancelled": cancelled.id,
        "running": running.id,
    }


def test_clear_completed_video_tasks(repository, tmp_path, temp_profile):
    """clear-completed 端点:succeeded/failed/cancelled 全删,running 保留。"""
    ids = _seed_clear_tasks(repository, temp_profile)
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app(
        "secret", tmp_path / "missing", repository, login,
        video_service=_FakeVideoServiceWithClear(repository),
    ))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post("/api/video-tasks/clear-completed", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted_count": 3}
    # queued + running 保留
    remaining = {t.id for t in repository.list_video_tasks()}
    assert remaining == {ids["queued"], ids["running"]}


def test_clear_queued_video_tasks(repository, tmp_path, temp_profile):
    """clear-queued 端点:只删 queued,running 和 completed 都不动。"""
    ids = _seed_clear_tasks(repository, temp_profile)
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app(
        "secret", tmp_path / "missing", repository, login,
        video_service=_FakeVideoServiceWithClear(repository),
    ))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post("/api/video-tasks/clear-queued", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted_count": 1}
    # 4 条保留:succeeded/failed/cancelled/running
    remaining = {t.id for t in repository.list_video_tasks()}
    assert remaining == {ids["succeeded"], ids["failed"], ids["cancelled"], ids["running"]}


def test_clear_endpoints_require_auth(repository, tmp_path):
    """401:clear 端点也要鉴权。"""
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app(
        "secret", tmp_path / "missing", repository, login,
        video_service=_FakeVideoServiceWithClear(repository),
    ))
    for path in (
        "/api/video-tasks/clear-completed",
        "/api/video-tasks/clear-queued",
        "/api/results/clear-downloaded",
        "/api/results/clear-all",
    ):
        response = client.post(path)
        assert response.status_code == 401, path


def test_clear_endpoints_return_503_when_service_down(repository, tmp_path):
    """503:video_service 未启动。"""
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    for path in (
        "/api/video-tasks/clear-completed",
        "/api/video-tasks/clear-queued",
        "/api/results/clear-downloaded",
        "/api/results/clear-all",
    ):
        response = client.post(path, headers={"X-DouPool-Token": "secret"})
        assert response.status_code == 503, path


def _seed_results_tasks(repository, profile_dir):
    """铺 3 条 succeeded:1 有 clean,1 只有 result_url,1 两个 URL 都没有。"""
    from doupool.db.models import Account, VideoTask
    account = Account.create(
        id="acc-res", display_name="res 账号", doubao_user_id="u-res",
        profile_dir=profile_dir, enabled=True, status="active",
    )
    with_clean = repository.create_video_task(account.id, "有 clean", "seedance_v2.0_mini", "1:1", 5)
    repository.update_video_task(with_clean.id, status="succeeded")
    VideoTask.update(clean_video_url="https://x.test/clean.mp4").where(
        VideoTask.id == with_clean.id
    ).execute()
    with_result = repository.create_video_task(account.id, "只有 result", "seedance_v2.0_mini", "1:1", 5)
    repository.update_video_task(with_result.id, status="succeeded", result_url="https://x.test/res.mp4")
    no_url = repository.create_video_task(account.id, "无 URL", "seedance_v2.0_mini", "1:1", 5)
    repository.update_video_task(no_url.id, status="succeeded")
    return {"with_clean": with_clean.id, "with_result": with_result.id, "no_url": no_url.id}


def test_clear_downloaded_results_only_removes_tasks_with_url(repository, tmp_path, temp_profile):
    """clear-downloaded:只删 clean_video_url OR result_url IS NOT NULL 的 succeeded。"""
    ids = _seed_results_tasks(repository, temp_profile)
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app(
        "secret", tmp_path / "missing", repository, login,
        video_service=_FakeVideoServiceWithClear(repository),
    ))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post("/api/results/clear-downloaded", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted_count": 2}
    # no_url 保留
    remaining = {t.id for t in repository.list_video_tasks()}
    assert remaining == {ids["no_url"]}


def test_clear_all_results_removes_every_succeeded(repository, tmp_path, temp_profile):
    """clear-all:全部 succeeded 都删,包括无 URL 的。"""
    ids = _seed_results_tasks(repository, temp_profile)
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app(
        "secret", tmp_path / "missing", repository, login,
        video_service=_FakeVideoServiceWithClear(repository),
    ))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post("/api/results/clear-all", headers=headers)

    assert response.status_code == 200, response.text
    assert response.json() == {"deleted_count": 3}
    assert repository.list_video_tasks() == []


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


def test_get_webmssdk_tokens_returns_available_false_on_unexpected_exception(
    repository, tmp_path, temp_profile, monkeypatch,
):
    """v0.2.36:extract 抛了非 TokenBundleUnavailable 异常(profile 路径含特殊字符 /
    sqlite3.DatabaseError 漏网 / 其他 OSError)→ 不能 500 让前端拿不到原因。

    场景:账号已登录,token bundle 抽不到(可能 web_id 缺失或 SQLite 损坏)。
    旧版会 500 → 前端用兜底文案「token 状态加载失败」(用户迷惑:账号明明已登录)。
    现在应该 200 + available=False + hint 携带真实异常类名,让前端/用户能区分
    "真的没 token"vs"系统读不到"。
    """
    from doupool.db.models import Account

    account = Account.create(
        id="account-broken", display_name="损坏 profile", doubao_user_id="u",
        profile_dir=temp_profile,
    )

    def fake_extract(profile_dir):
        # 模拟:Profile 路径上的 SQLite 文件被损坏,抛 sqlite3.DatabaseError
        # —— 不是预期的 TokenBundleUnavailable,旧版本会 500。
        import sqlite3
        raise sqlite3.DatabaseError("database disk image is malformed")

    monkeypatch.setattr("doupool.api.app.extract_webmssdk_tokens", fake_extract)

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))

    response = client.get(
        f"/api/accounts/{account.id}/webmssdk-tokens",
        headers={"X-DouPool-Token": "secret"},
    )

    assert response.status_code == 200, (
        "v0.2.36: 非预期异常也应返回 200 + available=False,不能 500 让前端崩溃"
    )
    payload = response.json()
    assert payload["available"] is False
    assert "DatabaseError" in payload["hint"], (
        f"v0.2.36: hint 必须携带真实异常类型,让前端能定位根因; got {payload['hint']!r}"
    )
    assert "database disk image is malformed" in payload["hint"]
    assert payload["web_id"] == ""
    assert payload["ms_token_preview"] == ""


def test_get_webmssdk_tokens_returns_available_false_on_runtime_error(
    repository, tmp_path, temp_profile, monkeypatch,
):
    """v0.2.36:兜底异常路径 —— extract 抛 RuntimeError(比如路径含非法字符 /
    Windows 长路径 / 其他 ProfileDir 异常)也走 200 + available=False 而非 500。"""
    from doupool.db.models import Account

    account = Account.create(
        id="account-rt", display_name="RuntimeError", doubao_user_id="u",
        profile_dir=temp_profile,
    )

    def fake_extract(profile_dir):
        raise RuntimeError("profile dir 含非法字符 ✗")

    monkeypatch.setattr("doupool.api.app.extract_webmssdk_tokens", fake_extract)

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))

    response = client.get(
        f"/api/accounts/{account.id}/webmssdk-tokens",
        headers={"X-DouPool-Token": "secret"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["available"] is False
    assert "RuntimeError" in payload["hint"]
    assert "profile dir 含非法字符" in payload["hint"]


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

                def is_closed(self):
                    return False

                def cookies(self, urls=None):
                    return [
                        {"name": "msToken", "value": "ms_refreshed_zzz",
                         "domain": ".doubao.com", "path": "/"},
                        {"name": "_signature", "value": "sig_refreshed",
                         "domain": ".doubao.com", "path": "/"},
                    ]

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

                def is_closed(self):
                    return False

                def cookies(self, urls=None):
                    return [
                        {"name": "msToken", "value": "ms_fake",
                         "domain": ".doubao.com", "path": "/"},
                    ]

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


# ============================================================
# v0.2.37.2:re-export-cookies 端点 —— 让 Playwright 重新打开浏览器拉一次
# cookies 写回 cookies.json,适合 SQLite DPAPI 加密读不出明文时用户兜底用。
# ============================================================


def test_re_export_cookies_returns_ok_when_cookies_saved(
    repository, tmp_path, temp_profile, monkeypatch,
):
    """v0.2.37.2:Playwright 拉到 doubao.com cookie → 200 + ok/saved=True + elapsed。"""
    from doupool.db.models import Account

    account = Account.create(
        id="account-reexport-1", display_name="账号1", doubao_user_id="re1",
        profile_dir=temp_profile,
    )

    class _Page:
        def goto(self, url, **kw):
            pass

        def wait_for_timeout(self, ms):
            pass

    class FakeBrowser:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @property
        def chromium(self):
            return self

        def launch_persistent_context(self, profile_dir, **kwargs):
            # 验证 endpoint 走可视化窗口 + 偏移位置(避免主屏闪烁)
            assert kwargs.get("headless") is False
            assert "--window-position=-2400,-2400" in kwargs["args"]

            class _Ctx:
                def __init__(self):
                    self.pages = [_Page()]

                def is_closed(self):
                    return False

                def close(self):
                    pass

                def new_page(self):
                    return _Page()

                def cookies(self, urls=None):
                    return [
                        {"name": "msToken", "value": "ms_reexport",
                         "domain": ".doubao.com", "path": "/"},
                        {"name": "_signature", "value": "sig_reexport",
                         "domain": ".doubao.com", "path": "/"},
                    ]

            return _Ctx()

    monkeypatch.setattr("doupool.api.app.sync_playwright", lambda: FakeBrowser())

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post(
        f"/api/accounts/{account.id}/re-export-cookies",
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["saved"] is True
    assert payload["elapsed"] >= 0
    assert "已重新导出 cookies.json" in payload["hint"]
    # 关键副作用:cookies.json 真的写出来了
    cookies_json = Path(temp_profile) / "cookies.json"
    assert cookies_json.exists()
    import json as _json
    data = _json.loads(cookies_json.read_text(encoding="utf-8"))
    assert any(c["name"] == "msToken" for c in data)


def test_re_export_cookies_returns_400_when_no_doubao_cookies(
    repository, tmp_path, temp_profile, monkeypatch,
):
    """v0.2.37.2:Playwright 拉到 cookie 但没有 doubao.com 域 → 400,提示用户重新扫码。

    场景:账号掉登录、或另一个用户在该 profile 登录了别的域名。
    """
    from doupool.db.models import Account

    account = Account.create(
        id="account-reexport-empty", display_name="空账号", doubao_user_id="re_empty",
        profile_dir=temp_profile,
    )

    class _Page:
        def goto(self, url, **kw):
            pass

        def wait_for_timeout(self, ms):
            pass

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
                pages = []  # 空 → 让 endpoint 走 new_page() 分支

                def is_closed(self):
                    return False

                def close(self):
                    pass

                def new_page(self):
                    return _Page()

                def cookies(self, urls=None):
                    return []  # 空数组 → 没有 doubao.com cookie

            return _Ctx()

    monkeypatch.setattr("doupool.api.app.sync_playwright", lambda: FakeBrowser())

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post(
        f"/api/accounts/{account.id}/re-export-cookies",
        headers=headers,
    )

    assert response.status_code == 400
    detail = response.json()["detail"]
    assert "掉登录" in detail or "重新扫码" in detail


def test_re_export_cookies_returns_503_when_playwright_raises(
    repository, tmp_path, temp_profile, monkeypatch,
):
    """v0.2.37.2:Playwright 启动失败 / Chromium 没装 → 503,跟 refresh-tokens 一致。"""
    from doupool.db.models import Account

    account = Account.create(
        id="account-reexport-fail", display_name="坏账号", doubao_user_id="re_fail",
        profile_dir=temp_profile,
    )

    def fake_playwright():
        raise RuntimeError("Chromium 没装")

    monkeypatch.setattr("doupool.api.app.sync_playwright", fake_playwright)

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post(
        f"/api/accounts/{account.id}/re-export-cookies",
        headers=headers,
    )

    assert response.status_code == 503
    assert "Chromium 没装" in response.json()["detail"]


def test_re_export_cookies_404_when_account_missing(repository, tmp_path):
    """v0.2.37.2:对不存在的账号调 re-export-cookies → 404,不触发 Playwright。"""
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))

    response = client.post(
        "/api/accounts/does-not-exist/re-export-cookies",
        headers={"X-DouPool-Token": "secret"},
    )

    assert response.status_code == 404


def test_re_export_cookies_requires_auth(repository, tmp_path):
    """v0.2.37.2:无 token → 401,跟其他端点一致。"""
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))

    response = client.post("/api/accounts/anything/re-export-cookies")

    assert response.status_code == 401


# ============================================================
# v0.2.20:open-browser / close-browser / browser-status 端点
# ============================================================


class _FakeOpenBrowserCtx:
    """open-browser 用的 fake Chromium context —— 让 _open_browser_runner 自然退出。"""

    def __init__(self):
        self.pages = []

    def is_closed(self):
        return False

    def on(self, event, handler):
        pass

    def new_page(self):
        class _P:
            url = ""

            def is_closed(self):
                return True

            def on(self, *args, **kwargs):
                pass

            def goto(self, *args, **kwargs):
                pass

            def wait_for_timeout(self, ms):
                pass

        p = _P()
        self.pages.append(p)
        return p

    def close(self):
        pass


class _FakeOpenBrowserPW:
    """open-browser 用的 fake sync_playwright —— 让 runner 拿到 fake context。"""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    @property
    def chromium(self):
        return self

    def launch_persistent_context(self, profile_dir, **kwargs):
        assert kwargs["headless"] is False
        # open-browser 必须非隐身(不要 -2400,-2400 那套),让用户能看到窗口
        assert "--window-position=-2400,-2400" not in (kwargs.get("args") or [])
        return _FakeOpenBrowserCtx()


def test_open_browser_returns_202_and_starts_runner(
    repository, tmp_path, temp_profile, monkeypatch,
):
    """v0.2.20:open-browser 202 + 触发 Playwright runner 在后台跑(用 fake 验证
    runner 真起 + 端点不阻塞)。
    """
    from doupool.db.models import Account

    account = Account.create(
        id="acc-open-1", display_name="可打开", doubao_user_id="open-user",
        profile_dir=temp_profile,
    )

    monkeypatch.setattr(
        "doupool.api.app.sync_playwright",
        lambda: _FakeOpenBrowserPW(),
    )

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post(f"/api/accounts/{account.id}/open-browser", headers=headers)

    assert response.status_code == 202
    payload = response.json()
    assert payload["ok"] is True
    assert payload["account_id"] == account.id
    assert "浏览器窗口已启动" in payload["message"]

    # cleanup:runner thread 自己会退出(get_active() 返回空 → break),但保险起见
    # 再调一次 close-browser 让 registry 状态干净。
    client.post(f"/api/accounts/{account.id}/close-browser", headers=headers)


def test_open_browser_404_when_account_missing(repository, tmp_path):
    """v0.2.20:打开不存在的账号 → 404。"""
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))

    response = client.post(
        "/api/accounts/missing/open-browser",
        headers={"X-DouPool-Token": "secret"},
    )
    assert response.status_code == 404


def test_open_browser_409_when_profile_dir_missing(
    repository, tmp_path, monkeypatch,
):
    """v0.2.20:账号存在但 profile_dir 被删了 → 409 引导用户重新登录。"""
    from doupool.db.models import Account

    ghost_dir = tmp_path / "ghost-profile"
    # 不创建,Account.profile_dir 指向不存在目录
    account = Account.create(
        id="acc-ghost", display_name="幽灵账号", doubao_user_id="ghost-user",
        profile_dir=ghost_dir,
    )

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post(f"/api/accounts/{account.id}/open-browser", headers=headers)
    assert response.status_code == 409
    assert "profile 目录不存在" in response.json()["detail"]


def test_open_browser_requires_auth(repository, tmp_path):
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    response = client.post("/api/accounts/anything/open-browser")
    assert response.status_code == 401


def test_browser_status_reflects_registry_state(repository, tmp_path):
    """v0.2.20:browser-status 跟住 registry,没窗口 → open=False。"""
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))

    response = client.get(
        "/api/accounts/never-opened/browser-status",
        headers={"X-DouPool-Token": "secret"},
    )
    assert response.status_code == 200
    assert response.json() == {"account_id": "never-opened", "open": False}


def test_close_browser_returns_cancel_sent_false_for_unknown_account(repository, tmp_path):
    """v0.2.20:close-browser 调从未打开过的账号 → ok=True + cancel_sent=False。"""
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    response = client.post(
        "/api/accounts/never-touched/close-browser", headers=headers
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    assert payload["cancel_sent"] is False
    assert "没有打开的浏览器窗口" in payload["message"]


def test_open_then_close_browser_lifecycle(
    repository, tmp_path, temp_profile, monkeypatch,
):
    """v0.2.20:open → status=open=True → close → status=open=False。"""
    from doupool.db.models import Account

    account = Account.create(
        id="acc-lifecycle", display_name="生命周期", doubao_user_id="lc-user",
        profile_dir=temp_profile,
    )

    monkeypatch.setattr(
        "doupool.api.app.sync_playwright",
        lambda: _FakeOpenBrowserPW(),
    )

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    # open
    open_resp = client.post(
        f"/api/accounts/{account.id}/open-browser", headers=headers
    )
    assert open_resp.status_code == 202

    # close
    close_resp = client.post(
        f"/api/accounts/{account.id}/close-browser", headers=headers
    )
    assert close_resp.status_code == 200
    assert close_resp.json()["cancel_sent"] is True


# ---------- v0.2.22 Q4:POST /api/results/{task_id}/refresh-url ----------


class FakeVideoServiceWithRefresh(FakeVideoService):
    """v0.2.22 Q4:复刻 service.schedule_refresh_url 的最小契约。

    返回的 wrapper 可 await,await 后直接修改 task.result_url 并返回新 task,
    够 API 层测试路由 + 状态码映射(404/409/200)。"""
    def __init__(self, repository, *, raise_value_error: str | None = None, raise_runtime_error: str | None = None):
        super().__init__(repository)
        self.raise_value_error = raise_value_error
        self.raise_runtime_error = raise_runtime_error

    def schedule_refresh_url(self, task_id: str):
        if self.raise_value_error:
            async def boom():
                raise ValueError(self.raise_value_error)
            return boom()
        if self.raise_runtime_error:
            async def boom():
                raise RuntimeError(self.raise_runtime_error)
            return boom()

        async def wrapper():
            task = self.repository.get_video_task(task_id)
            if task is None:
                raise ValueError("任务不存在")
            # 模拟 runner.recheck_result 拿到 fresh URL
            self.repository.update_video_task(
                task_id,
                result_url="https://fresh.example/video.mp4",
                backup_result_url="https://fresh.example/backup.mp4",
                fallback_result_url="https://fresh.example/fallback.mp4",
            )
            return self.repository.get_video_task(task_id)

        return wrapper()


def test_refresh_url_endpoint_writes_fresh_url(repository, tmp_path):
    """v0.2.22 Q4:POST /api/results/:task_id/refresh-url → 200 + 新 result_url。"""
    task = repository.create_video_task(None, "下载", "seedance_v2.0_mini", "1:1", 5)
    repository.update_video_task(
        task.id, status="succeeded", result_url="https://old.example/video.mp4",
    )
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(
        create_app(
            "secret", tmp_path / "missing", repository, login,
            video_service=FakeVideoServiceWithRefresh(repository),
        )
    )
    headers = {"X-DouPool-Token": "secret"}

    response = client.post(f"/api/results/{task.id}/refresh-url", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == task.id
    assert body["result_url"] == "https://fresh.example/video.mp4"
    assert body["backup_result_url"] == "https://fresh.example/backup.mp4"
    assert body["fallback_result_url"] == "https://fresh.example/fallback.mp4"


def test_refresh_url_endpoint_missing_task_returns_404(repository, tmp_path):
    """v0.2.22 Q4:任务不存在 → 404(不是 500)。"""
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(
        create_app(
            "secret", tmp_path / "missing", repository, login,
            video_service=FakeVideoServiceWithRefresh(
                repository, raise_value_error="任务不存在",
            ),
        )
    )
    headers = {"X-DouPool-Token": "secret"}

    response = client.post("/api/results/does-not-exist/refresh-url", headers=headers)
    assert response.status_code == 404
    assert "任务不存在" in response.json()["detail"]


def test_refresh_url_endpoint_non_succeeded_returns_409(repository, tmp_path):
    """v0.2.22 Q4:仅 succeeded 任务支持 refresh-url,失败/排队中 → 409。"""
    task = repository.create_video_task(None, "未完成", "seedance_v2.0_mini", "1:1", 5)
    # 默认 status = queued
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(
        create_app(
            "secret", tmp_path / "missing", repository, login,
            video_service=FakeVideoServiceWithRefresh(
                repository, raise_value_error="仅 succeeded 任务支持刷新下载链接",
            ),
        )
    )
    headers = {"X-DouPool-Token": "secret"}

    response = client.post(f"/api/results/{task.id}/refresh-url", headers=headers)
    assert response.status_code == 409
    assert "仅 succeeded" in response.json()["detail"]


# ---------- v0.2.28 Q2:批量任务按组下载到独立文件夹 ----------
# 三个用例覆盖:
#  1) 正常路径 —— 3 条 succeeded 任务,httpx 流式落盘,文件名 `{group_index:02d}_{HHMMSS}_{prompt前12字符}.mp4`
#  2) 过期签名 —— httpx 返回 403 → 409 提示用户先点刷新
#  3) 空组 —— group_id 不存在 → 404

class _FakeChunkedResponse:
    """模拟 httpx 流式响应:status_code + aiter_bytes"""
    def __init__(self, status_code: int, payload: bytes):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def aiter_bytes(self, chunk_size: int = 65536):
        # 单 chunk 一次吐完
        yield self._payload


class _FakeStreamClient:
    """模拟 httpx.AsyncClient.stream('GET', url)"""
    def __init__(self, response_map: dict[str, _FakeChunkedResponse]):
        self._response_map = response_map
        self.calls: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def stream(self, method: str, url: str):
        self.calls.append(url)
        # 取最后一次路径段做 key(测试里 url = https://example.test/v1.mp4)
        # 简化:完整 url 当 key
        if url not in self._response_map:
            raise RuntimeError(f"unexpected url {url}")
        # 返回一个异步 ctx manager
        resp = self._response_map[url]
        return resp


def _seed_grouped_tasks(repository, group_id: str, count: int = 3):
    """在指定 group_id 下建 count 条 succeeded 任务,result_url 用
    https://example.test/v1.mp4 ... v{count}.mp4。"""
    urls = {f"https://example.test/v{i}.mp4": _FakeChunkedResponse(200, f"video-{i}".encode()) for i in range(1, count + 1)}
    task_ids = []
    for i in range(1, count + 1):
        task = repository.create_video_task(
            None, f"段{i}", "seedance_v2.0_mini", "1:1", 5,
        )
        # 后端逻辑里 group_id 是建任务时外部传入的(批量提交时由 service 设),
        # repository 没有 update_group 工具,直接走 SQL 模拟:
        from doupool.db.models import VideoTask
        VideoTask.update(group_id=group_id, group_index=i).where(VideoTask.id == task.id).execute()
        repository.update_video_task(task.id, status="succeeded", result_url=f"https://example.test/v{i}.mp4")
        task_ids.append(task.id)
    return task_ids, urls


# ---------- v0.2.35:批量下载命名 —— 单条任务 download_filename 字段 ----------
class _FakeTaskForFilename:
    """最小 _build_download_filename 鸭子类型:只读 prompt / created_at /
    group_index / clean_video_url / id,无需 DB 实例。"""

    def __init__(self, *, prompt: str, group_index: int = 0,
                 created_at=None, clean_video_url: str | None = None,
                 id=None):
        from datetime import datetime as _dt
        import hashlib as _h
        self.prompt = prompt
        self.group_index = group_index
        self.created_at = created_at or _dt(2026, 1, 2, 13, 51, 45)
        self.clean_video_url = clean_video_url
        # 默认 id 用 prompt+group+ts 拼一个稳定串,这样不同 fixture
        # 自动产生不同 hash,无需手填 UUID
        self.id = id if id is not None else f"fake-{prompt}-{group_index}-{self.created_at.isoformat()}"


def _expected_hash(task) -> str:
    """根据 task.id 计算 SHA1 前 8 字符(跟 _build_download_filename 内部保持一致)。"""
    import hashlib as _h
    return _h.sha1(str(task.id).encode("utf-8", errors="replace")).hexdigest()[:8]


def test_build_download_filename_format_basic():
    """v0.2.35:基础格式 `{group_index:02d}_{HHMMSS}_{prompt前12字符}_{id_hash}.mp4`"""
    from doupool.api.app import _build_download_filename
    task = _FakeTaskForFilename(prompt="猫在草地上跑", group_index=1)
    fn = _build_download_filename(task)
    assert fn == f"01_135145_猫在草地上跑_{_expected_hash(task)}.mp4"


def test_build_download_filename_truncates_long_prompt():
    """超过 12 字符的 prompt 只截前 12 字符"""
    from doupool.api.app import _build_download_filename, _sanitize_filename_part
    long_prompt = "猫猫在长长的草地上拼命奔跑跳跃"
    assert len(long_prompt) > 12  # 确认测试本身在跑长字符串分支
    task = _FakeTaskForFilename(prompt=long_prompt, group_index=3)
    fn = _build_download_filename(task)
    # 文件名 stem 部分 = `03_135145_` + prompt前 12 字符 + _ + id_hash(8)
    stem = fn.removesuffix(".mp4")
    expected_hash = _expected_hash(task)
    name_part = stem.removeprefix("03_135145_")
    # 前 12 字符是 prompt 截断,后 8 字符是 hash,中间用 `_` 隔开
    assert name_part == f"{long_prompt[:12]}_{expected_hash}"
    # 完整文件名格式校验
    assert fn.startswith("03_135145_") and fn.endswith(f"_{expected_hash}.mp4")


def test_build_download_filename_sanitizes_illegal_chars():
    """Windows 非法字符 \\ / : * ? " < > | 与控制字符统一换成 _"""
    from doupool.api.app import _build_download_filename, _sanitize_filename_part
    task = _FakeTaskForFilename(prompt='a/b\\c:d*e', group_index=5)
    fn = _build_download_filename(task)
    # 直接验证 sanitize 行为
    sanitized = _sanitize_filename_part('a/b\\c:d*e')
    assert sanitized == "a_b_c_d_e"
    assert fn == f"05_135145_a_b_c_d_e_{_expected_hash(task)}.mp4"


def test_build_download_filename_appends_clean_when_present():
    """有 clean_video_url 时文件名追加 -clean 后缀(优先于重名去重 -N)"""
    from doupool.api.app import _build_download_filename
    task = _FakeTaskForFilename(prompt="猫", group_index=2, clean_video_url="https://x.test/clean.mp4")
    fn = _build_download_filename(task)
    assert fn == f"02_135145_猫_{_expected_hash(task)}-clean.mp4"


def test_build_download_filename_handles_empty_prompt():
    """空 prompt 兜底为 'video'(避免纯 '_' / 空后缀)"""
    from doupool.api.app import _build_download_filename
    task = _FakeTaskForFilename(prompt="", group_index=0)
    fn = _build_download_filename(task)
    assert fn == f"00_135145_video_{_expected_hash(task)}.mp4"


def test_build_download_filename_zero_group_index_for_single_task():
    """单条任务 group_index=0 → 仍然落到 01_ 前缀(统一格式,排序稳定)"""
    from doupool.api.app import _build_download_filename
    task = _FakeTaskForFilename(prompt="单独的任务", group_index=0)
    fn = _build_download_filename(task)
    assert fn == f"00_135145_单独的任务_{_expected_hash(task)}.mp4"


def test_build_download_filename_dedup_by_task_id_hash():
    """v0.3.3:同 prompt + 同 group_index + 同秒 → task_id 不同 → 文件名不撞。

    这是 race 防御的次生防线:即使 parse_creation_result race 防御漏过,DB
    两条都拿到错 URL,本文件名 hash 也能让 group_download 时本地落盘
    两个文件不互相覆盖,用户在视觉上能看到「确实有两条」。
    """
    from datetime import datetime as _dt
    from doupool.api.app import _build_download_filename

    same_when = _dt(2026, 8, 13, 10, 30, 45)
    # 同样的 prompt + group_index + 创建时间,只 task.id 不同
    task1 = _FakeTaskForFilename(
        prompt="同样的 prompt", group_index=0, created_at=same_when,
        id="uuid-aaaa-bbbb-cccc-dddd-eeee-ffff",
    )
    task2 = _FakeTaskForFilename(
        prompt="同样的 prompt", group_index=0, created_at=same_when,
        id="uuid-1111-2222-3333-4444-5555-6666",
    )
    name1 = _build_download_filename(task1)
    name2 = _build_download_filename(task2)
    assert name1 != name2, f"v0.3.3 race 防御次生防线失败:{name1} == {name2}"
    # hash 是 8 字符 hex
    h1 = name1.removesuffix(".mp4").split("_")[-1]
    h2 = name2.removesuffix(".mp4").split("_")[-1]
    assert len(h1) == 8 and len(h2) == 8
    assert h1 != h2


def test_group_download_streams_all_videos(repository, tmp_path, database_manager, monkeypatch):
    """正常路径:3 条 succeeded 任务,httpx 流式落盘 settings.download_dir/<batch_folder>/。"""
    settings = SettingsService(repository, tmp_path, database_manager.path)
    settings.update({"download_dir": str(tmp_path / "downloads")})

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app(
        "secret", tmp_path / "missing", repository, login,
        video_service=FakeVideoService(repository),
        settings_service=settings,
    ))
    headers = {"X-DouPool-Token": "secret"}

    group_id = "abcdef12-3456-7890-abcd-ef1234567890"
    task_ids, urls = _seed_grouped_tasks(repository, group_id, count=3)

    fake = _FakeStreamClient(urls)
    monkeypatch.setattr("doupool.api.app.httpx.AsyncClient", lambda *a, **kw: fake)

    response = client.post("/api/results/group-download", headers=headers, json={"group_id": group_id})

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["file_count"] == 3
    saved_dir = Path(body["saved_dir"])
    assert saved_dir.exists()
    assert saved_dir.name.startswith("abcdef12_")  # {group_id 前 8 位}_{HHMMSS}
    # v0.2.35 + v0.3.3:批量下载命名 —— 文件名格式
    # `{group_index:02d}_{HHMMSS}_{prompt前12字符}_{task_id短哈希}.mp4`
    # v0.3.3 加 SHA1 后缀避免同 group_index + 同秒撞名。测试任务的 prompt 是
    # "段1/段2/段3"(≤12 字符,无需截断),所以期望形如
    # `01_HHMMSS_段1_<8位hash>.mp4`。我们只断言文件名后缀 `_段{N}.mp4`,
    # 前缀用 glob 模糊匹配(hash 用 `[0-9a-f]×8` 收窄)。
    for i, tid in enumerate(task_ids, 1):
        matches = list(saved_dir.glob(f"??_*_段{i}_[0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f][0-9a-f].mp4"))
        assert len(matches) == 1, f"期望恰好 1 个匹配 段{i} 的 mp4,实际 {matches}"
        fp = matches[0]
        assert fp.read_bytes() == f"video-{i}".encode()
        # 顺便回归:旧 `doubao-<id>.mp4` 不应再出现
        assert not (saved_dir / f"doubao-{tid}.mp4").exists()
    # httpx.stream GET 调用 3 次(每条任务一次)
    assert len(fake.calls) == 3


def test_group_download_prefers_sanitized_group_name(
    repository, tmp_path, database_manager, monkeypatch,
):
    """v0.3.8:批量下载目录优先使用组名,并清洗 Windows 非法字符。"""
    settings = SettingsService(repository, tmp_path, database_manager.path)
    settings.update({"download_dir": str(tmp_path / "downloads")})
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app(
        "secret", tmp_path / "missing", repository, login,
        video_service=FakeVideoService(repository),
        settings_service=settings,
    ))
    headers = {"X-DouPool-Token": "secret"}
    group_id = "named-group-1"
    _task_ids, urls = _seed_grouped_tasks(repository, group_id, count=1)
    from doupool.db.models import VideoTask
    VideoTask.update(group_name="美女蛇/竖屏").where(VideoTask.group_id == group_id).execute()

    fake = _FakeStreamClient(urls)
    monkeypatch.setattr("doupool.api.app.httpx.AsyncClient", lambda *a, **kw: fake)
    response = client.post(
        "/api/results/group-download", headers=headers, json={"group_id": group_id},
    )

    assert response.status_code == 200, response.text
    saved_dir = Path(response.json()["saved_dir"])
    assert saved_dir.name == "美女蛇_竖屏"


def test_group_download_handles_expired_signature(repository, tmp_path, database_manager, monkeypatch):
    """httpx 返回 403 → 端点返 409,提示用户先刷新下载链接。"""
    settings = SettingsService(repository, tmp_path, database_manager.path)
    settings.update({"download_dir": str(tmp_path / "downloads")})

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app(
        "secret", tmp_path / "missing", repository, login,
        video_service=FakeVideoService(repository),
        settings_service=settings,
    ))
    headers = {"X-DouPool-Token": "secret"}

    group_id = "expired-group-1"
    _seed_grouped_tasks(repository, group_id, count=1)
    # 把签名 URL 改成会触发 403 的占位
    from doupool.db.models import VideoTask
    VideoTask.update(result_url="https://example.test/expired.mp4").where(VideoTask.group_id == group_id).execute()

    fake = _FakeStreamClient({
        "https://example.test/expired.mp4": _FakeChunkedResponse(403, b""),
    })
    monkeypatch.setattr("doupool.api.app.httpx.AsyncClient", lambda *a, **kw: fake)

    response = client.post("/api/results/group-download", headers=headers, json={"group_id": group_id})
    assert response.status_code == 409
    assert "签名链接已过期" in response.json()["detail"]


def test_group_download_empty_group_returns_404(repository, tmp_path, database_manager):
    """group_id 不存在 → 404。"""
    settings = SettingsService(repository, tmp_path, database_manager.path)
    settings.update({"download_dir": str(tmp_path / "downloads")})

    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app(
        "secret", tmp_path / "missing", repository, login,
        video_service=FakeVideoService(repository),
        settings_service=settings,
    ))

    response = client.post(
        "/api/results/group-download",
        headers={"X-DouPool-Token": "secret"},
        json={"group_id": "non-existent-group"},
    )
    assert response.status_code == 404
    assert "non-existent-group" in response.json()["detail"]
