from __future__ import annotations

import secrets

from doupool.paths import configure_runtime_environment

# Must run before any Playwright import.
configure_runtime_environment()

from doupool.api.app import create_app
from doupool.config import Settings
from doupool.db.database import DatabaseManager
from doupool.db.repository import AccountRepository
from doupool.desktop import DesktopRuntime
from doupool.login.browser import PlaywrightLoginRunner
from doupool.login.service import LoginService
from doupool.logging.setup import configure_logging
from doupool.settings.service import SettingsService
from doupool.video.browser import PlaywrightVideoRunner
from doupool.video.service import VideoTaskService


def main() -> None:
    settings = Settings.from_environment()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    manager = DatabaseManager(settings.data_dir / "doupool.sqlite3")
    manager.initialize()
    configure_logging(settings.log_dir)
    repository = AccountRepository(manager.database)
    settings_service = SettingsService(repository, settings.data_dir, manager.path)
    service = LoginService(repository, PlaywrightLoginRunner(), settings.data_dir / "profiles", settings.login_timeout_seconds)
    video_service = VideoTaskService(
        repository,
        PlaywrightVideoRunner(),
        settings_service,
        assets_dir=settings.data_dir,
    )
    app = create_app(
        secrets.token_urlsafe(32), settings.frontend_dir, repository, service,
        video_service, settings_service,
    )
    try:
        DesktopRuntime(app, settings.debug).run()
    finally:
        manager.close()


if __name__ == "__main__":
    main()
