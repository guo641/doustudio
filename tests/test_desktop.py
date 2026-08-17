from unittest.mock import patch

import pytest
import webview

from doupool.desktop import _pick_download_dir, find_free_port
from doupool.settings.service import DownloadDirPickerUnavailable


def test_find_free_port_returns_loopback_port():
    port = find_free_port()
    assert isinstance(port, int)
    assert 0 < port < 65536


def test_pick_download_dir_requires_ready_webview_window():
    with patch("doupool.desktop.webview.windows", []):
        with pytest.raises(DownloadDirPickerUnavailable):
            _pick_download_dir("C:\\Downloads")


def test_pick_download_dir_uses_folder_dialog_and_returns_first_path():
    class FakeWindow:
        def create_file_dialog(self, folder, *, directory):
            self.call = (folder, directory)
            return ["C:\\Videos\\Picked"]

    window = FakeWindow()
    with patch("doupool.desktop.webview.windows", [window]):
        assert _pick_download_dir("C:\\Downloads") == "C:\\Videos\\Picked"
    assert window.call == (webview.FileDialog.FOLDER, "C:\\Downloads")


def test_pick_download_dir_cancel_returns_none():
    class FakeWindow:
        def create_file_dialog(self, folder, *, directory):
            return []

    with patch("doupool.desktop.webview.windows", [FakeWindow()]):
        assert _pick_download_dir("") is None
