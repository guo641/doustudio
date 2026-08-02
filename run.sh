#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

for command in uv node npm; do
  command -v "$command" >/dev/null 2>&1 || { echo "缺少依赖: $command" >&2; exit 1; }
done

cd frontend
npm ci
npm run build
cd ..

uv sync
if ! uv run python -c 'from playwright.sync_api import sync_playwright; p=sync_playwright().start(); assert p.chromium.executable_path.exists(); p.stop()' >/dev/null 2>&1; then
  uv run playwright install chromium
fi
exec uv run python -m doupool.main
