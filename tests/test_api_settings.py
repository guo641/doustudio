from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

from fastapi.testclient import TestClient

from doupool.api.app import create_app
from doupool.login.service import LoginService
from doupool.settings.service import DownloadDirPickerUnavailable, SettingsService


class IdleRunner:
    def run(self, *args):
        raise RuntimeError("not used")


def _client(repository, database_manager, tmp_path, *, picker=None):
    login = LoginService(repository, IdleRunner(), tmp_path / "profiles")
    settings = SettingsService(repository, tmp_path, database_manager.path)
    app = create_app(
        "secret", tmp_path / "missing", repository, login,
        settings_service=settings, download_dir_picker=picker,
    )
    return TestClient(app), settings


def test_pick_download_dir_returns_path_without_persisting(repository, database_manager, tmp_path):
    calls = []

    def picker(start):
        calls.append(start)
        return "C:\\Videos\\Picked"

    client, settings = _client(repository, database_manager, tmp_path, picker=picker)
    response = client.post(
        "/api/settings/pick-download-dir",
        headers={"X-DouPool-Token": "secret"},
        json={"start_dir": "C:\\Videos"},
    )
    assert response.status_code == 200
    assert response.json() == {"path": "C:\\Videos\\Picked"}
    assert calls == ["C:\\Videos"]
    assert settings.get()["download_dir"] != "C:\\Videos\\Picked"


def test_pick_download_dir_uses_saved_dir_when_start_omitted(repository, database_manager, tmp_path):
    calls = []
    client, settings = _client(
        repository, database_manager, tmp_path,
        picker=lambda start: calls.append(start) or None,
    )
    saved = str(tmp_path / "downloads")
    settings.update({"download_dir": saved})
    response = client.post(
        "/api/settings/pick-download-dir",
        headers={"X-DouPool-Token": "secret"},
        json={},
    )
    assert response.status_code == 200
    assert response.json() == {"path": None}
    assert calls == [saved]


def test_pick_download_dir_no_desktop_picker_is_503(repository, database_manager, tmp_path):
    client, _ = _client(repository, database_manager, tmp_path)
    response = client.post(
        "/api/settings/pick-download-dir",
        headers={"X-DouPool-Token": "secret"}, json={},
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "桌面窗口未就绪,无法打开目录选择器"


def test_pick_download_dir_unavailable_picker_is_503(repository, database_manager, tmp_path):
    def picker(_start):
        raise DownloadDirPickerUnavailable("not ready")
    client, _ = _client(repository, database_manager, tmp_path, picker=picker)
    response = client.post(
        "/api/settings/pick-download-dir",
        headers={"X-DouPool-Token": "secret"}, json={},
    )
    assert response.status_code == 503


def test_pick_download_dir_picker_error_is_cancel_like_200(repository, database_manager, tmp_path):
    def picker(_start):
        raise RuntimeError("dialog failed")
    client, _ = _client(repository, database_manager, tmp_path, picker=picker)
    response = client.post(
        "/api/settings/pick-download-dir",
        headers={"X-DouPool-Token": "secret"}, json={},
    )
    assert response.status_code == 200
    assert response.json() == {"path": None}


def test_pick_download_dir_serializes_modal_picker(repository, database_manager, tmp_path):
    entered = Event()
    release = Event()

    def picker(_start):
        entered.set()
        release.wait(timeout=3)
        return "/chosen"

    client, _ = _client(repository, database_manager, tmp_path, picker=picker)
    headers = {"X-DouPool-Token": "secret"}
    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(
            client.post, "/api/settings/pick-download-dir", headers=headers, json={}
        )
        assert entered.wait(timeout=3)
        second = client.post(
            "/api/settings/pick-download-dir", headers=headers, json={}
        )
        assert second.status_code == 409
        assert second.json()["detail"] == "目录选择器已在使用中"
        release.set()
        assert first.result(timeout=3).json() == {"path": "/chosen"}


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
    client, _ = _client(
        repository, database_manager, tmp_path,
        picker=lambda _start: str(tmp_path / "picked"),
    )
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
