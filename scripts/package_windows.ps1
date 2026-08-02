# Build DouStudio Windows onedir package (exe + frontend + Playwright Chromium).
# Run from repository root on Windows:
#   powershell -ExecutionPolicy Bypass -File scripts\package_windows.ps1

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $Root

Write-Host "==> Root: $Root"

function Require-Command($name) {
  if (-not (Get-Command $name -ErrorAction SilentlyContinue)) {
    throw "Missing required command: $name"
  }
}

Require-Command node
Require-Command npm

$uv = Join-Path $Root "tools\uv.exe"
if (-not (Test-Path $uv)) {
  if (Get-Command uv -ErrorAction SilentlyContinue) {
    $uv = "uv"
  } else {
    throw "Missing tools\uv.exe and no global uv on PATH"
  }
}

Write-Host "==> UV: $uv"
& $uv --version

Write-Host "==> Build frontend"
Push-Location (Join-Path $Root "frontend")
try {
  npm ci
  npm run build
} finally {
  Pop-Location
}

if (-not (Test-Path (Join-Path $Root "frontend\dist\index.html"))) {
  throw "frontend/dist/index.html missing after build"
}

Write-Host "==> Sync Python deps (including pyinstaller)"
& $uv sync --group dev

$browsersPath = Join-Path $Root "ms-playwright"
$env:PLAYWRIGHT_BROWSERS_PATH = $browsersPath
Write-Host "==> Install Playwright Chromium into $browsersPath"
& $uv run playwright install chromium

if (-not (Test-Path $browsersPath)) {
  throw "Playwright browsers directory was not created: $browsersPath"
}

Write-Host "==> PyInstaller"
$distDir = Join-Path $Root "dist"
$buildDir = Join-Path $Root "build"
if (Test-Path $distDir) { Remove-Item -Recurse -Force $distDir }
if (Test-Path $buildDir) { Remove-Item -Recurse -Force $buildDir }

& $uv run pyinstaller `
  --noconfirm `
  --clean `
  --distpath $distDir `
  --workpath $buildDir `
  (Join-Path $Root "packaging\doubao_manager.spec")

$appDir = Join-Path $distDir "DouStudio"
$exeName = "DouStudio.exe"
if (-not (Test-Path (Join-Path $appDir $exeName))) {
  throw "PyInstaller did not produce $exeName"
}

Write-Host "==> Copy Playwright browsers next to exe"
$targetBrowsers = Join-Path $appDir "ms-playwright"
if (Test-Path $targetBrowsers) { Remove-Item -Recurse -Force $targetBrowsers }
Copy-Item -Recurse -Force $browsersPath $targetBrowsers

# Ensure frontend is available beside the exe as well (in case MEIPASS layout differs).
$frontendTarget = Join-Path $appDir "frontend\dist"
New-Item -ItemType Directory -Force -Path $frontendTarget | Out-Null
Copy-Item -Recurse -Force (Join-Path $Root "frontend\dist\*") $frontendTarget

Write-Host "==> Write version file"
$version = if ($env:GITHUB_REF_NAME) { $env:GITHUB_REF_NAME } else { "0.1.0-dev" }
# 规范化版本号:v0.2.0 -> 0.2.0(去掉前导 v,与 updater 的版本解析对齐)
$cleanVersion = $version.TrimStart('v')
@"
name=DouStudio
version=$cleanVersion
playwright_browsers=ms-playwright
"@ | Set-Content -Encoding UTF8 (Join-Path $appDir "VERSION.txt")

Write-Host "==> Zip package"
$plat = "windows-x86_64"
# zip 文件名保留 v 前缀 → 跟 build_exe.py / release.yml glob /
# build-windows.yml glob / updater.py 全部对齐(v0.1.0 release 也用的这个格式)
$zipName = "DouStudio-$version-$plat.zip"
$zipPath = Join-Path $distDir $zipName
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
Compress-Archive -Path $appDir -DestinationPath $zipPath -Force

Write-Host ""
Write-Host "Package ready:"
Write-Host "  App: $appDir\$exeName"
Write-Host "  Zip: $zipPath"
Write-Host "  Browsers: $targetBrowsers"