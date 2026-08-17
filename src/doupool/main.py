from __future__ import annotations

import io
import os
import sys

# v0.3.0.1:GUI 子系统 EXE(sys.stdout/sys.stderr=None)下,uvicorn / loguru /
# 等任意依赖 print / isatty() 的库会在 dictConfig / 格式化器初始化时炸
# AttributeError: 'NoneType' object has no attribute 'isatty' → 进程秒崩,
# 双击 exe 看到的就是「什么都没发生」。这里兜底成 StringIO 让所有 stdio
# 调用都安全返回(不会真打日志,但不会让进程死)。日志本来就写到
# settings.log_dir 里,不影响用户。
if sys.stdout is None:
    sys.stdout = io.StringIO()
if sys.stderr is None:
    sys.stderr = io.StringIO()

# v0.3.0:离线激活闸门 — import-time 触发。**必须在 configure_runtime_environment
# 之后、其余模块 import 之前**,这样闸门才能在 Playwright / FastAPI / 数据库
# 任何重活之前拒收未授权进程。哪怕用户 monkey-patch 掉 main() 函数体,这行
# import 仍会按设计触发 sys.exit(7) / 静默退出(expired)。
import doupool.license.verify_at_import  # noqa: F401, E402  # v0.3.0 激活闸门

from doupool.paths import configure_runtime_environment

# Must run before any Playwright import.
configure_runtime_environment()

# v0.3.1:半在线心跳 — 启动握手 + 后台 daemon。
#
# 设计:
#   - 启动握手是同步的(主 UI 渲染前),网络抽风 / 撤销命令也只 log,
#     绝不阻塞 UI —— grace 7d 兜底。
#   - daemon.start() 是非阻塞的:内部启守护线程,默认 24h 一次。
#   - 必须先 import verify_at_import(闸门已经先 import 了 .pyd),
#     否则 heartbeat / bootstrap 调 current_fingerprint() 拿到 'uncompiled'。
#
# --print-fingerprint 旁路:开发者子命令不应该 daemon 长跑 / 触发网络。
# 把握手 + daemon 放到 if 块外,但加 sys.argv 排除。
_IS_FINGERPRINT_CMD = "--print-fingerprint" in sys.argv

if not _IS_FINGERPRINT_CMD:
    from doupool.license import bootstrap as _license_bootstrap
    from doupool.license import heartbeat_daemon as _heartbeat_daemon

    _license_bootstrap.run_startup_handshake()
    _heartbeat_daemon.start()

# v0.3.0:开发者无 GUI 抓指纹用 --print-fingerprint 子命令。在做任何数据库 /
# Playwright / 浏览器初始化之前就走,免得开发者重发一次码要等 GUI 起。
# 必须先 import verify_at_import,这样 _license_verify.cp312-win_amd64.pyd
# 已经加载(否则 get_activation_status / current_fingerprint 会返降级值)。
if "--print-fingerprint" in sys.argv:
    from doupool.license import current_fingerprint, get_activation_status
    print(f"fingerprint={current_fingerprint()}")
    print(f"status={get_activation_status()}")
    sys.exit(0)

# v0.3.1.1:开发者 headless 验证激活握手 —— 复现用户报告的"激活码格式错误"
# 路径,直接调 lic.activate(CODE),不走 GUI 也不写盘(headless 容器看不到
# MainWindowHandle,见 verification-discipline)。用法:
#   DouStudio.exe --test-activate <base32>.<base32>
# 退出码:0 = ok,1 = fail
if "--test-activate" in sys.argv:
    idx = sys.argv.index("--test-activate")
    if idx + 1 >= len(sys.argv):
        print("usage: --test-activate <code>", file=sys.stderr)
        sys.exit(2)
    code = sys.argv[idx + 1]
    from doupool.license import activate
    ok, err = activate(code)
    print(f"ok={ok}")
    print(f"err={err!r}")
    sys.exit(0 if ok else 1)

from doupool.api.app import create_app
from doupool.config import Settings
from doupool.db.database import DatabaseManager
from doupool.db.repository import AccountRepository
from doupool.desktop import DesktopRuntime, _pick_download_dir
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
        download_dir_picker=_pick_download_dir,
        current_version=settings.version,
    )
    try:
        DesktopRuntime(app, settings.debug).run()
    finally:
        manager.close()


if __name__ == "__main__":
    main()
