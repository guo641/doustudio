from __future__ import annotations

import os

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


# v0.2.9:对齐 yaonieyo 默认 token —— 进程内 API 与本机 Python / curl 互调
# 都用同一个固定 key,免得每启一次都要从 stdout 抓随机串。WebView 前端继续通过
# window.__DOUPOOL_TOKEN__ 注入(见 api.app 的 SPA handler),值与此相同;
# 想换强 key 就 export DOUPOOL_API_TOKEN=<strong> 再启动,生产部署或多用户场景
# 用得上。
DEFAULT_API_TOKEN = "local-doubao-key"


def _resolve_api_token() -> str:
    return os.environ.get("DOUPOOL_API_TOKEN") or DEFAULT_API_TOKEN


def main() -> None:
    settings = Settings.from_environment()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    manager = DatabaseManager(settings.data_dir / "doupool.sqlite3")
    manager.initialize()
    configure_logging(settings.log_dir)
    repository = AccountRepository(manager.database)
    settings_service = SettingsService(repository, settings.data_dir, manager.path)
    service = LoginService(
        repository,
        # v0.2.20:扫码成功后保持浏览器窗口 30 秒,让用户在那个窗口里
        # 访问 doubao.com/chat/ 生成 WebMSSDK token。
        PlaywrightLoginRunner(keepalive_seconds=30.0),
        settings.data_dir / "profiles",
        settings.login_timeout_seconds,
        keepalive_seconds=30.0,
    )
    video_runner = PlaywrightVideoRunner()
    # v0.2.27:把 settings 里的全局超时默认值转成秒喂给 runner.timeout。
    # runner.timeout 在每次 run() 启动 deadline 循环时读(见 browser.py:988),
    # 所以用户改完设置保存后,下一个 task 立即用新超时 —— 不需要 live reload,
    # 也不需要重启进程。task 已经跑起来的部分不会中途变更 timeout。
    video_runner.timeout = settings_service.get().get("default_timeout_minutes", 7) * 60
    video_service = VideoTaskService(
        repository,
        video_runner,
        settings_service,
        assets_dir=settings.data_dir,
    )
    app = create_app(
        _resolve_api_token(), settings.frontend_dir, repository, service,
        video_service, settings_service,
        current_version=settings.version,
    )
    try:
        DesktopRuntime(app, settings.debug).run()
    finally:
        manager.close()


if __name__ == "__main__":
    main()
