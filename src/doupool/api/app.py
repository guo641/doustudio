from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import secrets
import shutil
import threading
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Callable

import httpx
from playwright.sync_api import sync_playwright

from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator

from doupool.db.models import Account, LoginAttempt, utcnow
from doupool.login.browser_sessions import (
    BrowserAlreadyOpen,
    BrowserSession,
    get_browser_sessions_registry,
)
from doupool.login.service import LoginAlreadyRunning
from doupool.logging.setup import set_log_level
from doupool.settings.service import DownloadDirPickerUnavailable, open_directory
from doupool.updater import check_for_update
from doupool.video.browser import TokenBundleUnavailable, extract_webmssdk_tokens
from doupool.video.protocol import FIXED_VIDEO_DURATION_SECONDS
from doupool.video.service import NoAvailableAccount, quota_window


class ImageAttachmentBody(BaseModel):
    name: str = "image.png"
    data_base64: str = Field(min_length=1)


class CreateVideoTaskBody(BaseModel):
    # v0.2.37.3:画面描述上限 2000→5000 字。用户反馈 2000 太短,复杂场景
    # 描述常常被截断。`prompts` 列表中每个元素也按 5000 字封顶(单段 prompt)。
    prompt: str = Field(default="", max_length=5000)
    prompts: list[str] = Field(default_factory=list, max_length=20)
    model: str = "seedance_v2.0_mini"
    ratio: str = "1:1"
    duration: int = FIXED_VIDEO_DURATION_SECONDS
    account_id: str | None = None
    mode: str = "t2v"
    images: list[ImageAttachmentBody] = Field(default_factory=list, max_length=9)
    # v0.2.9:外部异步回执 URL。任务到 terminal 状态后服务 POST JSON
    # {task_id, status, result_url, ...}。必须是 http:// 或 https://,
    # 其它 scheme 静默忽略(避免 file:// / gopher://)。
    callback_url: str | None = None
    # v0.2.32:手动重试失败任务时继承原 group_id,确保结果页按组聚合时
    # 不会漏掉这条新任务。只在手动重试路径传,普通新建留 None 让
    # service 端按 prompt 数量决定是否打组。
    group_id: str | None = None
    # v0.3.8:用户可为本次提交指定组名;service 负责去空白并在需要时建组。
    group_name: str | None = Field(default=None, max_length=40)

    @field_validator("duration", mode="before")
    @classmethod
    def _normalize_duration(cls, _value) -> int:
        return FIXED_VIDEO_DURATION_SECONDS

    @model_validator(mode="after")
    def _t2v_only(self):
        if self.mode != "t2v" or self.images:
            raise ValueError("当前版本仅支持文生视频")
        return self

    # v0.2.37.3:`prompts` 列表中每个元素也按 5000 字封顶(单段 prompt),跟
    # 上面的 `prompt` 单值一致。`max_length=20` 是段数上限,这里再加单段字符
    # 上限,避免有人写 5 万字一段触发模型限流。
    @field_validator("prompts")
    @classmethod
    def _prompts_max_length(cls, value: list[str]) -> list[str]:
        for idx, item in enumerate(value):
            if len(item) > 5000:
                raise ValueError(f"prompts[{idx}] exceeds 5000 characters")
        return value


class UpdateAccountBody(BaseModel):
    """PATCH /api/accounts/{id} body. 用 Pydantic 校验避免 bool('false') 等都被当成 True。"""
    enabled: bool | None = None


class GroupDownloadBody(BaseModel):
    """v0.2.28 Q2:把 group_id 下所有 succeeded 任务视频流式下载到本地
    settings.download_dir/<batch_folder>/。"""
    group_id: str = Field(min_length=1)


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
    """v0.2.29:共享额度池 —— 主字段 video_quota_used_shared/total_shared。

    旧字段 video_quota_used_mini/v2/std 保留(只读,镜像到 shared 让老前端
    缓存兜底);video_quota_used legacy alias 到 shared。
    daily_quotas 缺省时(老 API 调用方)用 shared 默认 50。
    """
    quotas = daily_quotas or {"shared": 50}
    shared_used = account.video_quota_used_shared or 0
    shared_total = int(quotas["shared"])
    return {
        "id": account.id,
        "display_name": account.display_name,
        "nickname": account.doubao_nickname,
        "status": account.status,
        "enabled": account.enabled,
        "last_verified_at": account.last_verified_at.isoformat() if account.last_verified_at else None,
        # v0.2.29 共享池主字段
        "video_quota_used_shared": shared_used,
        "video_quota_total_shared": shared_total,
        # 老字段 alias 到 shared,前端缓存 / 老测试兜底
        "video_quota_used": shared_used,
        "video_quota_total": shared_total,
        # v0.2.9 旧三桶保留(只读,镜像到 shared)。前端 v0.2.29 起不再读这些。
        "video_quota_used_mini": shared_used,
        "video_quota_total_mini": shared_total,
        "video_quota_used_v2": shared_used,
        "video_quota_total_v2": shared_total,
        "video_quota_used_std": shared_used,
        "video_quota_total_std": shared_total,
        "video_quota_date": account.video_quota_date.isoformat() if account.video_quota_date else None,
        "video_limited_until": account.video_limited_until.isoformat() if account.video_limited_until else None,
    }


def _sanitize_filename_part(text: str, max_len: int = 12) -> str:
    """v0.2.35:把 prompt 截前 12 字符 + 清洗 Windows/Unix 非法字符。

    Windows 不允许 \\ / : * ? " < > | 共 9 个,加控制字符(< 0x20)。
    把不可用字符替换成下划线,前后空白裁掉,空字符串兜底为 "video"。
    """
    if not text:
        return "video"
    cleaned = []
    for ch in text:
        if ch.isprintable() and ch not in '\\/:*?"<>|\r\n\t':
            cleaned.append(ch)
        else:
            cleaned.append("_")
    result = "".join(cleaned).strip().strip(".")[:max_len]
    return result or "video"


def _build_download_filename(task) -> str:
    """v0.2.35 + v0.3.3:批量下载命名 —— `{group_index:02d}_{HHMMSS}_{prompt前12字符}_{task_id短哈希}[-clean].mp4`。

    单条任务(group_index=0)同样落到 01:统一格式方便排序。
    -clean 后缀优先于重名去重 N(无水印版本始终命名为 -clean.mp4,
    原画重名才加 -2/-3)。

    v0.3.3 加 task_id 短哈希(8 字符,SHA1):避免同 group_index + 同秒提交
    时文件名撞车 —— group_download 写到同一目录,后者覆盖前者,用户只看到
    1 个文件,误以为「两条都拿到同一个视频」。这是 v0.3.3 单账号多任务并发
    修复的次生防线:race 防御管 DB 写入,文件名 hash 管下载到本地后的可
    区分性,两道关一起兜底。
    """
    group_index = getattr(task, "group_index", 0) or 0
    index_str = f"{group_index:02d}"
    ts = task.created_at.strftime("%H%M%S")
    name_part = _sanitize_filename_part(task.prompt, max_len=12)
    task_id = getattr(task, "id", None) or 0
    id_hash = hashlib.sha1(str(task_id).encode("utf-8", errors="replace")).hexdigest()[:8]
    stem = f"{index_str}_{ts}_{name_part}_{id_hash}"
    if getattr(task, "clean_video_url", None):
        stem = f"{stem}-clean"
    return f"{stem}.mp4"


def _video_task_dict(task, daily_quota: int = 50) -> dict:
    account = task.account if task.account_id else None
    image_count = 0
    if getattr(task, "image_paths", None):
        try:
            image_count = len(json.loads(task.image_paths))
        except (TypeError, json.JSONDecodeError):
            image_count = 0
    # v0.2.29:共享池下 quota_used/total 直接读 shared 桶(老 video_quota_used
    # legacy alias 已被 _account_dict 重定向,前端读 task.quota_used 也是 shared)。
    quota_used = account.video_quota_used_shared if account else None
    return {
        "id": task.id,
        "group_id": task.group_id,
        "group_name": getattr(task, "group_name", None),
        "group_index": task.group_index,
        "account_id": account.id if account else None,
        "account_name": account.display_name if account else None,
        "quota_used": quota_used,
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
        # v0.2.35:批量下载命名 —— 单条下载建议文件名,与 group_download 共用
        "download_filename": _build_download_filename(task),
    }


def create_app(
    token: str,
    frontend_dir: Path,
    repository,
    login_service,
    video_service=None,
    settings_service=None,
    current_version: str = "0.1.0",
    download_dir_picker: Callable[[str], str | None] | None = None,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app):
        # v0.2.29:启动时一次性把旧 mini/v2/std 累加进 shared 桶(幂等)。
        try:
            migrated = repository.migrate_legacy_quota_buckets()
            if migrated:
                logging.getLogger("doupool.api").info(
                    "v0.2.29:已迁移 %d 个账号的旧额度到共享池", migrated,
                    extra={"event": "quota_migration", "migrated": migrated},
                )
        except Exception as exc:
            logging.getLogger("doupool.api").warning(
                "v0.2.29:quota 迁移失败(非致命,继续): %s", exc,
                extra={"event": "quota_migration_failed"},
            )
        if video_service is not None and hasattr(video_service, "resume_queued"):
            await video_service.resume_queued()
        # v0.2.29:启动独立重置 cron —— 到 quota_reset_time 跨日清桶,
        # 不依赖任务在跑。
        if video_service is not None and hasattr(video_service, "start_reset_cron"):
            video_service.start_reset_cron()
        yield
        # v0.2.20:app 退出时关掉所有「📂 打开浏览器」留下的窗口,避免
        # Chromium 进程游离在系统里。
        get_browser_sessions_registry().shutdown()
        if video_service is not None:
            await video_service.shutdown()
        # license verifier 单测里 create_app(..., login_service=None, ...),
        # 退出时不能因为 None 调用 shutdown 而崩 —— 测试 fixture 跟生产路径都共用 lifespan。
        if login_service is not None:
            await login_service.shutdown()

    app = FastAPI(title="DouPool", docs_url=None, redoc_url=None, lifespan=lifespan)
    frontend_dir = Path(frontend_dir)
    # Native file dialogs are modal and only one may be open at a time.  The
    # API runs in worker threads, so serialize calls while leaving the rest of
    # the app concurrent.
    _pick_lock = threading.Lock()

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

    def authorize_with_license(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ) -> None:
        """v0.3.0:本地 token 校验 + 离线激活闸门。失败 401 / 403,前端走
        ActivationDialog 而不是直接报错。

        闸门状态:
            'valid'        → 通过
            'missing'      → 403(前端显示激活窗)
            'expired'      → 403(前端显示「已过期」窗)
            'uncompiled'   → 通过(开发机 / 测试场景;生产应走 .pyd)

        为什么不强制 'uncompiled' 拒绝:开发者在 macOS / Linux 上跑测试,
        _license_verify.pyd 没编但仍要能调 API。生产 PyInstaller onedir
        打包一定会带 .pyd,这条 fallback 永远不命中。
        """
        authorize(x_doupool_token, authorization)
        # 内部 import 避免模块加载顺序敏感
        from doupool.license import get_activation_status
        status = get_activation_status()
        if status == "missing":
            raise HTTPException(status_code=403, detail={"error": "license_missing"})
        if status == "expired":
            raise HTTPException(status_code=403, detail={"error": "license_expired"})

    @app.get("/api/health")
    def health():
        # v0.3.0:加 activated 字段,前端 health-check 时能立刻判定是否走激活窗
        from doupool.license import get_activation_status
        status = get_activation_status()
        activated = status == "valid"
        return {
            "status": "ok" if activated else "degraded",
            "version": current_version,
            "activated": activated,
            "license_status": status,
        }

    @app.get("/api/license/status")
    def license_status():
        """无授权检查 —— 前端在未激活时也要能调。

        Returns:
            {status: 'valid'|'expired'|'missing'|'uncompiled',
             fingerprint: str, customer: str, expires_at: int|null}
        """
        from doupool.license import current_fingerprint, get_activation_status
        from doupool.license.storage import read_token
        status = get_activation_status()
        fingerprint = current_fingerprint()
        customer = ""
        expires_at = 0
        if status == "valid":
            blob = read_token()
            if blob:
                # 解码 payload 取 customer / expires_at(轻量,verifier 已有
                # decode 路径,这里复用)
                try:
                    from doupool.license import _license_verify as _v
                    if _v is not None:
                        _, payload, _ = _v.verify_token(blob)
                        if payload:
                            customer = str(payload.get("customer", ""))
                            expires_at = int(payload.get("expires_at", 0))
                except Exception:
                    pass
        return {
            "status": status,
            "fingerprint": fingerprint,
            "customer": customer,
            "expires_at": expires_at if expires_at > 0 else None,
        }

    @app.post("/api/license/activate")
    def license_activate(body: dict):
        """无授权检查 —— 用户的「首次激活」路径。

        Body: {code: str}
        Returns: {ok: True} 或 400 + 中文错误
        """
        code = str(body.get("code", "")).strip()
        if not code:
            raise HTTPException(status_code=400, detail="激活码不能为空")
        from doupool.license import activate as _activate
        success, err = _activate(code)
        if not success:
            raise HTTPException(status_code=400, detail=err)
        return {"ok": True}

    @app.post("/api/license/quit")
    def license_quit():
        """强杀进程 —— webview 窗口随之关闭,用户在激活窗点「退出」触发。

        不用 raise SystemExit —— uvicorn 还会捕获,导致 webview 窗留
        着。直接 os._exit(0) 跳过所有 hook,关得干净。
        """
        import os as _os
        # 起个 200ms 计时器让响应先发出去
        import threading
        def _kill():
            _os._exit(0)
        threading.Timer(0.2, _kill).start()
        return {"ok": True}

    @app.get("/api/update-check")
    async def update_check(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize_with_license(x_doupool_token, authorization)
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
        authorize_with_license(x_doupool_token, authorization)
        quotas = settings_service.get_daily_quotas() if settings_service else {"shared": 50}
        return [_account_dict(item, quotas) for item in repository.list_accounts()]

    @app.post("/api/accounts/login-attempts", status_code=202)
    async def create_login(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize_with_license(x_doupool_token, authorization)
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
        authorize_with_license(x_doupool_token, authorization)
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
        authorize_with_license(x_doupool_token, authorization)
        account = Account.get_or_none(Account.id == account_id)
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        if body.enabled is not None:
            account.enabled = body.enabled
            account.status = "active" if account.enabled else "disabled"
            account.save()
        quotas = settings_service.get_daily_quotas() if settings_service else {"shared": 50}
        return _account_dict(account, quotas)

    @app.delete("/api/accounts/{account_id}", status_code=204)
    def delete_account(
        account_id: str,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize_with_license(x_doupool_token, authorization)
        account = Account.get_or_none(Account.id == account_id)
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        if repository.has_active_tasks(account_id):
            raise HTTPException(status_code=409, detail="账号有正在运行的视频任务，暂时不能删除")
        profile_dir = Path(account.profile_dir)
        account.delete_instance(recursive=True)
        shutil.rmtree(profile_dir, ignore_errors=True)

    # v0.2.29:手动重置额度端点 —— 兜底,防止软件卡住时无解。
    # 单账号 + 一键全部。两个端点都返回 reset_count + reset_at。
    @app.post("/api/accounts/{account_id}/reset-quota")
    def reset_account_quota_endpoint(
        account_id: str,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """v0.2.29:清单个账号的 shared 桶 + limited_until + video_quota_date。

        账号不存在返 404,清成功返 {reset_count:1, reset_at}。幂等。
        """
        authorize_with_license(x_doupool_token, authorization)
        # 用 quota_window 算出业务日,跟 reset_daily_quotas 口径一致(避免
        # quota_reset_time > now 时业务日跨天的 corner case)。
        reset_value = settings_service.get().get("quota_reset_time", "00:00") if settings_service else "00:00"
        business_date, _ = quota_window(utcnow(), reset_value)
        ok = repository.reset_account_quota(account_id, business_date)
        if not ok:
            raise HTTPException(status_code=404, detail="account not found")
        return {
            "reset_count": 1,
            "reset_at": utcnow().isoformat(),
            "account_id": account_id,
        }

    @app.post("/api/accounts/reset-all-quota")
    def reset_all_quota_endpoint(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """v0.2.29:一键清所有 enabled 账号的 shared 桶 + limited_until。

        disabled 账号不动(用户显式关掉的不要自动清)。
        """
        authorize_with_license(x_doupool_token, authorization)
        reset_value = settings_service.get().get("quota_reset_time", "00:00") if settings_service else "00:00"
        business_date, _ = quota_window(utcnow(), reset_value)
        reset_count = repository.reset_all_quotas(business_date)
        return {
            "reset_count": reset_count,
            "reset_at": utcnow().isoformat(),
        }

    @app.get("/api/logs")
    def logs(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize_with_license(x_doupool_token, authorization)
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
        authorize_with_license(x_doupool_token, authorization)
        repository.clear_logs()

    @app.get("/api/settings")
    def get_settings(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize_with_license(x_doupool_token, authorization)
        if settings_service is None:
            raise HTTPException(status_code=503, detail="设置服务未启动")
        return settings_service.get()

    @app.put("/api/settings")
    def update_settings(
        body: dict,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize_with_license(x_doupool_token, authorization)
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
        authorize_with_license(x_doupool_token, authorization)
        if settings_service is None:
            raise HTTPException(status_code=503, detail="设置服务未启动")
        try:
            path = settings_service.backup()
        except OSError as exc:
            raise HTTPException(status_code=500, detail=f"数据库备份失败：{exc}") from exc
        return {"path": str(path)}

    @app.post("/api/settings/pick-download-dir")
    def pick_download_dir(
        body: dict | None = None,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """弹出系统目录选择器并返回用户选中的路径。

        目录选择本身不写设置；前端把返回路径填入表单，用户点击保存后才
        通过 ``PUT /api/settings`` 持久化。取消对话框返回 ``{"path": null}``。
        """
        authorize_with_license(x_doupool_token, authorization)
        if settings_service is None:
            raise HTTPException(status_code=503, detail="设置服务未启动")
        payload = body or {}
        start = str(payload.get("start_dir", "") or "").strip()
        if not start:
            start = str(settings_service.get().get("download_dir", "") or "")
        if download_dir_picker is None:
            raise HTTPException(status_code=503, detail="桌面窗口未就绪,无法打开目录选择器")
        if not _pick_lock.acquire(blocking=False):
            raise HTTPException(status_code=409, detail="目录选择器已在使用中")
        try:
            path = download_dir_picker(start)
        except DownloadDirPickerUnavailable as exc:
            raise HTTPException(status_code=503, detail="桌面窗口未就绪,无法打开目录选择器") from exc
        except HTTPException:
            raise
        except Exception as exc:  # native helpers are optional desktop integrations
            logging.getLogger("doupool.api").warning(
                "目录选择器调用失败: %s", exc,
                extra={"event": "settings_pick_download_dir_failed"},
            )
            path = None
        finally:
            _pick_lock.release()
        return {"path": path}

    @app.post("/api/settings/open-dir")
    def open_download_dir(
        body: dict,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """在系统文件管理器中打开下载目录（不修改设置）。"""
        authorize_with_license(x_doupool_token, authorization)
        path = str(body.get("path", "") or "").strip()
        if not path:
            return {"ok": False}
        try:
            opened = open_directory(path)
        except Exception as exc:  # optional desktop integration must be non-fatal
            logging.getLogger("doupool.api").warning(
                "打开下载目录失败: %s", exc,
                extra={"event": "settings_open_download_dir_failed"},
            )
            opened = False
        return {"ok": bool(opened)}

    @app.get("/api/video-tasks")
    def video_tasks(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize_with_license(x_doupool_token, authorization)
        quotas = settings_service.get_daily_quotas() if settings_service else {"shared": 50}
        return [_video_task_dict(task, quotas["shared"]) for task in repository.list_video_tasks()]

    @app.get("/api/video-task-groups")
    def video_task_groups(
        limit: int = 50,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """按 group_id 聚合返回最近的任务组(每个组首条 + 任务数)"""
        authorize_with_license(x_doupool_token, authorization)
        return repository.list_task_groups(limit=limit)

    @app.get("/api/video-task-groups/{group_id}")
    def video_task_group_detail(
        group_id: str,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """返回某 group 下所有任务,按 group_index 排序"""
        authorize_with_license(x_doupool_token, authorization)
        quotas = settings_service.get_daily_quotas() if settings_service else {"shared": 50}
        return [_video_task_dict(t, quotas["shared"]) for t in repository.list_tasks_by_group(group_id)]

    @app.post("/api/video-tasks", status_code=200)
    async def create_video_task(
        body: CreateVideoTaskBody,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize_with_license(x_doupool_token, authorization)
        if video_service is None:
            raise HTTPException(status_code=503, detail="视频服务未启动")
        try:
            payload = body.model_dump()
            # schema 已规整；路由再守一次，避免后续模型改动绕过固定时长。
            payload["duration"] = FIXED_VIDEO_DURATION_SECONDS
            payload["images"] = [
                {"name": item["name"], "data_base64": item["data_base64"]}
                for item in payload.get("images") or []
            ]
            # v0.2.32:body.group_id 是手动重试路径透传的组归属,
            # 默认 None 表示新建任务,由 service 按 prompt 数量自决。
            # v0.2.9:callback_url 直接透传给 service,service 负责入库
            # 与异步派发;这里不做 scheme 校验(让 dispatcher 留痕 callback_status=
            # 'failed' 即可,422 拒绝会让前端拿不到任务 ID,反而难排查)。
            # v0.2.35:跨账号凑余额 —— start() 改返回 (first_task, partial_rejected)
            # 二元组;200 OK 响应 + partial_rejected 给前端 Toast,不再是 202。
            task, partial_rejected = video_service.start(**payload)
        except (ValueError, NoAvailableAccount) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        quotas = settings_service.get_daily_quotas() if settings_service else {"shared": 50}
        # v0.2.35:把 task + partial_rejected 包成 dict 返回,
        # partial_rejected 给前端 Toast 提示「这几条 prompt 暂时排不进」。
        return {
            "task": _video_task_dict(task, quotas["shared"]),
            "partial_rejected": partial_rejected,
        }

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
        authorize_with_license(x_doupool_token, authorization)
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
        quotas = settings_service.get_daily_quotas() if settings_service else {"shared": 50}
        return _video_task_dict(task, quotas["shared"])

    @app.post("/api/results/{task_id}/refresh-url")
    async def refresh_result_url(
        task_id: str,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """v0.2.22 Q4:重解析 succeeded 任务的 result_url 链。

        背景:task.result_url 是豆包签名 CDN,TTL 几分钟-几小时,几天后
        Edge 直接「无法访问此页面 ERR_INVALID_RESPONSE」。前端 DownloadButton
        三层 fallback (cors / no-cors / window.open) 全失败时调这个端点,
        拿到 fresh URL 后再触发下载。

        同步语义:POST 等待(最长 60s)拿到最新 result_url 才返回,前端拿到
        响应里的 result_url 立即重试下载。和 retry-result 的「异步 + 轮询」
        区分 —— 重下载场景下用户已经在 UI 前等待,不需要再轮询一次。

        状态码:
          - 200:成功,响应体是新 task 行(result_url 已更新)
          - 404:task_id 不存在
          - 409:任务非 succeeded / 缺少 conversation_id / 原账号不可用 /
           已有 retry 在跑 / 重解析超时
          - 503:video_service 未启动
        """
        authorize_with_license(x_doupool_token, authorization)
        if video_service is None:
            raise HTTPException(status_code=503, detail="视频服务未启动")
        try:
            wrapper = video_service.schedule_refresh_url(task_id)
            task = await wrapper
        except ValueError as exc:
            msg = str(exc)
            if "任务不存在" in msg:
                raise HTTPException(status_code=404, detail=msg) from exc
            raise HTTPException(status_code=409, detail=msg) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if task is None:
            raise HTTPException(
                status_code=409,
                detail="刷新下载链接超时,远端尚未生成完成",
            )
        quotas = settings_service.get_daily_quotas() if settings_service else {"shared": 50}
        return _video_task_dict(task, quotas["shared"])

    @app.post("/api/results/group-download")
    async def group_download(
        body: GroupDownloadBody,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """v0.2.28 Q2:把 group_id 下所有 succeeded 任务的视频流式下载到
        settings.download_dir/<batch_folder>/。前端在结果页点「保存到下载
        目录」按钮时调用,完成后 alert 提示用户打开文件夹。

        优先级:clean_video_url(无水印) > result_url(原画)。文件命名
        doubao-<task_id>[-clean].mp4,重名追加 -2 / -3 ...。

        状态码:
          - 200:{saved_dir, file_count} —— 即使 file_count=0 也成功
          - 404:group_id 不存在
          - 409:某个视频的签名 URL 已过期(401/403) → 提示用户先点
            「刷新下载链接」
          - 500:磁盘满 / 权限不足 / 下载目录不存在 → 引导用户去设置改
            download_dir
          - 503:video_service 未启动 或 settings_service 缺失
        """
        authorize_with_license(x_doupool_token, authorization)
        if video_service is None:
            raise HTTPException(status_code=503, detail="视频服务未启动")
        if settings_service is None:
            raise HTTPException(status_code=503, detail="设置服务未启动")

        download_dir = Path(settings_service.get()["download_dir"]).expanduser()
        # 兜底 mkdir:settings.update 时已建,但若用户在系统层删了目录,
        # 这次保存前补建 —— 友好降级。
        try:
            download_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"下载目录 {download_dir} 无法创建: {exc}",
            ) from exc

        tasks = repository.list_tasks_by_group(body.group_id)
        if not tasks:
            raise HTTPException(status_code=404, detail=f"group_id {body.group_id} 不存在")

        # v0.3.8:有组名时目录直接使用清洗后的组名;旧任务/无名组继续
        # 使用 group_id 前缀 + 时间戳,同秒重名追加 -2 / -3 ...。
        timestamp = datetime.now().strftime("%H%M%S")
        raw_group_name = next(
            (
                task.group_name
                for task in tasks
                if getattr(task, "group_name", None)
                and task.group_name.strip()
            ),
            None,
        )
        if raw_group_name and raw_group_name.strip():
            base_folder = _sanitize_filename_part(raw_group_name.strip(), max_len=40)
        else:
            base_folder = f"{body.group_id[:8]}_{timestamp}"
        batch_folder = download_dir / base_folder
        n = 2
        while batch_folder.exists():
            batch_folder = download_dir / f"{base_folder}-{n}"
            n += 1
        try:
            batch_folder.mkdir(parents=False, exist_ok=False)
        except OSError as exc:
            raise HTTPException(
                status_code=500,
                detail=f"无法创建批次文件夹 {batch_folder}: {exc}",
            ) from exc

        saved_count = 0
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, connect=10.0),
                follow_redirects=True,
            ) as client:
                for task in tasks:
                    url = task.clean_video_url or task.result_url
                    if not url:
                        continue
                    # v0.2.35:批量下载命名 —— 与 _video_task_dict.download_filename 同源
                    stem = _build_download_filename(task).removesuffix(".mp4")
                    target = batch_folder / f"{stem}.mp4"
                    suffix_n = 2
                    while target.exists():
                        target = batch_folder / f"{stem}-{suffix_n}.mp4"
                        suffix_n += 1
                    try:
                        async with client.stream("GET", url) as resp:
                            if resp.status_code in (401, 403):
                                raise HTTPException(
                                    status_code=409,
                                    detail=f"任务 {task.id} 签名链接已过期,请先在结果页点刷新",
                                )
                            resp.raise_for_status()
                            with target.open("wb") as f:
                                async for chunk in resp.aiter_bytes(chunk_size=64 * 1024):
                                    f.write(chunk)
                    except HTTPException:
                        # 签名过期这种业务错误直接往上抛
                        raise
                    except OSError as exc:
                        err_no = getattr(exc, "errno", None)
                        if err_no == 28:  # ENOSPC
                            raise HTTPException(
                                status_code=500,
                                detail="本地磁盘空间不足,无法保存视频",
                            ) from exc
                        if err_no == 13:  # EACCES
                            raise HTTPException(
                                status_code=500,
                                detail=f"下载目录无写权限,请在设置中更换路径: {download_dir}",
                            ) from exc
                        raise HTTPException(
                            status_code=500,
                            detail=f"写文件失败 {target}: {exc}",
                        ) from exc
                    except httpx.HTTPError as exc:
                        raise HTTPException(
                            status_code=502,
                            detail=f"下载任务 {task.id} 视频失败: {exc}",
                        ) from exc
                    saved_count += 1
        except HTTPException:
            # 出错时把已落盘的部分文件夹留着给用户手动取 —— 比直接清掉友好。
            raise

        return {"saved_dir": str(batch_folder), "file_count": saved_count}

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
        authorize_with_license(x_doupool_token, authorization)
        if video_service is None:
            raise HTTPException(status_code=503, detail="视频服务未启动")
        try:
            video_service.delete(task_id)
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    # ---------- v0.2.35:一键清除任务 + 一键清除结果 ----------

    @app.post("/api/video-tasks/clear-completed")
    def clear_completed_video_tasks(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """v0.2.35:一键清除已完成任务(succeeded / failed / cancelled)。

        running 状态(starting / generating / resolving) 不会被碰 —— 防打断正在生成。
        对预扣过额度的任务在删除前退额度(走 `_pre_charged_tasks` 内存 + DB 兜底)。
        本地视频文件保留(用户已下载过的归用户管)。

        返回:{"deleted_count": int}
        """
        authorize_with_license(x_doupool_token, authorization)
        if video_service is None:
            raise HTTPException(status_code=503, detail="视频服务未启动")
        deleted = video_service.clear_tasks("completed")
        return {"deleted_count": deleted}

    @app.post("/api/video-tasks/clear-queued")
    def clear_queued_video_tasks(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """v0.2.35:一键清除排队中任务(只动 queued,不动 starting/generating/resolving)。

        同上:预扣额度在删除前退。返回 {"deleted_count": int}。
        """
        authorize_with_license(x_doupool_token, authorization)
        if video_service is None:
            raise HTTPException(status_code=503, detail="视频服务未启动")
        deleted = video_service.clear_tasks("queued")
        return {"deleted_count": deleted}

    @app.post("/api/results/clear-downloaded")
    def clear_downloaded_results(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """v0.2.35:一键清除已下载结果(clean_video_url OR result_url IS NOT NULL)。

        succeeded 状态 —— 豆包已结算额度,无需退额度。只删 DB row,本地文件保留。
        返回 {"deleted_count": int}。
        """
        authorize_with_license(x_doupool_token, authorization)
        if video_service is None:
            raise HTTPException(status_code=503, detail="视频服务未启动")
        deleted = video_service.clear_results(downloaded_only=True)
        return {"deleted_count": deleted}

    @app.post("/api/results/clear-all")
    def clear_all_results(
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """v0.2.35:一键清除全部结果(所有 succeeded 任务)。

        同上:本地文件保留,DB row 物理删。返回 {"deleted_count": int}。
        """
        authorize_with_license(x_doupool_token, authorization)
        if video_service is None:
            raise HTTPException(status_code=503, detail="视频服务未启动")
        deleted = video_service.clear_results(downloaded_only=False)
        return {"deleted_count": deleted}

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
        authorize_with_license(x_doupool_token, authorization)
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
        except Exception as exc:
            # v0.2.36:兜底所有意外(profile 路径含特殊字符 / Windows 长路径 /
            # sqlite3.DatabaseError 漏网 / 其他 OSError),避免前端拿到 500
            # + 「token 状态加载失败」这种毫无信息量的兜底文案。改成 200 +
            # available=False + hint 携带真实异常(账号"已登录"标识与 token
            # 状态是两条独立路径,不应当让一个 IO 异常把整行 token 状态搞崩)。
            logging.getLogger("doupool.api").exception(
                "读取 token bundle 失败: account=%s profile=%s", account_id, profile_dir,
            )
            return {
                "available": False,
                "hint": f"{exc.__class__.__name__}:{exc}",
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

        v0.2.37.2:简化实现 —— 不再等 18s 探 web_id 落盘(那条路径实测在生产环境
        经常让用户等不到结果)。改成「打开浏览器 → 8s 等 WebMSSDK 初始化 →
        调用 _save_doubao_cookies_to_disk 写 cookies.json → 关窗」,让
        extract_webmssdk_tokens 通过 cookies.json 拿到当前真实登录态。

        如果 web_id 没落地(用户从未在浏览器里访问过 chat/),extract 仍会抛
        TokenBundleUnavailable,UI 提示「请在浏览器里手动访问 chat/ 主页」。
        """
        authorize_with_license(x_doupool_token, authorization)
        account = Account.get_or_none(Account.id == account_id)
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        profile_dir = Path(account.profile_dir)

        def _refresh():
            t0 = time.monotonic()
            from doupool.login.browser import _save_doubao_cookies_to_disk
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
                    # v0.2.37.2:8 秒等 SDK 初始化足够(主路径已经走 cookies.json
                    # 优先,web_id 缺失也只是 web_id 字段为空,不影响登录态读取)。
                    page.wait_for_timeout(8_000)
                    _save_doubao_cookies_to_disk(ctx, profile_dir)
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

    # v0.2.37.2:「重新导出 cookies」专用端点 ——
    # 跟 refresh-tokens 逻辑相同(打开浏览器 → 写 cookies.json → 关),但语义
    # 更清楚:用户点击这个按钮时,目标是「让 cookies.json 重新可读」,跟
    # 「让 WebMSSDK 跑一遍拿新 msToken」是两种不同诉求。如果 cookies.json
    # 还在,或者 cookies.json 解析失败,这个按钮能让 Playwright 用最新 cookie
    # 重写一份明文备份(避开 DPAPI 加密 / 文件损坏等问题)。
    @app.post("/api/accounts/{account_id}/re-export-cookies", status_code=200)
    async def re_export_cookies(
        account_id: str,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        authorize_with_license(x_doupool_token, authorization)
        account = Account.get_or_none(Account.id == account_id)
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        profile_dir = Path(account.profile_dir)

        def _do():
            from doupool.login.browser import _save_doubao_cookies_to_disk
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
                    page.wait_for_timeout(8_000)
                    saved = _save_doubao_cookies_to_disk(ctx, profile_dir)
                    return {
                        "saved": saved,
                        "elapsed": time.monotonic() - t0,
                    }
                finally:
                    try:
                        ctx.close()
                    except Exception:
                        pass

        try:
            result = await asyncio.to_thread(_do)
        except Exception as exc:
            logging.getLogger("doupool.api").exception(
                "重新导出 cookies 失败: account=%s", account_id
            )
            raise HTTPException(
                status_code=503,
                detail=f"重新导出 cookies 失败:{exc}",
            ) from exc
        if not result["saved"]:
            raise HTTPException(
                status_code=400,
                detail="重新导出失败:浏览器里读不到 doubao.com cookie,"
                "可能账号已掉登录,请重新扫码登录。",
            )
        return {
            "ok": True,
            "saved": True,
            "elapsed": round(result["elapsed"], 1),
            "hint": f"已重新导出 cookies.json,耗时 {result['elapsed']:.1f}s",
        }

    # v0.2.20:「📂 打开浏览器」按钮 ——
    # 复用账号已有的 login profile(cookies / identity / WebMSSDK leveldb
    # 缓存全在那个目录里)重新拉起一个可视化 Chromium 窗口,让用户可以在
    # 那个窗口里访问 doubao.com/chat/ 生成 WebMSSDK token,
    # 或者只是随便看一下自己的登录态。窗口会一直留着,直到用户主动关掉
    # 或前端调 close-browser。同 profile_dir 互斥,避免 Chromium
    # SingletonLock 冲突。

    def _open_browser_runner(account_id: str, profile_dir: Path, cancel: threading.Event) -> None:
        """在独立 daemon 线程里跑 Playwright,直到 cancel 或 context 自然关闭。

        必须用 sync_playwright() + chromium.launch_persistent_context,与登录
        runner 一致 —— 用同一个 profile_dir 才能让 cookies / identity / leveldb
        完全复用,不要走 _build_launch_kwargs 的「视频提交专用 stealth args」,
        用户打开浏览器就是要用正常 UI,不是隐身爬虫。

        active_pages 监听模式参考 login/browser.py:_on_initial_page_close,
        所有页 close 时主动 cancel context 让 run() 自然返回。
        """
        from playwright.sync_api import sync_playwright
        from playwright.sync_api import Error as PlaywrightError

        active_pages: list = []
        lock = threading.Lock()

        def add_page(page):
            with lock:
                if page not in active_pages:
                    active_pages.append(page)

        def remove_page(page):
            with lock:
                if page in active_pages:
                    active_pages.remove(page)

        def get_active():
            with lock:
                return [p for p in active_pages if not p.is_closed()]

        context = None
        try:
            with sync_playwright() as pw:
                context = pw.chromium.launch_persistent_context(
                    str(profile_dir),
                    headless=False,
                    viewport={"width": 1180, "height": 820},
                    args=[
                        # v0.2.20:用户手动打开浏览器窗口,不要再隐身到屏幕外。
                        # refresh-tokens 用 -2400,-2400 是因为那种是 headless 模拟,
                        # 这里是真的让人看。
                        "--window-position=80,80",
                        "--window-size=1180,820",
                    ],
                )
                context.on("page", add_page)
                initial = context.pages[0] if context.pages else context.new_page()
                add_page(initial)

                def _on_any_close(_payload):
                    remove_page(initial if _payload == initial else _payload)
                    # 所有 page 都被关掉,自然结束 run()
                    if not get_active():
                        cancel.set()

                initial.on("close", _on_any_close)

                try:
                    initial.goto(
                        "https://www.doubao.com/chat/",
                        wait_until="domcontentloaded",
                        timeout=30_000,
                    )
                except PlaywrightError as exc:
                    logging.getLogger("doupool.api").warning(
                        "打开浏览器:goto doubao.com 失败 account=%s err=%s",
                        account_id, exc,
                    )
                    # 即使 goto 失败也留着窗口,让用户看到错误自己处理
                    pass

                # 用户主动关窗口 → cancel 被 set;或 API cancel-browser →
                # cancel 被 set。两种都跳出循环,跑到 finally 关 context。
                while not cancel.is_set():
                    active = get_active()
                    if not active:
                        break
                    try:
                        active[0].wait_for_timeout(500)
                    except PlaywrightError:
                        # Playwright 自己已关掉(context close 后 page 失效),
                        # 跳出即可
                        break
        except Exception:
            logging.getLogger("doupool.api").exception(
                "「📂 打开浏览器」runner 异常 account=%s", account_id
            )
        finally:
            if context is not None:
                try:
                    context.close()
                except Exception:
                    pass

    @app.post("/api/accounts/{account_id}/open-browser", status_code=202)
    async def open_browser(
        account_id: str,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """v0.2.20:复用账号已有的 login profile 拉起 Chromium 窗口。

        设计:Playwright 跑在独立 daemon 线程,不阻塞 FastAPI 事件循环。
        同 profile_dir 已有窗口时返回 409。线程结束(cancel / 用户关窗口 /
        context 异常)后 registry 自动 unregister。
        """
        authorize_with_license(x_doupool_token, authorization)
        account = Account.get_or_none(Account.id == account_id)
        if not account:
            raise HTTPException(status_code=404, detail="account not found")
        profile_dir = Path(account.profile_dir)
        if not profile_dir.exists():
            raise HTTPException(
                status_code=409,
                detail="账号 profile 目录不存在,请先重新登录",
            )
        registry = get_browser_sessions_registry()
        cancel = threading.Event()
        thread = threading.Thread(
            target=_open_browser_runner,
            args=(account_id, profile_dir, cancel),
            name=f"open-browser-{account_id}",
            daemon=True,
        )
        try:
            registry.register(account_id, BrowserSession(thread, cancel, profile_dir))
        except BrowserAlreadyOpen as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        thread.start()
        return {
            "ok": True,
            "account_id": account_id,
            "message": "浏览器窗口已启动,关闭浏览器或调用 /close-browser 后自动结束",
        }

    @app.post("/api/accounts/{account_id}/close-browser")
    def close_browser(
        account_id: str,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """v0.2.20:主动关掉 open-browser 打开的窗口。

        cancel event set 之后 Playwright runner 会在下一个 wait_for_timeout
        切片检测到并 close context,thread 自然退出,registry 自动 unregister。
        """
        authorize_with_license(x_doupool_token, authorization)
        registry = get_browser_sessions_registry()
        sent = registry.request_cancel(account_id)
        return {
            "ok": True,
            "account_id": account_id,
            "cancel_sent": sent,
            "message": "已通知浏览器关闭" if sent else "该账号没有打开的浏览器窗口",
        }

    @app.get("/api/accounts/{account_id}/browser-status")
    def browser_status(
        account_id: str,
        x_doupool_token: str | None = Header(default=None),
        authorization: str | None = Header(default=None),
    ):
        """v0.2.20:前端轮询当前账号的「打开浏览器」状态,以决定按钮显示
        「打开」还是「关闭」。"""
        authorize_with_license(x_doupool_token, authorization)
        registry = get_browser_sessions_registry()
        return {"account_id": account_id, "open": registry.is_open(account_id)}

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
