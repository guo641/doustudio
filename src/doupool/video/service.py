from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import re
import threading
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from doupool.db.models import Account, VideoTask, utcnow
from doupool.db.repository import AccountRepository
from doupool.prompt_reviser import FailureKind, classify_failure, revise_prompt
from doupool.prompt_parser import split_by_segment_markers
from doupool import callbacks as callbacks_mod
from doupool.watermark import (
    ZhucekaConfigError,
    ZhucekaError,
    resolve_clean_url as zhuceka_resolve,
)

from .browser import TokenBundleUnavailable
from .cost import quota_cost
from .protocol import DURATIONS, MAX_I2V_IMAGES, MODELS, RATIOS, TASK_MODES, DoubaoContentRejected, DoubaoRateLimited


# v0.2.16:日志 / DB 时间统一按北京时间,跟 OS 时区解耦
SHANGHAI = ZoneInfo("Asia/Shanghai")


class NoAvailableAccount(RuntimeError):
    pass


class _DefaultSettings:
    # v0.2.29:共享池下 _DefaultSettings 不再分 mini/v2/std,统一 shared。
    def get(self):
        return {
            "daily_quota_shared": 50,
            "quota_reset_time": "00:00", "max_concurrency": 1,
        }

    def get_daily_quotas(self):
        return {"shared": 50}


def quota_window(now: datetime, reset_value: str) -> tuple[date, datetime]:
    """v0.2.33:正确处理入参时区 + 输出与 utcnow()/reset 比较口径一致。

    修复链:
    1. 入参时区:之前 `now.replace(tzinfo=UTC).astimezone(SHANGHAI)` 假定入参
       必为 UTC,但 `_run_inner` 实际传 `datetime.now(SHANGHAI)`(local),被错当
       UTC 后再转 Shanghai → local_now 的日期会+8h 漂移到「次日」。
    2. 返回 next_reset 时区:之前 `.astimezone(UTC).replace(tzinfo=None)` 输出
       UTC-naive,但 `reset_daily_quotas` 的 2nd UPDATE 比较 `lu <= utcnow()`
       时,`utcnow()` 实际返的是 SHANGHAI-naive(v0.2.16 改名 / 统一本地时间),
       与 UTC-naive 的 lu 比较 = 错位比较,可能让 mark_account_limited 写的
       cap 在下一秒就被 reset 当成「已过期的封号」清零。
       v0.2.33 改返 SHANGHAI-naive,与 utcnow() 口径一致。
    """
    sh_tz = ZoneInfo("Asia/Shanghai")
    if now.tzinfo is None:
        # 旧调用方传 naive datetime —— 假定是 UTC(向后兼容)
        local_now = now.replace(tzinfo=UTC).astimezone(sh_tz)
    else:
        # 新调用方传 aware datetime —— 正确 astimezone 即可
        local_now = now.astimezone(sh_tz)
    hour, minute = map(int, reset_value.split(":"))
    reset = datetime.combine(local_now.date(), time(hour, minute), sh_tz)
    if local_now < reset:
        business_date = local_now.date() - timedelta(days=1)
        next_reset = reset
    else:
        business_date = local_now.date()
        next_reset = reset + timedelta(days=1)
    # v0.2.33:返 SHANGHAI-naive,与 utcnow()(也是 SHANGHAI-naive)同口径。
    # mark_account_limited 写 DB 时也是这个值 → reset 比较时不会错位。
    return business_date, next_reset.replace(tzinfo=None)


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
        # v0.2.19:删除 _account_locks —— 同账号多 task 现在共享 BrowserContext
        # (PlaywrightVideoRunner 自己维护 per-profile 异步锁做首次 context 创建),
        # 同账号 5 个 mini 10s 视频可以并发跑,共享 50 点 quota。剩下的并发闸门是
        # _global_semaphore(max_concurrency,默认 1)。
        self._global_semaphore: asyncio.Semaphore | None = None
        self._semaphore_limit = 0
        # v0.2.34:任务间隔串行化锁 —— 多个 task 并发进 _run_inner 时各自 sleep
        # 自己的 interval 是无意义的(并行 sleep 一起醒来,race 触发豆包风控)。
        # 用 Lock 把 interval 段串行化:排队等锁的 task 必须等前一个 task
        # sleep 满 interval 才能开始自己的 sleep,真正拉开 dispatch 节奏。
        self._interval_lock = asyncio.Lock()
        # v0.2.29:独立重置 cron —— 即使没有 task 在跑,到 quota_reset_time
        # 也能跨日清桶(原 _run_inner 里 reset_daily_quotas 只在迭代时触发)。
        self._reset_cron_task: asyncio.Task[None] | None = None
        self._reset_cron_stop = asyncio.Event()
        # v0.2.33:start() 路径预扣登记表 —— task_id -> (account_id, cost, model)。
        # _run_inner 的 update() 闭包见到此条目即跳过 increment(避免双重扣);
        # 失败路径退额度走 _refund_pre_charge(task_id) 撤销 start 时的预扣。
        # 进程重启后内存丢失,但 `_run_inner` 重启后会重新 select_account +
        # 自然走 update() 闭包内的 increment(因为 start() 已成功扣过,
        # 重启后 update() 又会再扣一次 → 重复扣)。这里靠 update() 闭包的
        # 「首次进入 generating 才扣」闸门没法识别「曾被预扣」—— 因此
        # resume_queued 的 task 必须先被识别为"内存已失的预扣 task",才能
        # 让 _run_inner 不重复扣。下面 reconcile_pre_charged_after_restart
        # 在 DB 侧持久化标志位,resume_queued 检测后清零预扣。
        self._pre_charged_tasks: dict[str, tuple[str, int, str]] = {}

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
        # v0.2.32:手动重试路径透传原 task 的 group_id,确保新任务仍
        # 归属同一组(结果页按组折叠)。新建路径留 None,由下面按
        # prompt_list 长度自动决定是否打组。
        group_id: str | None = None,
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
        # v0.2.32:caller(手动重试)显式传 group_id 时优先用,沿用原组;
        # 否则按 prompt 数量自决新建组。
        if group_id:
            effective_group_id: str | None = group_id
        elif len(prompt_list) > 1:
            effective_group_id = str(uuid4())
        else:
            effective_group_id = None
        first_task = None
        # v0.2.35:跨账号凑余额 —— 每个 task 独立选 least_used,不再 sticky。
        # 字段 sticky_account_id 保留在代码中作兼容占位,但实际不读(完全去掉
        # 同组粘同账号逻辑)。每个 task 走 choose_and_reserve_account(自带
        # max_attempts=8 重试),选不到再 fallback 到队列(queued,等下次调度)。
        # v0.2.33:start() 路径失败累积 —— 任意一条预扣失败时,把已成功的
        # 预扣回退掉,不让用户看到"任务列表出现但实际没账号"的孤儿。
        pre_charged_to_refund: list[tuple[str, int, str]] = []
        # v0.2.35:逐条 prompt 的 partial_rejected 列表 —— 任一条选不到可用账号
        # 且 caller 未显式指定 account_id 时,把这条 prompt 加进去,最终
        # API 返回 200 OK + partial_rejected 给前端 Toast 提示用户「这几条
        # 暂时排不进,稍候自动重试」。
        partial_rejected: list[dict] = []
        try:
            for index, p in enumerate(prompt_list, start=1):
                cost = quota_cost(model, duration)

                if account_id:
                    # 显式指定:对该账号做 CAS,失败抛 NoAvailableAccount
                    # (v0.2.33 起的语义保留 —— caller 显式锁账号,失败 = 报错)
                    reserved = self._reserve_for_account(
                        account_id, by=cost, daily_quotas=self.settings_service.get_daily_quotas(),
                    )
                    if reserved is None:
                        raise NoAvailableAccount(
                            "无可用账号或配额已用完(v0.2.33 显式指定路径)"
                        )
                else:
                    # v0.2.35:跨账号凑余额 —— 走 choose_and_reserve_account
                    # (least_used + max_attempts=8 CAS 重试)。单次调用本身
                    # 已是「选 + 预扣」的原子组合,失败表示:全账号 shared 桶都
                    # 没空间放 cost,自然 fallback 到 queued。
                    reserved = self.repository.choose_and_reserve_account(
                        self.settings_service.get_daily_quotas(),
                        by=cost,
                        strategy="least_used",
                    )

                if reserved is not None:
                    reserved_account = reserved
                    task = self.repository.create_video_task(
                        reserved_account.id,
                        p,
                        model,
                        ratio,
                        duration,
                        mode=mode,
                        image_paths=image_paths or None,
                        group_id=effective_group_id,
                        group_index=index if effective_group_id else 0,
                        callback_url=callback_url,
                    )
                    # 记录预扣 —— _run_inner 见到就跳过 update() 的二次扣,
                    # 失败路径退款也走 _refund_pre_charge
                    self._pre_charged_tasks[task.id] = (reserved_account.id, cost, model)
                    pre_charged_to_refund.append((reserved_account.id, cost, model))
                    self._schedule(task.id)
                else:
                    # 无可用账号 —— 创建 queued 任务(不绑账号),等 resume_queued
                    # 或下次 _run_inner 看到 status=queued + 无 account_id 时走默认
                    # 「账号全满,保持 queued」分支。同时加入 partial_rejected,
                    # API 返回 200 OK 时给前端 Toast 提示用户「这几条 prompt 暂时
                    # 排不进」,让用户知道发生了什么(之前 v0.2.33 的行为是
                    # 静默 queued,用户看不到原因)。
                    task = self.repository.create_video_task(
                        None, p, model, ratio, duration,
                        mode=mode,
                        image_paths=image_paths or None,
                        group_id=effective_group_id,
                        group_index=index if effective_group_id else 0,
                        callback_url=callback_url,
                    )
                    # v0.2.35:仍调用 _schedule —— _run_inner 跑起来后选不到
                    # 账号就保持 queued,和 v0.2.33 的行为一致。
                    self._schedule(task.id)
                    partial_rejected.append({
                        "index": index,
                        "prompt": p,
                        "reason": "所有账号今日共享额度已用完,任务已入队稍后重试",
                    })
                if first_task is None:
                    first_task = task
            # 全部预扣成功 —— 清空退款队列(失败回滚不再触发)
            pre_charged_to_refund.clear()
            # v0.2.35:跨账号凑余额 —— start() 返回 (first_task, partial_rejected)
            # 二元组给 API 端组装 {task, partial_rejected} 响应。
            return first_task, partial_rejected
        except Exception:
            # 任一条预扣失败 / 异常 —— 回退已成功的预扣,避免孤儿扣款
            for acc_id, by_val, _model in pre_charged_to_refund:
                try:
                    self.repository.decrement_account_quota(
                        acc_id, model=_model, by=by_val,
                    )
                    # 把 _pre_charged_tasks 里的记录撤掉 —— _run_inner 可能
                    # 在 schedule 后立刻跑(已被 asyncio.create_task),保险起见
                    # 也清掉。
                    self._pre_charged_tasks.pop(next(
                        (k for k, v in self._pre_charged_tasks.items()
                         if v[0] == acc_id and v[1] == by_val and v[2] == _model),
                        None,
                    ),
                        None,
                    )
                except Exception:
                    pass
            raise

    def _reserve_for_account(
        self,
        account_id: str,
        *,
        by: int,
        daily_quotas: dict[str, int],
    ) -> Account | None:
        """v0.2.33:对指定账号做 CAS 预扣(sticky / caller 显式指定路径)。

        实现为「选该候选 + CAS」的单次组合 —— 没有竞争者(已经是 sticky / 显式
        指定),不需要 max_attempts 重试;CAS 失败直接返回 None(让上层抛
        NoAvailableAccount)。
        """
        from doupool.db.repository import SHARED_QUOTA_FIELD
        account = Account.get_or_none(Account.id == account_id)
        if account is None or not account.enabled or account.status != "active":
            return None
        now = utcnow()
        if account.video_limited_until is not None and account.video_limited_until > now:
            return None
        quota_limit = int(daily_quotas["shared"])
        field = getattr(Account, SHARED_QUOTA_FIELD)
        rows = (
            Account.update(**{SHARED_QUOTA_FIELD: field + by}, updated_at=now)
            .where(
                (Account.id == account_id)
                & (field + by <= quota_limit)
                & ((Account.video_limited_until.is_null(True))
                   | (Account.video_limited_until <= now))
            )
            .execute()
        )
        if rows != 1:
            return None
        # v0.2.33:顺手 NULL-only 写 date —— 防止同周期 reset 把刚预扣值清 0
        # (sticky 路径同样存在该问题,详见 repository._stamp_quota_date_if_null)。
        self.repository._stamp_quota_date_if_null(account_id)
        return Account.get_by_id(account_id)

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

    def start_reset_cron(self) -> None:
        """v0.2.29:启动独立重置 cron,到 quota_reset_time 跨日清桶。

        必须在 running event loop 里调(FastAPI lifespan / main.py)。多次调用幂等。
        """
        if self._reset_cron_task is not None and not self._reset_cron_task.done():
            return
        self._reset_cron_stop.clear()
        self._reset_cron_task = asyncio.create_task(self._reset_cron_loop())

    async def _reset_cron_loop(self) -> None:
        """v0.2.29:每 60s tick 一次,到 quota_reset_time 触发 reset_daily_quotas。

        `_run_inner` 里的 reset_daily_quotas 保留作双保险 —— 但 cron 是兜底,
        防止「没任务在跑 + 跨日」时清桶逻辑没被触发,导致账号永久卡死。
        """
        try:
            while not self._reset_cron_stop.is_set():
                try:
                    settings = self.settings_service.get()
                    reset_value = settings.get("quota_reset_time", "00:00") or "00:00"
                    business_date, next_reset = quota_window(
                        datetime.now(SHANGHAI), reset_value
                    )
                    now = datetime.now(SHANGHAI).replace(tzinfo=None)
                    if now >= next_reset:
                        # 到了(或刚过)重置点 → 清桶
                        self.repository.reset_daily_quotas(business_date)
                        self.logger.info(
                            "reset_loop tick 命中,清桶 business_date=%s",
                            business_date,
                            extra={"event": "reset_loop_tick", "business_date": str(business_date)},
                        )
                except Exception as exc:
                    self.logger.warning(
                        "reset_loop tick 异常(非致命): %s", exc,
                        extra={"event": "reset_loop_error"},
                    )
                # 用 wait_for + 1min tick,shutdown 时 stop event 唤醒即可退出。
                try:
                    await asyncio.wait_for(self._reset_cron_stop.wait(), timeout=60)
                except asyncio.TimeoutError:
                    pass
        except asyncio.CancelledError:
            raise

    def _refund_pre_charge_if_present(self, task_id: str, *, reason: str) -> None:
        """v0.2.33:兜底 —— 内存里 _pre_charged_tasks 仍记录的预扣撤销。

        `_run_inner` 的失败路径已经在 token bundle / risk_control / 通用
        失败 / 取消分支各自退预扣了,但 **顶层 _run 的 CancelledError / 通用
        Exception 分支** 不在 _run_inner 的 try/except 内 —— 必须在这里
        兜底,否则预扣变孤儿。pop(,None) 幂等;已被 _run_inner 退过的话
        这里直接 noop。

        退路:in-memory pop 拿到 None 时(典型场景:`_run_inner` 在 `runner.run`
        之前已经把 entry pop 出来,但 `runner.run` 立刻抛 CancelledError →
        bubble 到 _run 顶层 handler,此时 _pre_charged_tasks 已无对应 key),
        从 DB 反推 account + 按 cost(model, duration) 重算 by_val,完成退款。
        """
        entry = self._pre_charged_tasks.pop(task_id, None)
        if entry is None:
            entry = self._derive_pre_charge_from_db(task_id)
        if entry is None:
            return
        acc_id, by_val, mdl = entry
        try:
            self.repository.decrement_account_quota(acc_id, model=mdl, by=by_val)
            self.logger.warning(
                "v0.2.33 顶层兜底退预扣 %d 点 (reason=%s): %s",
                by_val, reason, acc_id,
                extra={
                    "event": "video_pre_charge_refunded_top",
                    "account_id": acc_id,
                    "task_id": task_id,
                    "refunded": by_val,
                    "reason": reason,
                },
            )
        except Exception as refund_exc:
            self.logger.exception(
                "v0.2.33 顶层兜底退预扣失败(非致命): %s", refund_exc,
                extra={
                    "event": "video_pre_charge_refund_failed",
                    "account_id": acc_id,
                    "task_id": task_id,
                },
            )

    def _derive_pre_charge_from_db(self, task_id: str):
        """v0.2.33:in-memory map 已被 pop 后,从 DB 反推预扣 entry 用于退款。

        仅供 _refund_pre_charge_if_present 兜底使用:DB 状态对得上预扣场景
        (task 有 account_id + 处于 starting/generating/queued 状态,且桶值
        看起来「刚被扣过 cost」)才返回,否则返 None(让外层 noop,避免误退)。
        """
        try:
            task = self.repository.get_video_task(task_id)
        except Exception:
            return None
        if task is None or not task.account_id:
            return None
        # 已经写到 terminal 状态(failed/succeeded/cancelled)→ _run_inner 的
        # 失败路径已自己处理过退款(走 refund_quota_if_recorded),不需要再退。
        if task.status in {"failed", "succeeded", "cancelled"}:
            return None
        try:
            cost = quota_cost(task.model, int(task.duration))
        except Exception:
            return None
        return (task.account_id, cost, task.model)

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

    # ---------- v0.2.22 Q4 refresh-url 入口 ----------
    def schedule_refresh_url(self, task_id: str) -> asyncio.Task[VideoTask]:
        """v0.2.22:同步语义的重解析,前端 DownloadButton 下载失败时 POST 调用。

        与 retry_result 的差别:
          - retry_result:异步(注册协程后立刻返回),UI 轮询 task 状态,给后台
           工具 / 脚本用。
          - schedule_refresh_url:同步等待(返回的 wrapper await 后才拿到新
           task 行),前端拿新 URL 立即重试下载。

        内部调 runner.recheck_result(deadline=60s),只刷新 result_url /
        backup_result_url / fallback_result_url —— 不动 status、不发
        callback、不跑 watermark、不消耗 quota。
        """
        try:
            task = self.repository.get_video_task(task_id)
        except Exception:
            raise ValueError("任务不存在") from None
        if task is None:
            raise ValueError("任务不存在")
        if task.status != "succeeded":
            raise ValueError("仅 succeeded 任务支持刷新下载链接")
        if not task.conversation_id:
            raise ValueError("任务缺少 conversation_id")
        if task.account_id is None:
            raise ValueError("原账号不可用")
        account = Account.get_or_none(Account.id == task.account_id)
        if account is None or not Path(account.profile_dir).exists():
            raise ValueError("原账号不可用,无法重解析")
        if task_id in self._retry_tasks and not self._retry_tasks[task_id].done():
            raise RuntimeError("已有 retry-result 在运行")
        cancellation = threading.Event()
        self._retry_cancellations[task_id] = cancellation
        self._retry_tasks[task_id] = asyncio.create_task(
            self._refresh_url_body(task_id, account.profile_dir, cancellation)
        )
        return self._retry_tasks[task_id]

    async def _refresh_url_body(
        self, task_id: str, profile_dir: str, cancellation: threading.Event
    ) -> VideoTask:
        """v0.2.22 Q4:refresh-url 后台协程 —— 只刷 result_url 系列字段,
        保留 status / watermark / callback 不变。

        错误兜底:同 _retry_result_body,失败时不动 status(避免覆盖旧
        succeeded),仅在 error_message 留痕 —— 旧 result_url 仍可下载。
        """
        try:
            task = self.repository.get_video_task(task_id)
        except Exception:
            return None
        if task is None:
            return None
        try:
            # recheck_result 已共用 _get_shared_context,serialized by profile_dir lock
            result = await self.runner.recheck_result(
                profile_dir,
                task.conversation_id or "",
                lambda **values: None,  # 不刷 UI status,纯后台
                cancellation,
                deadline_seconds=60,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.repository.update_video_task(
                task_id,
                error_message=f"刷新下载链接失败:{exc}",
            )
            self.logger.warning(
                "event=video_refresh_url_failed task_id=%s error=%s",
                task_id, exc,
                extra={"event": "video_refresh_url_failed", "task_id": task_id},
            )
            return None
        if result is None:
            self.repository.update_video_task(
                task_id,
                error_message="刷新下载链接超时,远端尚未生成完成",
            )
            return None
        # 拿到新 result:只写 result_url 系列字段,不动 status / watermark。
        self.repository.update_video_task(
            task_id,
            result_url=result.get("result_url"),
            backup_result_url=result.get("backup_result_url"),
            fallback_result_url=result.get("fallback_result_url"),
            error_message=None,
        )
        self.logger.info(
            "event=video_refresh_url_succeeded task_id=%s",
            task_id,
            extra={"event": "video_refresh_url_succeeded", "task_id": task_id},
        )
        try:
            return self.repository.get_video_task(task_id)
        except Exception:
            return None
        finally:
            self._retry_tasks.pop(task_id, None)
            self._retry_cancellations.pop(task_id, None)

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

    # ---------- v0.2.35:一键清除(批量 + 退额度) ----------
    _CLEAR_COMPLETED_STATUSES: tuple[str, ...] = ("succeeded", "failed", "cancelled")

    def clear_tasks(self, target: str) -> int:
        """v0.2.35:一键清除任务。

        target:
          - "completed" —— 清 succeeded / failed / cancelled
          - "queued"    —— 清 queued(running 状态绝对不动,防打断正在生成)

        对每条任务:如果还在 `_pre_charged_tasks` 里 → 走预扣退路(顶层退);
        否则无额度顾虑。退完调用 repository.delete_video_tasks_by_ids 物理删。

        返回实际删除的任务数。
        """
        if target == "completed":
            statuses = self._CLEAR_COMPLETED_STATUSES
        elif target == "queued":
            statuses = ("queued",)
        else:
            raise ValueError(f"未知清除目标: {target!r}")

        tasks = self.repository.list_video_tasks_by_statuses(statuses)
        if not tasks:
            return 0

        refunded_count = 0
        refunded_total = 0
        for task in tasks:
            # 优先 _pre_charged_tasks 在内存里有记录 —— start() 预扣过;
            # 否则查 DB(进程重启后内存失的孤儿预扣)。
            entry = self._pre_charged_tasks.pop(task.id, None)
            if entry is None:
                entry = self._derive_pre_charge_from_db(task.id)
            if entry is None:
                continue
            acc_id, by_val, _mdl = entry
            try:
                self.repository.decrement_account_quota(acc_id, model=_mdl, by=by_val)
                refunded_count += 1
                refunded_total += by_val
            except Exception as exc:  # noqa: BLE001 —— 退额度失败不阻断清任务
                self.logger.warning(
                    "v0.2.35 批量清除退额度失败 task=%s acc=%s err=%s",
                    task.id, acc_id, exc,
                    extra={
                        "event": "video_clear_refund_failed",
                        "task_id": task.id,
                        "account_id": acc_id,
                        "error": str(exc),
                    },
                )

        deleted = self.repository.delete_video_tasks_by_ids([t.id for t in tasks])
        self.logger.info(
            "v0.2.35 批量清除 target=%s deleted=%d refunded=%d (total %d 点)",
            target, deleted, refunded_count, refunded_total,
            extra={
                "event": "video_tasks_cleared",
                "target": target,
                "deleted_count": deleted,
                "refunded_count": refunded_count,
                "refunded_total": refunded_total,
            },
        )
        return deleted

    def clear_results(self, *, downloaded_only: bool = False) -> int:
        """v0.2.35:一键清除结果(只动 succeeded)。

        downloaded_only=True  —— 只清 `clean_video_url OR result_url` 不为 NULL 的
                                  (用户已经下载过 / 有可用 URL 的任务)
        downloaded_only=False —— 清全部 succeeded

        succeeded 任务在生成成功时已结算过额度,无需退额度(豆包扣过的已经扣过)。
        只删 DB row,本地视频文件保留(用户已经下到本地的归用户管)。
        """
        tasks = self.repository.list_succeeded_results(
            with_download_url=True if downloaded_only else None,
        )
        if not tasks:
            return 0
        deleted = self.repository.delete_video_tasks_by_ids([t.id for t in tasks])
        self.logger.info(
            "v0.2.35 批量清除结果 downloaded_only=%s deleted=%d",
            downloaded_only, deleted,
            extra={
                "event": "video_results_cleared",
                "downloaded_only": downloaded_only,
                "deleted_count": deleted,
            },
        )
        return deleted

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
            # v0.2.19:recheck_result 现在是 async(共享 BrowserContext),直接 await,
            # 不再走 asyncio.to_thread。
            result = await self.runner.recheck_result(
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
        # v0.2.33:重启后恢复 _pre_charged_tasks —— start() 的 CAS 预扣已落 DB,
        # 但内存失;如果不在 _pre_charged_tasks 重建记录,_run_inner 会走
        # update() 闭包的 increment 分支,造成二次扣款。in-flight 任务 = 任何
        # 还可能被 _schedule 拉起的 status(queued/starting/generating) +
        # account_id 非空的行 —— 这些都是 start() 走通后被 schedule 的,
        # 对应一次成功的 CAS 预扣。
        recovered = self._reconcile_pre_charged_after_restart()
        if recovered:
            self.logger.info(
                "v0.2.33:重启恢复预扣登记表 %d 条", recovered,
                extra={"event": "video_pre_charge_recovered", "count": recovered},
            )
        # v0.2.30:启动 sanitize —— 先把"卡 queued 太久"的任务标 failed。
        #
        # 历史 bug:被 cancel 的任务会写 status=queued + error="应用已停止,
        # 等待下次继续"(v0.2.27 之前),lifespan 调 resume_queued 试图
        # 拉起,但 cancel 信号很快又命中 _run 的 CancelledError 分支,
        # 又写回 queued,死循环。v0.2.30 Fix B1 已把 CancelledError 也写
        # failed,但万一未来又有人不小心写 queued,启动这个兜底能保证
        # 用户不会看到「永远卡 queued」的任务。
        #
        # stale 阈值 30min:正常任务的 "queued → starting" 转换发生在
        # _schedule 创建 asyncio task 后 1ms 内,任何超过 30min 还在
        # queued 的,都是被 cancel / 异常打断留下的孤儿。
        stale_count = self._sanitize_stale_queued_tasks()
        if stale_count:
            self.logger.warning(
                "v0.2.30:resume_queued 把 %d 条 stale queued 任务标 failed",
                stale_count,
                extra={"event": "video_stale_queued_sanitized", "count": stale_count},
            )
        for task in self.repository.list_queued_video_tasks():
            self._schedule(task.id)

    def _sanitize_stale_queued_tasks(
        self, *, stale_threshold_seconds: int = 1800
    ) -> int:
        """v0.2.30:把"卡 queued 太久"的任务标 failed。返处理条数。

        阈值 30min 默认值,可在测试里调小覆盖。

        实现细节:peewee 默认从 SQLite 读 DateTimeField 返 ISO str,
        不能直接跟 Python datetime 比较。所以过滤放在 SQL 表达式层
        (VideoTask.updated_at < cutoff WHERE status='queued'),
        只把命中的 stale 任务 hydrate 出来再逐条 mark failed。
        """
        try:
            cutoff = datetime.now(SHANGHAI).replace(tzinfo=None) - timedelta(
                seconds=stale_threshold_seconds
            )
            # 直接走 peewee 表达式:status='queued' AND updated_at < cutoff。
            # 只 hydrate 命中行,避免把 fresh queued 一次性加载到内存。
            stale_tasks = list(
                VideoTask.select().where(
                    (VideoTask.status == "queued")
                    & (VideoTask.updated_at < cutoff)
                )
            )
        except Exception:
            self.logger.exception(
                "stale queued 查询失败,跳过 sanitize",
                extra={"event": "stale_queued_sanitize_failed"},
            )
            return 0

        count = 0
        for task in stale_tasks:
            try:
                self.repository.assign_video_task(task.id, None)
                self.repository.update_video_task(
                    task.id,
                    status="failed",
                    error_message=(
                        "启动时清理:任务在 queued 状态卡住超过 "
                        f"{stale_threshold_seconds // 60} 分钟,可能上次"
                        "应用退出时被中断,已自动作废,请重新提交"
                    ),
                    completed_at=datetime.now(SHANGHAI).replace(tzinfo=None),
                )
                count += 1
            except Exception:
                self.logger.exception(
                    "stale queued 标 failed 失败: %s",
                    task.id,
                    extra={"task_id": task.id},
                )
        return count

    def _reconcile_pre_charged_after_restart(self) -> int:
        """v0.2.33:进程重启后恢复 _pre_charged_tasks。

        start() 的 CAS 预扣已经写进 DB(used_shared += cost),进程崩溃 / 异常
        退出后内存失。如果不在 _pre_charged_tasks 重建这些条目,_run_inner
        的 update() 闭包会走 increment 分支,在 generating 时二次扣款。

        实现:扫 status in (queued/starting/generating) 且 account_id 非空的
        task —— 这些都是曾经 start() 走通的,在 DB 里留了一次成功的 CAS。
        用 (model, duration) 重算 cost,回填到 _pre_charged_tasks。

        只动内存里的 _pre_charged_tasks;不动 DB 的 used_shared(start() 预扣
        已经精确写入,重启后再改 DB 等于猜测业务状态,可能踩跨 reset / 手动
        重置的边界)。allowed_used vs actual_used 的偏差如果出现,留给日志告警
        或手动对账处理。
        """
        try:
            in_flight = list(
                VideoTask.select().where(
                    VideoTask.status.in_(("queued", "starting", "generating"))
                    & VideoTask.account.is_null(False)
                )
            )
        except Exception:
            self.logger.exception(
                "reconcile 查询 in-flight 失败,跳过",
                extra={"event": "pre_charge_reconcile_failed"},
            )
            return 0
        count = 0
        for task in in_flight:
            try:
                cost = quota_cost(task.model, int(task.duration))
            except Exception:
                self.logger.exception(
                    "reconcile 计算 cost 失败: %s", task.id,
                    extra={"task_id": task.id},
                )
                continue
            # 存 account_id(str)保持和 start() 路径一致(update() 闭包需要)
            self._pre_charged_tasks[task.id] = (task.account.id, cost, task.model)
            count += 1
        return count

    async def _run(self, task_id: str, cancellation: threading.Event) -> None:
        try:
            await self._run_inner(task_id, cancellation)
        except asyncio.CancelledError:
            # v0.2.30:行为修正 —— 取消时直接标 failed,不再写 queued。
            #
            # 原先「queued + 应用已停止,等待下次继续」的设计假设重启后
            # resume_queued 会拉起,但实测两处 bug 会让任务永久卡 queued:
            #   (a) lifespan 启动 resume_queued → _schedule → asyncio.create_task
            #       → 几乎立刻被 uvicorn / webview shutdown 的 cancel 信号命中
            #       → 又走回这条 CancelledError 分支 → 写回 queued → 死循环
            #       (DB 实证:task 9830ed7c 17:53 提交,19:11 还卡 queued,
            #        updated_at 反复被自己刷)。
            #   (b) _run_inner 已经处理了 cancellation.is_set() 路径(写 failed
            #       + 退额度),但只有「runner.run 抛 Exception 后命中 is_set」
            #       才走那条;若 cancel 直接打断 await asyncio.sleep / DB 写,
            #       CancelledError 会跳过 _run_inner 的 except 直接冒到这里。
            #
            # 改成 failed + 「应用已停止,任务已取消」让用户明确知道已作废。
            # v0.2.33:start() 预扣场景下要退预扣,避免孤儿扣款。绝大多数
            # cancel 发生在 runner.run 启动前 / 立刻,quota 未扣,但已经
            # 在 start() 时预扣过 —— 这里按预扣值退。
            self._refund_pre_charge_if_present(task_id, reason="cancel")
            try:
                self.repository.assign_video_task(task_id, None)
                self.repository.update_video_task(
                    task_id,
                    status="failed",
                    error_message="应用已停止，任务已取消",
                    completed_at=datetime.now(SHANGHAI).replace(tzinfo=None),
                )
            except Exception:
                # DB 写不动也无所谓 —— shutdown 阶段本来就要退出,日志留给
                # 下次启动时 resume_queued 的 stale 兜底(fix B2)清理。
                self.logger.exception(
                    "CancelledError 分支写 failed 失败,留给 stale 兜底",
                    extra={"task_id": task_id},
                )
            self.logger.info(
                "应用停止,任务取消(CancelledError 顶层): %s", task_id,
                extra={"event": "video_cancelled_by_shutdown", "task_id": task_id},
            )
            raise
        except Exception as exc:
            # 顶层兜底:即使配额/账号选择/数据库出意外,也要把任务
            # 推进到一个 terminal 状态,绝不让它卡在 queued / starting
            # 永远不被前端看到。
            # v0.2.33:同 CancelledError,start() 预扣必须退。
            self._refund_pre_charge_if_present(task_id, reason="runner_crashed")
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
            business_date, next_reset = quota_window(datetime.now(SHANGHAI), settings["quota_reset_time"])
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
                    # v0.2.29:共享池下不再按 model 拆 quota,改成"共享额度"提示。
                    msg = "全部账号今日共享额度已用完,等到下一个额度重置时间会自动恢复"
                else:
                    msg = "等待可用账号"
                self.repository.update_video_task(task_id, status="queued", error_message=msg)
                await asyncio.sleep(self.account_poll_interval)
                continue
            self.repository.assign_video_task(task_id, account.id)
            # v0.2.34:任务间隔 —— 每个 task 在抢 _global_semaphore 之前 sleep
            # N 秒,避免多账号同时 dispatch 触发豆包风控。
            # 必须放在 semaphore 之外:放里面只 sleep 当前 task,不真正"间隔"。
            # 放在 assign 之后:账号绑定已落库,cancel 信号触发时也能通过
            # cancellation.is_set() 优雅退出,不浪费已经选的账号。
            interval = float(settings.get("task_interval_seconds", 0))
            if interval > 0:
                # 多个 task 并发进 _run_inner 时各自 sleep 自己的 interval
                # 是无意义的——并行 sleep 一起醒来,失去间隔效果。用全局
                # _interval_lock 把 interval 段串行化:排队等锁的 task 必须
                # 等前一个 task sleep 满 interval 才能开始自己的 sleep。
                # 等锁期间也要响应 cancellation.set():别让关停时被锁卡住。
                while True:
                    if cancellation.is_set():
                        return
                    try:
                        await asyncio.wait_for(
                            self._interval_lock.acquire(),
                            timeout=0.1,
                        )
                        break
                    except asyncio.TimeoutError:
                        continue
                try:
                    if cancellation.is_set():
                        return
                    # cancellation 是 threading.Event(不是 asyncio.Event),
                    # 它的 wait() 是同步阻塞调用、不能 await。用 to_thread
                    # 把它丢到工作线程,既能阻塞等满 interval 秒、又能在
                    # cancellation.set() 时立即返回 True。
                    cancelled = await asyncio.to_thread(
                        cancellation.wait, timeout=interval
                    )
                    if cancelled:
                        return
                finally:
                    self._interval_lock.release()
            # v0.2.19:删除 _account_locks —— 同账号多 task 现在共享 BrowserContext,
            # 锁反而阻塞并发(50 点账号一次只能跑 1 个 task 是 bug)。剩下
            # _global_semaphore 限制全局并发数(max_concurrency,默认 1)。
            async with self._global_semaphore:
                if cancellation.is_set():
                    return
                self.repository.update_video_task(task_id, status="starting", error_message=None)
                quota_recorded = False
                recorded_cost = 0
                # v0.2.33:start() 已预扣 → 此处跳过 increment。失败路径退款
                # 仍走 refund_quota_if_recorded()(recorded_cost 已设 = 预扣值)。
                pre_charged_entry = self._pre_charged_tasks.pop(task_id, None)

                def update(**values) -> None:
                    nonlocal quota_recorded, recorded_cost
                    if values.get("status") == "generating" and not quota_recorded:
                        # v0.2.11:按 model + duration 算 cost,扣对应桶。
                        # 非法 duration 走兜底 max(1, duration)。
                        # v0.2.33:start() 预扣 → 此处不再 increment(避免双重扣)。
                        recorded_cost = quota_cost(task.model, int(task.duration))
                        if pre_charged_entry is None:
                            # v0.2.33 兼容路径:resume_queued 启动后,内存里的
                            # _pre_charged_tasks 已失(task 没经过 start()),需要
                            # 在 _run_inner 首次进入 generating 时按老规则扣。
                            self.repository.increment_account_quota(
                                account.id, model=task.model, by=recorded_cost
                            )
                        else:
                            # v0.2.33:start() 已预扣 → 通常跳过 increment。
                            # 但 _run_inner 每轮会跑 reset_daily_quotas(business_date),
                            # 新账号 video_quota_date=NULL 时会命中 reset 把它清零。
                            # 此时 used_shared 已掉到 0,但预扣在 reset 之前已完成 →
                            # 这里补扣一次,把预扣值补回,保证最终桶值正确。
                            # 并发场景:同账号另一条 runner 也可能正在 reset,
                            # 但 reset 只对 video_quota_date=NULL 账号生效一次,
                            # date 写 today 后后续 noop → 各任务只补一次。
                            if account.video_quota_used_shared < recorded_cost:
                                # 用 pre_charged_entry 的 cost(start() 时预扣的真值)
                                # 而不是 recorded_cost —— 因为 reset_daily_quotas 会
                                # 清零已预扣的桶,而 recorded_cost 是 task 自身 cost,
                                # 两者只在 task.duration/model 一致时才相等;但同组
                                # 任务可能 model/duration 不一,这里取预扣的真值更稳。
                                recover_by = pre_charged_entry[1]
                                self.repository.increment_account_quota(
                                    account.id, model=task.model, by=recover_by
                                )
                                self.logger.warning(
                                    "v0.2.33:reset_daily_quotas 清零后补回预扣 %d",
                                    recover_by,
                                    extra={
                                        "event": "video_pre_charge_recover",
                                        "task_id": task_id,
                                        "account_id": account.id,
                                        "recovered": recover_by,
                                    },
                                )
                            else:
                                self.logger.debug(
                                    "v0.2.33:start() 预扣已扣,update() 跳过 increment",
                                    extra={"event": "video_pre_charge_skip", "task_id": task_id},
                                )
                        quota_recorded = True
                    self.repository.update_video_task(task_id, **values)

                def refund_quota_if_recorded() -> None:
                    # v0.2.19:失败退还额度(仅本 runner 调用的扣款)。
                    # 同一个 except 块调用多次幂等 —— recorded_cost = 0 后再调
                    # 直接 noop。Bucket 下界由 repository.decrement_account_quota
                    # 用 max(0, used-by) 保证。
                    # v0.2.33:start() 已预扣 → 若 update(generating) 没跑过
                    # (recorded_cost 仍是 0,常见于 runner 提前抛异常 / cancel
                    # 信号触发),但 pre_charged_entry 已记录了预扣值 → 这里
                    # 也要退,否则桶里就多了 5 点孤儿扣款。所有失败路径走同
                    # 一处退款,避免漏退。
                    nonlocal quota_recorded, recorded_cost, pre_charged_entry
                    # 决策退多少:update() 走过 → 按 recorded_cost 退;没走过但
                    # 是 start() 预扣路径 → 按 pre_charged_entry 的值退。
                    refund_by = 0
                    if quota_recorded and recorded_cost > 0:
                        refund_by = recorded_cost
                    elif pre_charged_entry is not None:
                        # 兼容路径:resume_queued 启动后,内存里的 _pre_charged_tasks
                        # 已失,task 没经过 start() 的预扣 → pre_charged_entry 这里是 None,
                        # 走不到这条分支。
                        # 当前走到这里:start() 已预扣,但 update(generating) 没机会
                        # 触发(runner 早抛 / cancel),需要退预扣。
                        refund_by = pre_charged_entry[1]
                    if refund_by > 0:
                        try:
                            self.repository.decrement_account_quota(
                                account.id, model=task.model, by=refund_by
                            )
                            self.logger.warning(
                                "失败退还额度 %d 点 (model=%s): %s",
                                refund_by, task.model, account.id,
                                extra={
                                    "event": "video_quota_refunded",
                                    "account_id": account.id,
                                    "task_id": task_id,
                                    "refunded": refund_by,
                                },
                            )
                        except Exception as refund_exc:
                            # 退款本身失败不能阻断主流程 —— 写日志,后续
                            # 用户看账期对账时可能能发现问题。
                            self.logger.exception(
                                "失败退还额度出错(非致命): %s",
                                refund_exc,
                                extra={
                                    "event": "video_quota_refund_failed",
                                    "account_id": account.id,
                                    "task_id": task_id,
                                },
                            )
                        # 标记「已退过」,避免同 except 块多次退。
                        quota_recorded = False
                        recorded_cost = 0
                        pre_charged_entry = None

                try:
                    image_paths = []
                    if task.image_paths:
                        try:
                            image_paths = json.loads(task.image_paths)
                        except json.JSONDecodeError:
                            image_paths = []
                    # v0.2.19:runner.run 现在是 async(共享 BrowserContext),
                    # 直接 await,不再走 asyncio.to_thread。
                    # v0.2.22 Q1:max_reject_retries 透传给 runner.run,0 关闭
                    # 改写重试(沿用 v0.2.21 默认);>0 时 runner 内部 catch
                    # DoubaoContentRejected 后用 prompt_reviser 改写 prompt 在
                    # 同一 page 重提交。retry 期间 update("generating") 仍由
                    # runner 触发,quota_recorded 闸门只扣一次。
                    # v0.2.22 Q2:window_visible 透传给 runner.run →
                    # _get_shared_context,决定 BrowserContext 首次创建时
                    # Chromium 窗口是否显示在桌面。
                    max_reject_retries = max(0, min(3, int(settings.get("max_reject_retries", 0) or 0)))
                    runner_window_visible = bool(settings.get("runner_window_visible", False))
                    result = await self.runner.run(
                        account.profile_dir,
                        task.prompt,
                        task.model,
                        task.ratio,
                        task.duration,
                        update,
                        cancellation,
                        mode=getattr(task, "mode", None) or "t2v",
                        image_paths=image_paths or None,
                        # v0.2.17:settings 里的 pc_version(默认 "3.27.4")
                        # 透传给 runner.run → load_browser_context,塞进
                        # payload.client_meta.pc_version。
                        pc_version=settings.get("pc_version"),
                        max_reject_retries=max_reject_retries,
                        window_visible=runner_window_visible,
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
                except TokenBundleUnavailable as exc:
                    # v0.2.17:profile 里抽不到 web_id(冷启动 profile 没让 WebMSSDK
                    # 跑过 / msToken 已过期)。不是 quota 问题,不要 cap 桶。
                    # 写清楚「请去浏览器访问主页 + 点刷新 token」,让用户自救。
                    # 直接 return:token 是 profile 级问题,重提同一 profile 必失败,
                    # 不要被外层 while not cancellation.is_set() 死循环重试。
                    # v0.2.33:start() 已预扣 → refund_quota_if_recorded()
                    # 已统一处理 recorded_cost=0 / pre_charged_entry 兜底退款。
                    refund_quota_if_recorded()
                    self.repository.update_video_task(
                        task.id,
                        status="failed",
                        error_message=(
                            "profile 中缺少 web_id,请在浏览器里访问 "
                            "https://www.doubao.com/chat/ 主页 5-10 秒后"
                            "点「刷新 token」(token 过期 / 冷启动 profile 都需要)"
                        ),
                    )
                    self.logger.warning(
                        "event=video_token_bundle_unavailable account_id=%s task_id=%s error=%s",
                        account.id,
                        task.id,
                        exc,
                    )
                    # token 是 profile 级问题,不是账号级 — 不 cap 桶、不改 limited_until,
                    # 让账号继续可调度,只是这条 task 标失败。下条 task 仍可能撞同样问题,
                    # 直到用户去点刷新 token。
                    return
                except DoubaoContentRejected as exc:
                    # v0.2.21:豆包在 chain 响应里给了真人能看到的「侵权/违规/换个
                    # 主题」等拒绝文案(protocol.parse_creation_result 兜底扫描到
                    # 命中)。之前这条路径会让 polling 一直返回 None,直到
                    # runner.timeout 5min 才抛「视频生成超时」,期间用户看任务
                    # 永远「生成中」。现在立即标 failed + 退还额度 + 触发
                    # callback。**不**触发 prompt 改写重试 —— 同 prompt 必拒,
                    # 改写反而浪费额度再撞同样的 reject(用户的真实反馈:
                    # 「加拒绝识别,不用调阈值」)。
                    refund_quota_if_recorded()
                    response_excerpt = (exc.response_text or "").replace("\r\n", " ")[:500]
                    self.repository.update_video_task(
                        task_id,
                        status="failed",
                        error_message=f"豆包拒绝:{exc.error_message}",
                        completed_at=datetime.now(SHANGHAI).replace(tzinfo=None),
                    )
                    self.logger.warning(
                        "event=video_content_rejected account_id=%s task_id=%s "
                        "reason=%s | response=%s",
                        account.id, task_id, exc.error_message, response_excerpt,
                        extra={
                            "event": "video_content_rejected",
                            "account_id": account.id,
                            "task_id": task_id,
                        },
                    )
                    self._schedule_callback(task_id)
                    return
                except DoubaoRateLimited as exc:
                    # v0.2.16:豆包把所有拦截都报 "rate limited",但
                    # extra.decision.from == "shark_admin" 是风控拦截,
                    # 不是 quota 限流 — 风控经常很快就放,不该 cap 桶封号。
                    response_excerpt = (exc.response_text or "").replace("\r\n", " ")[:500]
                    if exc.is_risk_control:
                        # 风控:不动桶,只标 task failed,让用户知道是被风控。
                        # self.repository.assign_video_task(task_id, None) 让
                        # 任务回到 queued,下次调度可能会选别的账号重试。
                        # v0.2.33:start() 已预扣 → refund_quota_if_recorded()
                        # 已统一处理 recorded_cost=0 / pre_charged_entry 兜底退款。
                        refund_quota_if_recorded()
                        self.repository.update_video_task(
                            task_id, status="failed",
                            error_message="账号被风控拦截（shark_admin verify），稍后重试或换号",
                            completed_at=datetime.now(SHANGHAI).replace(tzinfo=None),
                        )
                        self.repository.assign_video_task(task_id, None)
                        self.logger.warning(
                            "账号被风控拦截,任务标 failed 不阻塞账号: %s | response=%s",
                            exc, response_excerpt,
                            extra={"event": "video_risk_control", "account_id": account.id},
                        )
                        return  # 任务失败,不再 continue 重试这一条
                    # 真 quota 限流:cap 桶 + 封号 limited_until + 任务回 queued 等明天
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
                    self.logger.warning(
                        "账号今日视频额度已用完:%s | response=%s",
                        exc, response_excerpt,
                        extra={"event": "video_quota_limited", "account_id": account.id},
                    )
                    continue
                except Exception as exc:
                    if cancellation.is_set():
                        # v0.2.27:行为变更 —— 用户主动取消也退还额度。
                        # 取消 ≠ 任务成功,豆包即便已部分生成,用户没拿到视频就
                        # 算失败,不应扣 quota。与 NETWORK/POLICY/INVALID/TIMEOUT
                        # 退款是同一条「失败 = 退」语义路径。
                        #
                        # v0.2.30:行为修正 —— 取消时直接标 failed,不再写 queued。
                        # 原先「queued + 应用已停止,等待下次继续」的设计假设重启后
                        # resume_queued 会拉起,但实际 lifespan 时机 / 用户关掉程序
                        # 按钮时 cancellation 被设置,任务永久卡 queued,前端没有
                        # queued 文案,显示成原始 status 字符串「queued」让用户误以为
                        # 还在生成中。改成 failed + 「应用已停止,任务已取消」,
                        # 用户明确知道已作废,需要重新提交;同时不丢账号 (assign None)。
                        refund_quota_if_recorded()
                        self.repository.assign_video_task(task_id, None)
                        self.repository.update_video_task(
                            task_id,
                            status="failed",
                            error_message="应用已停止，任务已取消",
                            completed_at=datetime.now(SHANGHAI).replace(tzinfo=None),
                        )
                        self.logger.info(
                            "应用停止,任务取消并退还额度: %s", task_id,
                            extra={"event": "video_cancelled_by_shutdown", "task_id": task_id},
                        )
                        return
                    # 分类失败 → 决定是否改写 prompt 后重试
                    failure = classify_failure(str(exc))
                    # v0.2.19:网络异常 / 政策违规 / 无效输入 → 退还本 runner
                    # 已扣的额度。GENERATION_FAILED / UNKNOWN / RATE_LIMITED
                    # 不退 —— 前两类豆包大概率已计费,后者本来就是配额问题,
                    # 已经在上面 mark_account_limited 路径里处理。
                    # v0.2.27:加入 TIMEOUT —— 本地 deadline 等不到结果 = 用户
                    # 没拿到视频,应退。
                    # v0.2.33:start() 已预扣 → 失败路径**无条件**退 recorded_cost,
                    # 不再按 failure.kind 筛选。理由:预扣阶段已经把额度从账号扣走,
                    # 用户没拿到视频就该退,只让"豆包大概率已计费"的 kind 退
                    # (GENERATION_FAILED / UNKNOWN) 反而会留下预扣孤儿。
                    # 已退过的 recorded_cost=0 → refund_quota_if_recorded() 守门 noop。
                    refund_quota_if_recorded()

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
        # v0.2.29:先停 reset_cron —— 否则 _reset_cron_loop 在 stop.wait() 等不到
        # 时,下面的 cancel() 会直接打断 asyncio.wait_for 抛 CancelledError。
        self._reset_cron_stop.set()
        if self._reset_cron_task is not None:
            self._reset_cron_task.cancel()
            try:
                await self._reset_cron_task
            except (asyncio.CancelledError, Exception):
                pass
            self._reset_cron_task = None
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
        # v0.2.19:关掉 Playwright 共享 BrowserContext(避免进程退出时 Chromium 残留)。
        if hasattr(self.runner, "close"):
            try:
                await self.runner.close()
            except Exception as exc:
                self.logger.warning(
                    "runner.close() 异常: %s", exc,
                    extra={"event": "runner_close_error"},
                )
