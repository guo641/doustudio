@echo off
setlocal EnableExtensions
cd /d "%~dp0"

set "UV=%~dp0tools\uv.exe"

if not exist "%UV%" (
  echo [DouPool] 缺少内置 tools\uv.exe
  echo 请确认 tools 目录完整，或从 https://github.com/astral-sh/uv/releases 下载 Windows x64 版 uv.exe 放到 tools\
  exit /b 1
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

echo [DouPool] 使用内置 UV: "%UV%"
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
