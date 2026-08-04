from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shutil
import time
from contextlib import asynccontextmanager
from pathlib import Path

from playwright.sync_api import sync_playwright

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from doupool.db.models import Account, LoginAttempt
from doupool.login.service import LoginAlreadyRunning
from doupool.logging.setup import set_log_level
from doupool.updater import check_for_update
from doupool.video.browser import TokenBundleUnavailable, extract_webmssdk_tokens
from doupool.video.service import NoAvailableAccount


class ImageAttachmentBody(BaseModel):
    name: str = "image.png"
    data_base64: str = Field(min_length=1)


class CreateVideoTaskBody(BaseModel):
    prompt: str = ""
    prompts: list[str] = Field(default_factory=list, max_length=20)
    model: str = "seedance_v2.0_mini"
    ratio: str = "1:1"
    duration: int = 5
    account_id: str | None = None
    mode: str = "t2v"
    images: list[ImageAttachmentBody] = Field(default_factory=list, max_length=9)
    # v0.2.9:外部异步回执 URL。任务到 terminal 状态后服务 POST JSON
    # {task_id, status, result_url, ...}。必须是 http:// 或 https://,
    # 其它 scheme 静默忽略(避免 file:// / gopher://)。
    callback_url: str | None = None


class UpdateAccountBody(BaseModel):
    """PATCH /api/accounts/{id} body. 用 Pydantic 校验避免 bool('false') 等都被当成 True。"""
    enabled: bool | None = None


def _extract_bearer(authorization: str | None) -> str | None:
    """v0.2.9:从 Authorization 头里解析 Bearer token。

    只认大小写不敏感的 'Bearer ' 前缀(RFC 6750 §2.1),前后空白 trim
    后空字符串视为 None。Bearer 之外的 scheme(Basic / Digest / 其它自定义
    scheme)都不解,避免误把任意头当成 token 撞 hash。
    """
    if not authorization:
        return None
    parts = authorization.strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return None
    return parts[1].strip() or None


def _account_dict(account: Account, daily_quotas: dict[str, int] | None = None) -> dict:
    """v0.2.9:返回按 seedance 模型分桶的当日额度。旧 video_quota_used /
    video_quota_total 字段保留,alias 到 mini 桶,前端老代码 / 缓存兜底用。
    daily_quotas 缺省时(老 API 调用方)用 mini 默认 5。
    """
    quotas = daily_quotas or {"mini": 5, "v2": 5, "std": 5}
    return {
        "id": account.id,
        "display_name": account.display_name,
        "nickname": account.doubao_nickname,
        "status": account.status,
        "enabled": account.enabled,
        "last_verified_at": account.last_verified_at.isoformat() if account.last_verified_at else None,
        # 老字段:alias 到 mini 桶,前端缓存 / 老测试兜底
        "video_quota_used": account.video_quota_used_mini,
        "video_quota_total": quotas["mini"],
        # v0.2.9 新三桶
        "video_quota_used_mini": account.video_quota_used_mini,
        "video_quota_total_mini": quotas["mini"],
        "video_quota_used_v2": account.video_quota_used_v2,
        "video_quota_total_v2": quotas["v2"],
        "video_quota_used_std": account.video_quota_used_std,
        "video_quota_total_std": quotas["std"],
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
        "group_id": task.group_id,
        "group_index": task.group_index,
        "account_id": account.id if account else None,
        "account_name": account.display_name if account else None,
        "quota_used": account.video_quota_used if account else None,
        "quota_total": daily_quota if account else None,
        "prompt": task.prompt,
        "original_prompt": task.original_prompt or task.prompt,
        "prompt_retry_count": task.prompt_retry_count,
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
        "clean_video_url": task.clean_video_url,
        "clean_error": task.clean_error,
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
    current_version: str = "0.1.0",
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

    def authorize(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> None:
        """v0.2.9:同时接受 X-Doupool-Token(前端 legacy)和 Authorization: Bearer
        (对齐 yaonieyo 默认 key 的 curl / 外部集成风格)。

        优先级:Authorization: Bearer > X-Doupool-Token。两者都为 None 或都错误
        才 401。secrets.compare_digest 抗计时攻击。
        """
        candidate = _extract_bearer(authorization) or x_doupool_token
        if not candidate or not secrets.compare_digest(candidate, token):
            raise HTTPException(status_code=401, detail="invalid local token")

    @app.get("/api/health")
    def health():
        return {"status": "ok", "version": current_version}

    @app.get("/api/update-check")
    async def update_check(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize(x_doupool_token, authorization)
        info = await check_for_update(current_version)
        return {
            "current_version": info.current_version,
            "latest_version": info.latest_version,
            "has_update": info.has_update,
            "release_url": info.release_url,
            "release_notes": info.release_notes,
            "asset_urls": info.asset_urls,
        }

    @app.get("/api/accounts", dependencies=[])
    def accounts(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize(x_doupool_token, authorization)
        quotas = settings_service.get_daily_quotas() if settings_service else {"mini": 5, "v2": 5, "std": 5}
        return [_account_dict(item, quotas) for item in repository.list_accounts()]

    @app.post("/api/accounts/login-attempts", status_code=202)
    async def create_login(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize(x_doupool_token, authorization)
        try:
            attempt = login_service.start()
        except LoginAlreadyRunning as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"id": attempt.id, "state": attempt.state}

    @app.get("/api/login-attempts/{attempt_id}")
    def get_attempt(
        attempt_id: str,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize(x_doupool_token, authorization)
        attempt = LoginAttempt.get_or_none(LoginAttempt.id == attempt_id)
        if not attempt:
            raise HTTPException(status_code=404, detail="login attempt not found")
        return {"id": attempt.id, "state": attempt.state, "error": attempt.error_message}

    @app.get("/api/login-attempts/{attempt_id}/events")
    async def login_events(
        attempt_id: str,
        access_token: str = Query(default=""),
        authorization: str | None = Header(default=None),
    ):
        # v0.2.9:SSE 同时接受 ?access_token= 旧约定(浏览器 EventSource 不能
        # 自定义 Header)和 Authorization: Bearer 新约定(curl / 集成友好)。
        candidate = _extract_bearer(authorization) or access_token
        if not candidate or not secrets.compare_digest(candidate, token):
            raise HTTPException(status_code=401, detail="invalid local token")

        async def stream():
            async for event in login_service.events(attempt_id):
                payload = json.dumps(event.__dict__ if hasattr(event, "__dict__") else {
                    "attempt_id": event.attempt_id, "state": event.state, "message": event.message
                }, ensure_ascii=False)
                yield f"event: login_state\ndata: {payload}\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream")

    @app.patch("/api/accounts/{account_id}")
    def update_account(
        account_id: str,
        body: UpdateAccountBody,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize(x_doupool_token, authorization)
        account = Account.get_or_none(Account.id == account_id)
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        if body.enabled is not None:
            account.enabled = body.enabled
            account.status = "active" if account.enabled else "disabled"
            account.save()
        quotas = settings_service.get_daily_quotas() if settings_service else {"mini": 5, "v2": 5, "std": 5}
        return _account_dict(account, quotas)

    @app.delete("/api/accounts/{account_id}", status_code=204)
    def delete_account(
        account_id: str,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize(x_doupool_token, authorization)
        account = Account.get_or_none(Account.id == account_id)
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        if repository.has_active_tasks(account_id):
            raise HTTPException(status_code=409, detail="账号有正在运行的视频任务，暂时不能删除")
        profile_dir = Path(account.profile_dir)
        account.delete_instance(recursive=True)
        shutil.rmtree(profile_dir, ignore_errors=True)

    @app.get("/api/logs")
    def logs(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize(x_doupool_token, authorization)
        if settings_service:
            repository.prune_logs(int(settings_service.get()["log_retention_days"]))
        return [{"id": row.id, "level": row.level, "module": row.module,
                 "event": row.event, "message": row.message,
                 "created_at": row.created_at.isoformat()} for row in repository.list_logs()]

    @app.delete("/api/logs", status_code=204)
    def clear_logs(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize(x_doupool_token, authorization)
        repository.clear_logs()

    @app.get("/api/settings")
    def get_settings(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize(x_doupool_token, authorization)
        if settings_service is None:
            raise HTTPException(status_code=503, detail="设置服务未启动")
        return settings_service.get()

    @app.put("/api/settings")
    def update_settings(
        body: dict,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize(x_doupool_token, authorization)
        if settings_service is None:
            raise HTTPException(status_code=503, detail="设置服务未启动")
        try:
            updated = settings_service.update(body)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        set_log_level(updated["log_level"])
        return updated

    @app.post("/api/settings/backup", status_code=201)
    def backup_settings(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize(x_doupool_token, authorization)
        if settings_service is None:
            raise HTTPException(status_code=503, detail="设置服务未启动")
        try:
            path = settings_service.backup()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"数据库备份失败：{exc}") from exc
        return {"path": str(path)}

    @app.get("/api/video-tasks")
    def video_tasks(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize(x_doupool_token, authorization)
        quotas = settings_service.get_daily_quotas() if settings_service else {"mini": 5, "v2": 5, "std": 5}
        return [_video_task_dict(task, quotas["mini"]) for task in repository.list_video_tasks()]

    @app.get("/api/video-task-groups")
    def video_task_groups(
        limit: int = 50,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """按 group_id 聚合返回最近的任务组(每个组首条 + 任务数)"""
        authorize(x_doupool_token, authorization)
        return repository.list_task_groups(limit=limit)

    @app.get("/api/video-task-groups/{group_id}")
    def video_task_group_detail(
        group_id: str,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """返回某 group 下所有任务,按 group_index 排序"""
        authorize(x_doupool_token, authorization)
        quotas = settings_service.get_daily_quotas() if settings_service else {"mini": 5, "v2": 5, "std": 5}
        return [_video_task_dict(t, quotas["mini"]) for t in repository.list_tasks_by_group(group_id)]

    @app.post("/api/video-tasks", status_code=202)
    async def create_video_task(
        body: CreateVideoTaskBody,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize(x_doupool_token, authorization)
        if video_service is None:
            raise HTTPException(status_code=503, detail="视频服务未启动")
        try:
            payload = body.model_dump()
            payload["images"] = [
                {"name": item["name"], "data_base64": item["data_base64"]}
                for item in payload.get("images") or []
            ]
            # v0.2.9:callback_url 直接透传给 service,service 负责入库
            # 与异步派发;这里不做 scheme 校验(让 dispatcher 留痕 callback_status=
            # 'failed' 即可,422 拒绝会让前端拿不到任务 ID,反而难排查)。
            task = video_service.start(**payload)
        except (ValueError, NoAvailableAccount) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        quotas = settings_service.get_daily_quotas() if settings_service else {"mini": 5, "v2": 5, "std": 5}
        return _video_task_dict(task, quotas["mini"])

    @app.post("/api/requests/{task_id}/retry-result", status_code=202)
    async def retry_result(
        task_id: str,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """v0.2.9:只重解析不重提交,不扣额度。

        yaonieyo 风格路径 /api/requests/:id/retry-result —— 外部脚本
        (n8n / Airflow / 各种 batch runner)用同一个 task_id 轮询结果时,
        卡住或下载链接失效就 POST 这个端点让服务再查一次远端 chain。

        路径风格刻意保持 /api/requests/<task_id>/...,和 /api/video-tasks
        并存而不是合并 —— 这样老集成(直接读 /api/video-tasks)不动,
        新集成的"用 task_id 跟踪单个任务"心智模型也清晰。

        状态码:
          - 202:已接收,后台开始重解析(返回当前 task 字典,前端可继续轮询)
          - 404:task_id 不存在
          - 409:缺少 conversation_id / 原账号不可用 / 已有 retry 在跑
          - 503:video_service 未启动
        """
        authorize(x_doupool_token, authorization)
        if video_service is None:
            raise HTTPException(status_code=503, detail="视频服务未启动")
        try:
            task = await video_service.schedule_retry_result(task_id)
        except ValueError as exc:
            msg = str(exc)
            if "任务不存在" in msg:
                raise HTTPException(status_code=404, detail=msg) from exc
            raise HTTPException(status_code=409, detail=msg) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        quotas = settings_service.get_daily_quotas() if settings_service else {"mini": 5, "v2": 5, "std": 5}
        return _video_task_dict(task, quotas["mini"])

    @app.delete("/api/requests/{task_id}", status_code=204)
    def delete_video_task(
        task_id: str,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """v0.2.11:删除一条视频任务。

        状态码:
          - 204:成功删除
          - 404:task_id 不存在
          - 409:任务正在生成中(状态 starting / generating / resolving),不能删
          - 503:video_service 未启动
        """
        authorize(x_doupool_token, authorization)
        if video_service is None:
            raise HTTPException(status_code=503, detail="视频服务未启动")
        try:
            video_service.delete(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    def _token_bundle_dict(bundle, available: bool, hint: str = "") -> dict:
        """v0.2.17:把 TokenBundle 序列化成 API 响应。available=False(抽不到 web_id)
        时 msToken / web_id / device_id 等值仍返回(用于 UI 调试看是哪个字段缺失),
        统一在 hint 里说明原因。
        """
        return {
            "available": available,
            "hint": hint,
            "ms_token_preview": (bundle.ms_token[:12] + "...") if bundle.ms_token else "",
            "web_id": bundle.web_id,
            "web_id_signature": bundle.web_id_signature,
            "device_id": bundle.device_id,
            "tea_uuid": bundle.tea_uuid,
            "pc_version": bundle.pc_version,
            "fetched_at": bundle.fetched_at,
            "age_seconds": bundle.age_seconds(),
        }

    @app.get("/api/accounts/{account_id}/webmssdk-tokens")
    def get_webmssdk_tokens(
        account_id: str,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """v0.2.17:从账号 profile 抽当前 WebMSSDK / TeaSDK token bundle。

        不启动浏览器,只读 Default/Cookies SQLite + Local Storage leveldb。
        字段缺失(典型:刚 login 没让主页跑过 WebMSSDK)→ 200 但 available=False
        + hint 引导用户去浏览器访问主页后再调一次。
        """
        authorize(x_doupool_token, authorization)
        account = Account.get_or_none(Account.id == account_id)
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        profile_dir = Path(account.profile_dir)
        try:
            bundle = extract_webmssdk_tokens(profile_dir)
        except TokenBundleUnavailable as exc:
            # 返回 200 + available=False,UI 区分"账号不存在"vs"token 抽不到"
            return {
                "available": False,
                "hint": str(exc),
                "ms_token_preview": "",
                "web_id": "",
                "web_id_signature": "",
                "device_id": "",
                "tea_uuid": "",
                "pc_version": "",
                "fetched_at": 0.0,
                "age_seconds": None,
            }
        return _token_bundle_dict(bundle, available=True)

    @app.post("/api/accounts/{account_id}/refresh-tokens", status_code=202)
    async def refresh_webmssdk_tokens(
        account_id: str,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """v0.2.17:启动 headless Playwright 访问 doubao.com/chat/ 让 WebMSSDK
        重新跑一次(产生新 msToken),然后再 extract_webmssdk_tokens 读新 bundle。

        msToken 过期时用户点 UI 「刷新 token」按钮触发。同步跑 Playwright 会
        阻塞 FastAPI 事件循环 ~5-10 秒,所以走 asyncio.to_thread 抛到 threadpool。
        """
        authorize(x_doupool_token, authorization)
        account = Account.get_or_none(Account.id == account_id)
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        profile_dir = Path(account.profile_dir)

        def _refresh():
            # headless=False 是必须的:WebMSSDK 会拒绝 headless 拉新 token。
            # 但 refresh 端点不需要可视化 — 浏览器窗口弹个 1s 就关,不打扰用户。
            t0 = time.monotonic()
            with sync_playwright() as pw:
                ctx = pw.chromium.launch_persistent_context(
                    str(profile_dir),
                    headless=False,
                    args=[
                        "--window-position=-2400,-2400",
                        "--window-size=900,650",
                    ],
                )
                try:
                    page = ctx.pages[0] if ctx.pages else ctx.new_page()
                    page.goto(
                        "https://www.doubao.com/chat/",
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                    # WebMSSDK 跑 msToken 缓存通常 3-6 秒,留 8 秒 buffer
                    page.wait_for_timeout(8_000)
                finally:
                    try:
                        ctx.close()
                    except Exception:
                        pass
            bundle = extract_webmssdk_tokens(profile_dir)
            return bundle, time.monotonic() - t0

        try:
            bundle, elapsed = await asyncio.to_thread(_refresh)
        except TokenBundleUnavailable as exc:
            # 主页访问了但 leveldb 还是没 web_id —— 罕见,可能是 disk flush
            # 延迟。返回 200 + available=False 让前端显示 hint。
            logging.getLogger("doupool.api").warning(
                "刷新 token 后仍抽不到 web_id: account=%s err=%s", account_id, exc
            )
            return {
                "available": False,
                "hint": str(exc),
                "ms_token_preview": "",
                "web_id": "",
                "web_id_signature": "",
                "device_id": "",
                "tea_uuid": "",
                "pc_version": "",
                "fetched_at": 0.0,
                "age_seconds": None,
            }
        except Exception as exc:
            # Playwright 启动失败 / Chromium 没装 / profile lock 占用
            logging.getLogger("doupool.api").exception(
                "刷新 token 失败: account=%s", account_id
            )
            raise HTTPException(status_code=503, detail=f"刷新 token 失败:{exc}") from exc
        return {
            **_token_bundle_dict(bundle, available=True),
            "hint": f"刷新成功,耗时 {elapsed:.1f}s",
        }

    assets = frontend_dir / "assets"
    if assets.exists():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{path:path}", response_class=HTMLResponse)
    def spa(path: str):
        index = frontend_dir / "index.html"
        if not index.exists():
            return HTMLResponse("<h1>Frontend not built</h1>", status_code=503)
        html = index.read_text(encoding="utf-8")
        # 把 token 通过 <script> 注入到 window 全局,不再写到 <meta> 里。
        # meta 内容会留在任何静态 HTML 缓存里;script 注入只对本次响应有效,
        # 前端从 window.__DOUPOOL_TOKEN__ 同步读取。
        injection = (
            f'<script>window.__DOUPOOL_TOKEN__ = {token!r};</script>'
        )
        if "</head>" in html:
            html = html.replace("</head>", f"{injection}</head>", 1)
        else:
            html = injection + html
        return HTMLResponse(html)

    return app
