@echo off
setlocal EnableExtensions
cd /d "%~dp0"

rem ===== 解析 uv:tools\uv.exe → 全局 uv → 提示 winget 安装 =====
set "UV="

if exist "%~dp0tools\uv.exe" (
  set "UV=%~dp0tools\uv.exe"
) else (
  where uv >nul 2>&1
  if not errorlevel 1 (
    set "UV=uv"
  ) else (
    echo [DouPool] 缺少 uv,请选择一种方式安装:
    echo   1. winget install --id=astral-sh.uv
    echo   2. 或下载 uv.exe 放到 %~dp0tools\uv.exe
    echo   3. 或安装 Node.js 后通过 npm i -g @astral/uv
    exit /b 1
  )
)

where node >nul 2>&1
if errorlevel 1 (
  echo [DouPool] 缺少 Node.js，请先安装并加入 PATH: https://nodejs.org/
  exit /b 1
)

where npm >nul 2>&1
if errorlevel 1 (
  echo [DouPool] 缺少 npm，请先安装 Node.js: https://nodejs.org/
  exit /b 1
)

echo [DouPool] 使用 UV: "%UV%"
"%UV%" --version

echo [DouPool] 构建前端...
pushd frontend
call npm ci
if errorlevel 1 (
  echo [DouPool] npm ci 失败
  popd
  exit /b 1
)
call npm run build
if errorlevel 1 (
  echo [DouPool] 前端构建失败
  popd
  exit /b 1
)
popd

echo [DouPool] 同步 Python 依赖...
"%UV%" sync
if errorlevel 1 (
  echo [DouPool] uv sync 失败
  exit /b 1
)

echo [DouPool] 检查 Playwright Chromium...
"%UV%" run python -c "from playwright.sync_api import sync_playwright; p=sync_playwright().start(); assert p.chromium.executable_path.exists(); p.stop()" >nul 2>&1
if errorlevel 1 (
  echo [DouPool] 首次安装 Chromium，请稍候...
  "%UV%" run playwright install chromium
  if errorlevel 1 (
    echo [DouPool] Chromium 安装失败
    exit /b 1
  )
)

echo [DouPool] 启动桌面应用...
"%UV%" run python -m doupool.main
set "EXIT_CODE=%ERRORLEVEL%"
if not "%EXIT_CODE%"=="0" (
  echo [DouPool] 应用退出码: %EXIT_CODE%
)
exit /b %EXIT_CODE%