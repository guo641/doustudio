from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import threading
from collections import defaultdict
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from doupool.db.models import Account, VideoTask
from doupool.db.repository import AccountRepository

from .protocol import DURATIONS, MAX_I2V_IMAGES, MODELS, RATIOS, TASK_MODES, DoubaoRateLimited


class NoAvailableAccount(RuntimeError):
    pass


class _DefaultSettings:
    def get(self):
        return {"daily_quota": 5, "quota_reset_time": "00:00", "max_concurrency": 1}


def quota_window(now: datetime, reset_value: str) -> tuple[date, datetime]:
    local_now = now.replace(tzinfo=UTC).astimezone(ZoneInfo("Asia/Shanghai"))
    hour, minute = map(int, reset_value.split(":"))
    reset = datetime.combine(local_now.date(), time(hour, minute), ZoneInfo("Asia/Shanghai"))
    if local_now < reset:
        business_date = local_now.date() - timedelta(days=1)
        next_reset = reset
    else:
        business_date = local_now.date()
        next_reset = reset + timedelta(days=1)
    return business_date, next_reset.astimezone(UTC).replace(tzinfo=None)


_DATA_URL_RE = re.compile(r"^data:(image/[\w.+-]+);base64,(.+)$", re.I | re.S)
_MAX_IMAGE_BYTES = 15 * 1024 * 1024


class VideoTaskService:
    def __init__(
        self,
        repository: AccountRepository,
        runner,
        settings_service=None,
        account_poll_interval: float = 1,
        assets_dir: Path | None = None,
    ):
        self.repository = repository
        self.runner = runner
        self.settings_service = settings_service or _DefaultSettings()
        self.account_poll_interval = account_poll_interval
        self.assets_dir = Path(assets_dir) if assets_dir else None
        self.logger = logging.getLogger("doupool.video")
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._cancellations: dict[str, threading.Event] = {}
        self._account_locks: defaultdict[str, asyncio.Lock] = defaultdict(asyncio.Lock)
        self._global_semaphore: asyncio.Semaphore | None = None
        self._semaphore_limit = 0

    def start(
        self,
        prompt: str,
        model: str,
        ratio: str,
        duration: int,
        account_id: str | None = None,
        mode: str = "t2v",
        images: list[dict] | None = None,
    ):
        prompt = prompt.strip()
        mode = (mode or "t2v").strip().lower()
        if not prompt:
            raise ValueError("请输入画面描述")
        if mode not in TASK_MODES:
            raise ValueError("不支持的任务类型")
        if model not in MODELS or ratio not in RATIOS or duration not in DURATIONS:
            raise ValueError("不支持的视频参数")
        if account_id:
            account = Account.get_or_none(Account.id == account_id)
            if not account or not account.enabled or account.status != "active":
                raise NoAvailableAccount("指定的豆包账号不可用")

        image_paths: list[str] = []
        if mode == "i2v":
            if not images:
                raise ValueError("图生视频请至少上传 1 张图片")
            if len(images) > MAX_I2V_IMAGES:
                raise ValueError(f"图生视频最多支持 {MAX_I2V_IMAGES} 张图片")
            if self.assets_dir is None:
                raise ValueError("图生视频资源目录未配置")
            image_paths = self._persist_images(images)
        elif images:
            raise ValueError("文生视频不支持图片附件")

        task = self.repository.create_video_task(
            account_id,
            prompt,
            model,
            ratio,
            duration,
            mode=mode,
            image_paths=image_paths or None,
        )
        self._schedule(task.id)
        return task

    def _persist_images(self, images: list[dict]) -> list[str]:
        count = len(images)
        if count < 1 or count > MAX_I2V_IMAGES:
            raise ValueError(f"图生视频支持 1–{MAX_I2V_IMAGES} 张图片")
        folder = self.assets_dir / "video-assets" / str(uuid4())
        folder.mkdir(parents=True, exist_ok=True)
        saved: list[str] = []
        used_names: set[str] = set()
        for index, image in enumerate(images):
            raw_name = str(image.get("name") or f"image-{index + 1}.png")
            safe_name = re.sub(r"[^\w.\-]+", "_", raw_name).strip("._") or f"image-{index + 1}.png"
            if not Path(safe_name).suffix:
                safe_name += ".png"
            # avoid collisions when multiple files share a name
            if safe_name in used_names:
                stem = Path(safe_name).stem
                suffix = Path(safe_name).suffix
                safe_name = f"{stem}-{index + 1}{suffix}"
            used_names.add(safe_name)
            payload = str(image.get("data_base64") or "")
            mime = "image/png"
            match = _DATA_URL_RE.match(payload)
            if match:
                mime = match.group(1).lower()
                payload = match.group(2)
            try:
                data = base64.b64decode(payload, validate=False)
            except (binascii.Error, ValueError) as exc:
                raise ValueError("图片数据无效") from exc
            if not data:
                raise ValueError("图片内容为空")
            if len(data) > _MAX_IMAGE_BYTES:
                raise ValueError("单张图片不能超过 15MB")
            if not any(mime.startswith(prefix) for prefix in ("image/png", "image/jpeg", "image/webp", "image/gif")):
                # still allow if extension looks like an image
                if Path(safe_name).suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".gif"}:
                    raise ValueError("仅支持 png/jpeg/webp/gif 图片")
            path = folder / safe_name
            path.write_bytes(data)
            saved.append(str(path))
        return saved

    def _schedule(self, task_id: str) -> None:
        if task_id in self._tasks and not self._tasks[task_id].done():
            return
        cancellation = threading.Event()
        self._cancellations[task_id] = cancellation
        self._tasks[task_id] = asyncio.create_task(self._run(task_id, cancellation))

    async def resume_queued(self) -> None:
        for task in self.repository.list_queued_video_tasks():
            self._schedule(task.id)

    async def _run(self, task_id: str, cancellation: threading.Event) -> None:
        settings = self.settings_service.get()
        while not cancellation.is_set():
            settings = self.settings_service.get()
            concurrency = int(settings.get("max_concurrency", 1))
            if self._global_semaphore is None or self._semaphore_limit != concurrency:
                self._global_semaphore = asyncio.Semaphore(concurrency)
                self._semaphore_limit = concurrency
            business_date, next_reset = quota_window(datetime.now(UTC), settings["quota_reset_time"])
            self.repository.reset_daily_quotas(business_date)
            task = self.repository.get_video_task(task_id)
            account = task.account if task.account_id else self.repository.choose_available_account(
                int(settings["daily_quota"]), strategy=settings.get("scheduler_strategy", "least_used")
            )
            if account is None:
                self.repository.update_video_task(task_id, status="queued", error_message="等待可用账号")
                await asyncio.sleep(self.account_poll_interval)
                continue
            self.repository.assign_video_task(task_id, account.id)
            async with self._global_semaphore:
                async with self._account_locks[account.id]:
                    if cancellation.is_set():
                        return
                    self.repository.update_video_task(task_id, status="starting", error_message=None)
                    quota_recorded = False

                    def update(**values) -> None:
                        nonlocal quota_recorded
                        if values.get("status") == "generating" and not quota_recorded:
                            self.repository.increment_account_quota(account.id)
                            quota_recorded = True
                        self.repository.update_video_task(task_id, **values)

                    try:
                        image_paths = []
                        if task.image_paths:
                            try:
                                image_paths = json.loads(task.image_paths)
                            except json.JSONDecodeError:
                                image_paths = []
                        result = await asyncio.to_thread(
                            self.runner.run,
                            account.profile_dir,
                            task.prompt,
                            task.model,
                            task.ratio,
                            task.duration,
                            update,
                            cancellation,
                            mode=getattr(task, "mode", None) or "t2v",
                            image_paths=image_paths or None,
                        )
                        self.repository.update_video_task(task_id, status="succeeded", **result)
                        self.logger.info(
                            "视频任务生成成功", extra={"event": "video_succeeded", "account_id": account.id}
                        )
                        return
                    except DoubaoRateLimited:
                        self.repository.mark_account_limited(
                            account.id, next_reset, int(settings["daily_quota"])
                        )
                        self.repository.assign_video_task(task_id, None)
                        self.repository.update_video_task(
                            task_id, status="queued", error_message="账号今日额度已用完，正在切换账号"
                        )
                        self.logger.warning(
                            "账号今日视频额度已用完",
                            extra={"event": "video_quota_limited", "account_id": account.id},
                        )
                        continue
                    except Exception as exc:
                        if cancellation.is_set():
                            self.repository.assign_video_task(task_id, None)
                            self.repository.update_video_task(
                                task_id, status="queued", error_message="应用已停止，等待下次继续"
                            )
                            return
                        self.repository.update_video_task(task_id, status="failed", error_message=str(exc))
                        self.logger.exception(
                            "视频任务失败", extra={"event": "video_failed", "account_id": account.id}
                        )
                        return

    async def shutdown(self) -> None:
        for cancellation in self._cancellations.values():
            cancellation.set()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
