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
    assert client.get("/api/health").json() == {"status": "ok"}


def test_spa_injects_token_when_index_has_no_head(repository, tmp_path):
    frontend = tmp_path / "dist"
    frontend.mkdir()
    (frontend / "index.html").write_text('<div id="app"></div>', encoding="utf-8")
    service = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", frontend, repository, service))

    response = client.get("/")

    assert '<meta name="doupool-token" content="secret">' in response.text


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

    assert client.get("/api/settings", headers=headers).json()["daily_quota"] == 5
    updated = client.put("/api/settings", headers=headers, json={"daily_quota": 7})
    assert updated.status_code == 200
    assert updated.json()["daily_quota"] == 7
    assert client.put("/api/settings", headers=headers, json={"max_concurrency": 0}).status_code == 422
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
        profile_dir=temp_profile, video_quota_used=3, video_quota_date=date(2026, 7, 13),
    )
    repository.create_video_task(account.id, "运行中", "seedance_v2.0_mini", "1:1", 5)
    task = repository.list_video_tasks()[0]
    repository.update_video_task(task.id, status="generating")
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    headers = {"X-DouPool-Token": "secret"}

    payload = client.get("/api/accounts", headers=headers).json()[0]
    assert payload["video_quota_used"] == 3
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
