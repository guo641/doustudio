from __future__ import annotations

import json
import secrets
import shutil
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from doupool.db.models import Account, LoginAttempt
from doupool.login.service import LoginAlreadyRunning
from doupool.logging.setup import set_log_level
from doupool.video.service import NoAvailableAccount


class ImageAttachmentBody(BaseModel):
    name: str = "image.png"
    data_base64: str = Field(min_length=1)


class CreateVideoTaskBody(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    model: str = "seedance_v2.0_mini"
    ratio: str = "1:1"
    duration: int = 5
    account_id: str | None = None
    mode: str = "t2v"
    images: list[ImageAttachmentBody] = Field(default_factory=list, max_length=9)


def _account_dict(account: Account, daily_quota: int = 5) -> dict:
    return {
        "id": account.id,
        "display_name": account.display_name,
        "nickname": account.doubao_nickname,
        "status": account.status,
        "enabled": account.enabled,
        "last_verified_at": account.last_verified_at.isoformat() if account.last_verified_at else None,
        "video_quota_used": account.video_quota_used,
        "video_quota_total": daily_quota,
        "video_quota_date": account.video_quota_date.isoformat() if account.video_quota_date else None,
        "video_limited_until": account.video_limited_until.isoformat() if account.video_limited_until else None,
    }


def _video_task_dict(task, daily_quota: int = 5) -> dict:
    account = task.account if task.account_id else None
    image_count = 0
    if getattr(task, "image_paths", None):
        try:
            image_count = len(json.loads(task.image_paths))
        except (TypeError, json.JSONDecodeError):
            image_count = 0
    return {
        "id": task.id,
        "account_id": account.id if account else None,
        "account_name": account.display_name if account else None,
        "quota_used": account.video_quota_used if account else None,
        "quota_total": daily_quota if account else None,
        "prompt": task.prompt,
        "model": task.model,
        "ratio": task.ratio,
        "duration": task.duration,
        "mode": getattr(task, "mode", None) or "t2v",
        "image_count": image_count,
        "status": task.status,
        "conversation_id": task.conversation_id,
        "remote_task_id": task.remote_task_id,
        "vid": task.vid,
        "result_url": task.result_url,
        "backup_result_url": task.backup_result_url,
        "fallback_result_url": task.fallback_result_url,
        "cover_url": task.cover_url,
        "error": task.error_message,
        "created_at": task.created_at.isoformat(),
        "updated_at": task.updated_at.isoformat(),
    }


def create_app(
    token: str,
    frontend_dir: Path,
    repository,
    login_service,
    video_service=None,
    settings_service=None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        if video_service is not None and hasattr(video_service, "resume_queued"):
            await video_service.resume_queued()
        yield
        if video_service is not None:
            await video_service.shutdown()
        await login_service.shutdown()

    app = FastAPI(title="DouPool", docs_url=None, redoc_url=None, lifespan=lifespan)
    frontend_dir = Path(frontend_dir)

    def authorize(x_doupool_token: str | None = Header(default=None)) -> None:
        if x_doupool_token is None or not secrets.compare_digest(x_doupool_token, token):
            raise HTTPException(status_code=401, detail="invalid local token")

    @app.get("/api/health")
    def health():
        return {"status": "ok"}

    @app.get("/api/accounts", dependencies=[])
    def accounts(x_doupool_token: str | None = Header(default=None)):
        authorize(x_doupool_token)
        quota = int(settings_service.get()["daily_quota"]) if settings_service else 5
        return [_account_dict(item, quota) for item in repository.list_accounts()]

    @app.post("/api/accounts/login-attempts", status_code=202)
    async def create_login(x_doupool_token: str | None = Header(default=None)):
        authorize(x_doupool_token)
        try:
            attempt = login_service.start()
        except LoginAlreadyRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": attempt.id, "state": attempt.state}

    @app.get("/api/login-attempts/{attempt_id}")
    def get_attempt(attempt_id: str, x_doupool_token: str | None = Header(default=None)):
        authorize(x_doupool_token)
        attempt = LoginAttempt.get_or_none(LoginAttempt.id == attempt_id)
        if not attempt:
            raise HTTPException(status_code=404, detail="login attempt not found")
        return {"id": attempt.id, "state": attempt.state, "error": attempt.error_message}

    @app.get("/api/login-attempts/{attempt_id}/events")
    async def login_events(attempt_id: str, access_token: str = Query(default="")):
        if not secrets.compare_digest(access_token, token):
            raise HTTPException(status_code=401, detail="invalid local token")

        async def stream():
            async for event in login_service.events(attempt_id):
                payload = json.dumps(event.__dict__ if hasattr(event, "__dict__") else {
                    "attempt_id": event.attempt_id, "state": event.state, "message": event.message
                }, ensure_ascii=False)
                yield f"event: login_state\ndata: {payload}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.patch("/api/accounts/{account_id}")
    def update_account(account_id: str, body: dict, x_doupool_token: str | None = Header(default=None)):
        authorize(x_doupool_token)
        account = Account.get_or_none(Account.id == account_id)
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        if "enabled" in body:
            account.enabled = bool(body["enabled"])
            account.status = "active" if account.enabled else "disabled"
            account.save()
        quota = int(settings_service.get()["daily_quota"]) if settings_service else 5
        return _account_dict(account, quota)

    @app.delete("/api/accounts/{account_id}", status_code=204)
    def delete_account(account_id: str, x_doupool_token: str | None = Header(default=None)):
        authorize(x_doupool_token)
        account = Account.get_or_none(Account.id == account_id)
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        if repository.has_active_tasks(account_id):
            raise HTTPException(status_code=409, detail="账号有正在运行的视频任务，暂时不能删除")
        profile_dir = Path(account.profile_dir)
        account.delete_instance(recursive=True)
        shutil.rmtree(profile_dir, ignore_errors=True)

    @app.get("/api/logs")
    def logs(x_doupool_token: str | None = Header(default=None)):
        authorize(x_doupool_token)
        if settings_service:
            repository.prune_logs(int(settings_service.get()["log_retention_days"]))
        return [{"id": row.id, "level": row.level, "module": row.module,
                 "event": row.event, "message": row.message,
                 "created_at": row.created_at.isoformat()} for row in repository.list_logs()]

    @app.delete("/api/logs", status_code=204)
    def clear_logs(x_doupool_token: str | None = Header(default=None)):
        authorize(x_doupool_token)
        repository.clear_logs()

    @app.get("/api/settings")
    def get_settings(x_doupool_token: str | None = Header(default=None)):
        authorize(x_doupool_token)
        if settings_service is None:
            raise HTTPException(status_code=503, detail="设置服务未启动")
        return settings_service.get()

    @app.put("/api/settings")
    def update_settings(body: dict, x_doupool_token: str | None = Header(default=None)):
        authorize(x_doupool_token)
        if settings_service is None:
            raise HTTPException(status_code=503, detail="设置服务未启动")
        try:
            updated = settings_service.update(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        set_log_level(updated["log_level"])
        return updated

    @app.post("/api/settings/backup", status_code=201)
    def backup_settings(x_doupool_token: str | None = Header(default=None)):
        authorize(x_doupool_token)
        if settings_service is None:
            raise HTTPException(status_code=503, detail="设置服务未启动")
        try:
            path = settings_service.backup()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"数据库备份失败：{exc}") from exc
        return {"path": str(path)}

    @app.get("/api/video-tasks")
    def video_tasks(x_doupool_token: str | None = Header(default=None)):
        authorize(x_doupool_token)
        quota = int(settings_service.get()["daily_quota"]) if settings_service else 5
        return [_video_task_dict(task, quota) for task in repository.list_video_tasks()]

    @app.post("/api/video-tasks", status_code=202)
    async def create_video_task(body: CreateVideoTaskBody, x_doupool_token: str | None = Header(default=None)):
        authorize(x_doupool_token)
        if video_service is None:
            raise HTTPException(status_code=503, detail="视频服务未启动")
        try:
            payload = body.model_dump()
            payload["images"] = [
                {"name": item["name"], "data_base64": item["data_base64"]}
                for item in payload.get("images") or []
            ]
            task = video_service.start(**payload)
        except (ValueError, NoAvailableAccount) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        quota = int(settings_service.get()["daily_quota"]) if settings_service else 5
        return _video_task_dict(task, quota)

    assets = frontend_dir / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", response_class=HTMLResponse)
    def spa(path: str):
        index = frontend_dir / "index.html"
        if not index.exists():
            return HTMLResponse("<h1>Frontend not built</h1>", status_code=503)
        html = index.read_text(encoding="utf-8")
        meta = f'<meta name="doupool-token" content="{token}">'
        if "</head>" in html:
            html = html.replace("</head>", f"{meta}</head>", 1)
        else:
            html = meta + html
        return HTMLResponse(html)

    return app
