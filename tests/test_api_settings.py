from unittest.mock import patch

from fastapi.testclient import TestClient

from doupool.api.app import create_app
from doupool.login.service import LoginService
from doupool.settings.service import SettingsService


class IdleRunner:
    def run(self, *args):
        raise RuntimeError("not used")


def _client(repository, database_manager, tmp_path):
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    settings = SettingsService(repository, tmp_path, database_manager.path)
    app = create_app("secret", tmp_path / "missing", repository, login, settings_service=settings)
    return TestClient(app), settings


def test_pick_download_dir_returns_path_without_persisting(repository, database_manager, tmp_path):
    client, settings = _client(repository, database_manager, tmp_path)
    with patch("doupool.api.app.pick_directory", return_value="C:\\Videos\\Picked") as pick:
        response = client.post(
            "/api/settings/pick-download-dir",
            headers={"X-DouPool-Token": "secret"},
            json={"start_dir": "C:\\Videos"},
        )
    assert response.status_code == 200
    assert response.json() == {"path": "C:\\Videos\\Picked"}
    pick.assert_called_once_with("C:\\Videos")
    assert settings.get()["download_dir"] != "C:\\Videos\\Picked"


def test_pick_download_dir_uses_saved_dir_when_start_omitted(repository, database_manager, tmp_path):
    client, settings = _client(repository, database_manager, tmp_path)
    saved = str(tmp_path / "downloads")
    settings.update({"download_dir": saved})
    with patch("doupool.api.app.pick_directory", return_value=None) as pick:
        response = client.post(
            "/api/settings/pick-download-dir",
            headers={"X-DouPool-Token": "secret"},
            json={},
        )
    assert response.status_code == 200
    assert response.json() == {"path": None}
    pick.assert_called_once_with(saved)


def test_pick_download_dir_requires_settings_service(repository, tmp_path):
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    client = TestClient(create_app("secret", tmp_path / "missing", repository, login))
    response = client.post(
        "/api/settings/pick-download-dir",
        headers={"X-DouPool-Token": "secret"},
        json={},
    )
    assert response.status_code == 503


def test_pick_download_dir_requires_local_token(repository, database_manager, tmp_path):
    client, _ = _client(repository, database_manager, tmp_path)
    response = client.post("/api/settings/pick-download-dir", json={})
    assert response.status_code == 401


def test_open_download_dir_returns_open_status(repository, database_manager, tmp_path):
    client, _ = _client(repository, database_manager, tmp_path)
    with patch("doupool.api.app.open_directory", return_value=True) as open_dir:
        response = client.post(
            "/api/settings/open-dir",
            headers={"X-DouPool-Token": "secret"},
            json={"path": str(tmp_path)},
        )
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    open_dir.assert_called_once_with(str(tmp_path))


def test_open_download_dir_empty_path_is_noop(repository, database_manager, tmp_path):
    client, _ = _client(repository, database_manager, tmp_path)
    with patch("doupool.api.app.open_directory") as open_dir:
        response = client.post(
            "/api/settings/open-dir",
            headers={"X-DouPool-Token": "secret"},
            json={},
        )
    assert response.status_code == 200
    assert response.json() == {"ok": False}
    open_dir.assert_not_called()
