from pathlib import Path

from doupool.paths import configure_runtime_environment, frontend_dir, resource_dir


def test_resource_dir_points_at_project_root():
    root = resource_dir()
    assert (root / "pyproject.toml").exists() or (root / "src" / "doupool").exists()


def test_configure_runtime_sets_playwright_path(monkeypatch, tmp_path: Path):
    browsers = tmp_path / "ms-playwright"
    browsers.mkdir()
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", str(browsers))
    configure_runtime_environment()
    assert Path(__import__("os").environ["PLAYWRIGHT_BROWSERS_PATH"]) == browsers


def test_frontend_dir_prefers_existing_dist(tmp_path: Path, monkeypatch):
    dist = tmp_path / "frontend" / "dist"
    dist.mkdir(parents=True)
    (dist / "index.html").write_text("<div id='app'></div>", encoding="utf-8")
    monkeypatch.setenv("DOUPOOL_FRONTEND_DIR", str(dist))
    assert frontend_dir() == dist
