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
from doupool.prompt_reviser import classify_failure, revise_prompt
from doupool.prompt_parser import split_by_segment_markers
from doupool import callbacks as callbacks_mod
from doupool.watermark import (
    ZhucekaConfigError,
    ZhucekaError,
    resolve_clean_url as zhuceka_resolve,
)

from .cost import quota_cost
from .protocol import DURATIONS, MAX_I2V_IMAGES, MODELS, RATIOS, TASK_MODES, DoubaoRateLimited


class NoAvailableAccount(RuntimeError):
    pass


class _DefaultSettings:
    def get(self):
        return {
            "daily_quota_mini": 5, "daily_quota_v2": 5, "daily_quota_std": 5,
            "quota_reset_time": "00:00", "max_concurrency": 1,
        }

    def get_daily_quotas(self):
        return {"mini": 5, "v2": 5, "std": 5}


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
        # v0.2.9:retry-result 独立 task 池 —— 不和正常生成 task 混,
        # shutdown 时也不强制 cancel(用户主动 retry 的话让他跑完)。
        self._retry_tasks: dict[str, asyncio.Task[None]] = {}
        self._retry_cancellations: dict[str, threading.Event] = {}
        # v0.2.9:callback 任务池 —— 每个 task 最多一个 callback 在跑,
        # 重试 retry-result 不重复发回执(只在最终 terminal 状态发一次)。
        self._callback_tasks: dict[str, asyncio.Task[None]] = {}
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
        prompts: list[str] | None = None,
        callback_url: str | None = None,
    ):
        prompt = prompt.strip()
        mode = (mode or "t2v").strip().lower()
        # 兼容: 同时支持单 prompt / prompts 列表
        # v0.2.11:只有单 prompt 字段才后端切段(prompts 列表前端已切好,
        # 再切会把"第一段"字样当成标记误伤 prompt 文本)。
        prompt_list: list[str] = []
        if prompts:
            for p in prompts:
                p2 = (p or "").strip()
                if p2:
                    prompt_list.append(p2)
        if prompt:
            if prompt_list:
                # 同一调用里同时给 prompt 和 prompts,prompt 当作第一段前缀补在队首
                prompt_list.insert(0, prompt)
            else:
                # 单 prompt 字段:防御性 — 后端再切一次,
                # 兼容 curl / 老前端没切就发过来。
                prompt_list = split_by_segment_markers(prompt)
                if not prompt_list:
                    prompt_list = [prompt]
        if not prompt_list:
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

        # 多个 prompt → 同一 group_id,自动归组
        group_id = str(uuid4()) if len(prompt_list) > 1 else None
        first_task = None
        for index, p in enumerate(prompt_list, start=1):
            task = self.repository.create_video_task(
                account_id,
                p,
                model,
                ratio,
                duration,
                mode=mode,
                image_paths=image_paths or None,
                group_id=group_id,
                group_index=index if group_id else 0,
                callback_url=callback_url,
            )
            self._schedule(task.id)
            if first_task is None:
                first_task = task
        return first_task

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
            try:
                # 磁盘满 / 权限不足 / 文件被占用 → write_bytes 会抛 OSError
                # 之前直接冒泡成 500,前端只能看到泛化的"图片数据无效",
                # 用户根本不知道是磁盘写不进去。包成 ValueError,前端能拿到
                # 可读信息(如"保存图片失败:[Errno 28] No space left on device")。
                path.write_bytes(data)
            except OSError as exc:
                raise ValueError(f"保存图片失败:{exc}") from exc
            saved.append(str(path))
        return saved

    def _schedule(self, task_id: str) -> None:
        if task_id in self._tasks and not self._tasks[task_id].done():
            return
        cancellation = threading.Event()
        self._cancellations[task_id] = cancellation
        self._tasks[task_id] = asyncio.create_task(self._run(task_id, cancellation))

    def _schedule_callback(self, task_id: str) -> None:
        """v0.2.9:任务到 terminal 状态后异步派发 callback。

        同步 fire-and-forget —— 不 await。callback 失败/超时由 dispatcher
        内部退避重试,不影响主任务状态。每个 task 只发一次(retry-result
        拿新 result 后也会复用这个方法,这时旧 callback task 还没跑完的
        就让它跑完,用最新 payload 再发一次)。
        """
        # 同 task 已有 callback 在跑 → 取消,避免旧 payload 先发到 callback_url
        if task_id in self._callback_tasks and not self._callback_tasks[task_id].done():
            self._callback_tasks[task_id].cancel()
        self._callback_tasks[task_id] = asyncio.create_task(
            self._dispatch_callback(task_id)
        )

    async def _dispatch_callback(self, task_id: str) -> None:
        try:
            task = self.repository.get_video_task(task_id)
            if task is None:
                return
            # 没设 callback_url → 直接标 disabled,跳过 dispatcher。
            if not (task.callback_url or "").strip():
                if task.callback_status != "disabled":
                    self.repository.update_video_task(
                        task_id, callback_status="disabled", callback_last_error=None
                    )
                return
            await callbacks_mod.dispatch(
                task,
                lambda **values: self.repository.update_video_task(task_id, **values),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            self.logger.exception(
                "callback 调度异常",
                extra={"event": "callback_dispatch_crashed", "task_id": task_id},
            )
        finally:
            self._callback_tasks.pop(task_id, None)

    # ---------- v0.2.9 retry-result 入口 ----------
    def retry_result(self, task_id: str) -> VideoTask:
        """同步入口:做参数校验 + 调度后台协程,本身不 await。

        asyncio.create_task 需要 running event loop,FastAPI route / pytest-asyncio
        测试都在 loop 内,所以这一层是安全的;为了让 service 测试能在没 loop
        的上下文里跑参数校验逻辑,这里把 create_task 拆出来,API 路由侧负责
        await 包了一层。
        """
        try:
            task = self.repository.get_video_task(task_id)
        except Exception:  # VideoTask.DoesNotExist —— peewee 抛错而非返回 None
            raise ValueError("任务不存在") from None
        if task is None:
            raise ValueError("任务不存在")
        if not task.conversation_id:
            raise ValueError("任务缺少 conversation_id,请重新提交")
        if task.account_id is None:
            raise ValueError("原账号不可用,无法重解析")
        account = Account.get_or_none(Account.id == task.account_id)
        if account is None or not Path(account.profile_dir).exists():
            raise ValueError("原账号不可用,无法重解析")
        # 每个 task 一次只允许一个重解析在跑,避免双开浏览器把同一 chain
        # 抓两遍互相打架。
        if task_id in self._retry_tasks and not self._retry_tasks[task_id].done():
            raise RuntimeError("已有 retry-result 在运行")
        cancellation = threading.Event()
        self._retry_cancellations[task_id] = cancellation
        self._retry_tasks[task_id] = asyncio.create_task(
            self._retry_result_inner(task_id, account.profile_dir, cancellation)
        )
        return task

    async def schedule_retry_result(self, task_id: str) -> VideoTask:
        """异步入口:在 running event loop 里安全 schedule,供 FastAPI 调用。
        pytest 没起 loop 的同步测试直接用 retry_result()。
        """
        return self.retry_result(task_id)

    # ---------- v0.2.11 任务删除入口 ----------
    def delete(self, task_id: str) -> None:
        """v0.2.11:删除一条视频任务。

        规则:
          - 任务不存在 → ValueError(API 层 404)
          - 状态在 {starting, generating, resolving} → RuntimeError(API 层 409)
          - 其它状态(queued / rechecking / succeeded / failed / limited / cancelled)
            → 取消可能还在跑的 callback / retry-result 协程,物理 delete_instance。

        _run_inner 在 cancellation 触发后写 status 时若 row 已不存在,
        peewee 会抛 DoesNotExist,但走不到这里(running 状态已被挡掉),
        只对 queued 任务需要小心:_schedule 已经把 cancellation 注册好,
        但 _run 里 cancellation.is_set() 后会返回,不会写 status,直接
        delete_instance 安全。
        """
        try:
            task = self.repository.get_video_task(task_id)
        except Exception:  # VideoTask.DoesNotExist
            raise ValueError("任务不存在") from None
        if task is None:
            raise ValueError("任务不存在")
        if task.status in AccountRepository._RUNNING_STATUSES:
            raise RuntimeError("任务正在生成中,请等待结束后再删除")

        # 取消可能正在跑 / 排队等跑的 callback 协程,避免它拿着失效 task_id 跑
        callback_task = self._callback_tasks.get(task_id)
        if callback_task is not None and not callback_task.done():
            callback_task.cancel()
            self._callback_tasks.pop(task_id, None)

        # 取消可能的 retry-result 后台协程
        retry_task = self._retry_tasks.get(task_id)
        if retry_task is not None and not retry_task.done():
            self._retry_cancellations.get(task_id, threading.Event()).set()
            retry_task.cancel()
            self._retry_tasks.pop(task_id, None)
            self._retry_cancellations.pop(task_id, None)

        self.repository.delete_video_task(task_id)
        self.logger.info(
            "video task deleted",
            extra={"event": "video_task_deleted", "task_id": task_id, "status": task.status},
        )

    async def _retry_result_inner(
        self, task_id: str, profile_dir: str, cancellation: threading.Event
    ) -> None:
        """retry-result 后台协程。和 _run_inner 解耦,不复用其配额 / 调度逻辑。

        错误兜底同 _run:不抛到外层,失败时把 task 标 failed 留痕。
        """
        try:
            await self._retry_result_body(task_id, profile_dir, cancellation)
        except asyncio.CancelledError:
            self.repository.update_video_task(
                task_id,
                status="failed",
                error_message="retry-result 已取消",
            )
            raise
        except Exception as exc:
            self.logger.exception(
                "retry-result 出现未捕获异常",
                extra={"event": "video_retry_crashed", "task_id": task_id},
            )
            try:
                self.repository.update_video_task(
                    task_id,
                    status="failed",
                    error_message=f"retry-result 异常:{exc}",
                )
            except Exception:
                self.logger.exception(
                    "retry-result 兜底写 failed 也失败", extra={"task_id": task_id}
                )
        finally:
            self._retry_tasks.pop(task_id, None)
            self._retry_cancellations.pop(task_id, None)

    async def _retry_result_body(
        self, task_id: str, profile_dir: str, cancellation: threading.Event
    ) -> None:
        task = self.repository.get_video_task(task_id)
        if task is None:
            return
        conversation_id = task.conversation_id or ""
        # 进入重解析流程,状态标记 rechecking 但要记住"原本是什么状态",
        # 抛错回滚时还原 —— 否则 succeeded 任务一旦重解析失败会被强标 failed,
        # 把旧 result_url 也覆盖掉,用户连已下载的链接都拿不回来。
        previous_status = task.status
        self.repository.update_video_task(
            task_id, status="rechecking", error_message=None
        )

        def update(**values) -> None:
            # 注意:不调用 increment_account_quota —— 重解析不消耗额度。
            self.repository.update_video_task(task_id, **values)

        try:
            result = await asyncio.to_thread(
                self.runner.recheck_result,
                profile_dir,
                conversation_id,
                update,
                cancellation,
            )
        except Exception as exc:
            # recheck_result 抛错 = 远端确实没 result / 网络挂了 / 风控拒了
            # 回退到 previous_status(原本可能是 succeeded),不强行标 failed。
            # 仅在 error_message 留痕 —— 已 succeeded 的旧 result_url 仍能让
            # 用户下载。
            self.repository.update_video_task(
                task_id,
                status=previous_status,
                error_message=f"重解析失败:{exc}",
            )
            return
        if result is None:
            # 还在生成中:回退到 generating(若原本是 queued 也合理)。
            # 失败 / succeeded 都覆盖成 generating 较激进,所以按 previous 决定:
            # terminal 状态保留,非 terminal 推到 generating。
            fallback = previous_status if previous_status in {"succeeded", "failed"} else "generating"
            self.repository.update_video_task(
                task_id,
                status=fallback,
                error_message="重解析超时,远端尚未生成完成",
            )
            return
        # 拿到新 result:把 succeeded 字段更新。watermark 异步清洗照常跑。
        self.repository.update_video_task(
            task_id, status="succeeded", error_message=None, **result
        )
        self.logger.info(
            "retry-result 拿到新 result_url", extra={"event": "video_retry_succeeded", "task_id": task_id}
        )
        settings = self.settings_service.get()
        try:
            await self._run_watermark(task_id, result, settings)
        except Exception as watermark_exc:
            self.repository.update_video_task(
                task_id, clean_error=f"去水印过程异常:{watermark_exc}"
            )
            self.logger.exception(
                "retry-result 后去水印异常", extra={"task_id": task_id}
            )
        # v0.2.9:retry-result 拿到新 result 也发 callback —— callback_url
        # 之前可能已经发过"旧 succeeded"的回执,但拿到的 result_url 已更新,
        # 重新发一次让接收方能拉到最新下载链接。
        self._schedule_callback(task_id)

    async def resume_queued(self) -> None:
        for task in self.repository.list_queued_video_tasks():
            self._schedule(task.id)

    async def _run(self, task_id: str, cancellation: threading.Event) -> None:
        try:
            await self._run_inner(task_id, cancellation)
        except asyncio.CancelledError:
            # 上层 shutdown / 用户主动取消,静默退出
            self.repository.update_video_task(
                task_id,
                status="queued",
                error_message="应用已停止，等待下次继续",
                account_id=None,
            )
            raise
        except Exception as exc:
            # 顶层兜底:即使配额/账号选择/数据库出意外,也要把任务
            # 推进到一个 terminal 状态,绝不让它卡在 queued / starting
            # 永远不被前端看到。
            self.logger.exception(
                "视频任务执行器出现未捕获异常",
                extra={"event": "video_runner_crashed", "task_id": task_id},
            )
            try:
                self.repository.update_video_task(
                    task_id,
                    status="failed",
                    error_message=f"任务执行异常:{exc}",
                    account_id=None,
                )
            except Exception:
                # 连 DB 都写不动,只能记日志
                self.logger.exception(
                    "兜底写 failed 状态也失败", extra={"task_id": task_id}
                )

    async def _run_inner(self, task_id: str, cancellation: threading.Event) -> None:
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
            # v0.2.15:任务被 DELETE 端点删了 → get_video_task 返 None,
            # worker 静默退出,不再刷 IndexError: list index out of range。
            if task is None:
                self.logger.info(
                    "视频任务已被删除,worker 退出",
                    extra={"event": "task_deleted_worker_exit", "task_id": task_id},
                )
                return
            # v0.2.9:按 task.model 找对应桶还有额度的账号。
            daily_quotas = self.settings_service.get_daily_quotas()
            account = task.account if task.account_id else self.repository.choose_available_account(
                daily_quotas,
                model=task.model,
                strategy=settings.get("scheduler_strategy", "least_used"),
            )
            if account is None:
                # v0.2.12:区分两种 None ——「没有账号」vs 「账号全满」,UI 才能给出准确指引。
                stats = self.repository.summarize_account_availability(
                    daily_quotas, model=task.model
                )
                if stats["enabled_total"] == 0:
                    msg = "暂无账号,请先在账号面板添加账号"
                elif stats["bucket_full"] >= stats["enabled_total"]:
                    msg = f"全部账号今日 {task.model} 额度已用完,明早 00:00 自动恢复"
                else:
                    msg = "等待可用账号"
                self.repository.update_video_task(task_id, status="queued", error_message=msg)
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
                            # v0.2.11:按 model + duration 算 cost,扣对应桶。
                            # 非法 duration 走兜底 max(1, duration)。
                            cost = quota_cost(task.model, int(task.duration))
                            self.repository.increment_account_quota(
                                account.id, model=task.model, by=cost
                            )
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
                        # 生成成功后异步调去水印,失败也不影响主流程。
                        # 这里再包一层 try,保证即使 _run_watermark 内部出现
                        # 意料之外的异常(比如 zhuceka 改了实现抛非 ZhucekaError),
                        # 也不会让"已 succeeded"的任务被错标为 failed。
                        try:
                            await self._run_watermark(task_id, result, settings)
                        except Exception as watermark_exc:
                            self.repository.update_video_task(
                                task_id, clean_error=f"去水印过程异常:{watermark_exc}"
                            )
                            self.logger.exception(
                                "去水印过程出现未捕获异常,任务保留 succeeded 状态",
                                extra={"event": "watermark_unexpected_error", "task_id": task_id},
                            )
                        # v0.2.9:succeeded 后异步发 callback —— 拿到最新 task 行
                        # (含 result_url / clean_video_url)再发,前端收到时就能直接用。
                        self._schedule_callback(task_id)
                        return
                    except DoubaoRateLimited as exc:
                        # v0.2.15:把豆包真正返回的 error_msg + SSE 响应原文写进
                        # WARNING —— 之前只写「额度已用完」,真正 error_msg 被吞,
                        # 「额度误报」(fingerprint / IP / 风控等)时完全没线索。
                        self.repository.mark_account_limited(
                            account.id, next_reset, daily_quotas,
                            business_date=business_date,
                        )
                        self.repository.assign_video_task(task_id, None)
                        self.repository.update_video_task(
                            task_id, status="queued", error_message=f"账号今日额度已用完:{exc}"
                        )
                        response_excerpt = (exc.response_text or "").replace("\r\n", " ")[:500]
                        self.logger.warning(
                            "账号今日视频额度已用完:%s | response=%s",
                            exc, response_excerpt,
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
                        # 分类失败 → 决定是否改写 prompt 后重试
                        failure = classify_failure(str(exc))
                        attempt = getattr(task, "prompt_retry_count", 0) or 0
                        max_attempts = int(settings.get("max_prompt_retries", 2))
                        if failure.revise_prompt and attempt < max_attempts:
                            new_prompt = revise_prompt(task.prompt, failure, attempt=attempt + 1)
                            if new_prompt and new_prompt != task.prompt:
                                self.repository.update_video_task(
                                    task_id,
                                    prompt=new_prompt,
                                    prompt_retry_count=attempt + 1,
                                    status="queued",
                                    error_message=f"改写 prompt 第 {attempt + 1} 次重试:{failure.kind}",
                                    account_id=None,
                                )
                                self.logger.warning(
                                    "prompt 改写重试 %d/%d: %s",
                                    attempt + 1, max_attempts, failure.detail,
                                    extra={"event": "prompt_revised", "task_id": task_id},
                                )
                                continue
                        self.repository.update_video_task(task_id, status="failed", error_message=str(exc))
                        self.logger.exception(
                            "视频任务失败", extra={"event": "video_failed", "account_id": account.id}
                        )
                        # v0.2.9:failed 也触发 callback —— 让 callback_url 知道最终落点。
                        self._schedule_callback(task_id)
                        return

    async def _run_watermark(self, task_id: str, result: dict, settings: dict) -> None:
        """
        生成成功后异步调 zhuceka 去水印。失败/未启用都不影响主任务状态。
        写入 clean_video_url / clean_error 字段供前端展示。
        """
        if not settings.get("watermark_enabled"):
            return
        uid = (settings.get("watermark_uid") or "").strip()
        key = (settings.get("watermark_key") or "").strip()
        if not (uid and key):
            self.repository.update_video_task(
                task_id, clean_error="未配置 zhuceka uid/key,请在设置面板填写"
            )
            return

        # 优先用 result_url(无水印)→ 备用 backup_result_url → 最后 fallback_result_url(可能带水印)
        source_url = (
            result.get("result_url")
            or result.get("backup_result_url")
            or result.get("fallback_result_url")
            or ""
        )
        if not source_url:
            self.repository.update_video_task(task_id, clean_error="无可用视频链接,跳过去水印")
            return

        try:
            clean_url = await zhuceka_resolve(source_url, uid=uid, key=key)
            self.repository.update_video_task(task_id, clean_video_url=clean_url, clean_error=None)
            self.logger.info(
                "zhuceka 去水印成功", extra={"event": "watermark_succeeded", "task_id": task_id}
            )
        except ZhucekaConfigError as exc:
            self.repository.update_video_task(task_id, clean_error=str(exc))
        except ZhucekaError as exc:
            self.repository.update_video_task(task_id, clean_error=f"去水印失败: {exc}")
            self.logger.warning(
                "zhuceka 去水印失败: %s", exc, extra={"event": "watermark_failed", "task_id": task_id}
            )

    async def shutdown(self) -> None:
        for cancellation in self._cancellations.values():
            cancellation.set()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        # retry-result 任务不强制 cancel —— 让它跑完,免得用户白白浪费
        # 一次浏览器重开 / 风控校验。但等一个上限,避免僵尸卡住关停。
        if self._retry_tasks:
            done, pending = await asyncio.wait(
                list(self._retry_tasks.values()),
                timeout=10,
                return_when=asyncio.ALL_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        # callback 任务同理 —— 关停时让最后一次 retry 发完,免得接收方误以为
        # 任务中断。timeout 给到重试总时间上限(5+25=30s),别让 UI 卡死。
        if self._callback_tasks:
            done, pending = await asyncio.wait(
                list(self._callback_tasks.values()),
                timeout=35,
                return_when=asyncio.ALL_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
