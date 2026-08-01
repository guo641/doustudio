# Build DoubaoManager Windows onedir package (exe + frontend + Playwright Chromium).
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

$appDir = Join-Path $distDir "DoubaoManager"
if (-not (Test-Path (Join-Path $appDir "DoubaoManager.exe"))) {
  throw "PyInstaller did not produce DoubaoManager.exe"
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
@"
name=DoubaoManager
version=$version
playwright_browsers=ms-playwright
"@ | Set-Content -Encoding UTF8 (Join-Path $appDir "VERSION.txt")

Write-Host "==> Zip package"
$zipPath = Join-Path $distDir "DoubaoManager-windows-x64.zip"
if (Test-Path $zipPath) { Remove-Item -Force $zipPath }
Compress-Archive -Path $appDir -DestinationPath $zipPath -Force

Write-Host ""
Write-Host "Package ready:"
Write-Host "  App: $appDir\DoubaoManager.exe"
Write-Host "  Zip: $zipPath"
Write-Host "  Browsers: $targetBrowsers"
