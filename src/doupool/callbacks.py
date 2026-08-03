"""v0.2.9:异步回执调度器。

任务到 terminal 状态(succeeded / failed / retry 后的新 succeeded)后,
服务 POST 一个 JSON 回执到提交时给定的 callback_url,带 task_id / status /
result_url / error_message 等。失败指数退避重试 3 次(0s / 5s / 25s),
最终仍失败把 callback_status='failed' + callback_last_error 写入 DB
留给运维 / 前端排查。
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx

from doupool.db.models import VideoTask


_LOG = logging.getLogger("doupool.callbacks")

# 单次 POST 超时(秒)。外部 URL 经常是云函数冷启动 3-5 秒,留 8 秒缓冲。
_CALLBACK_REQUEST_TIMEOUT = 8.0

# 重试间隔(秒)。指数退避:第 1 次失败 → 等 5s 重试 → 再失败 → 等 25s 重试。
_RETRY_DELAYS = (5.0, 25.0)


@dataclass(frozen=True, slots=True)
class CallbackOutcome:
    """调度结果摘要,留作 metrics / 测试断言用。"""
    attempts: int
    delivered: bool
    last_error: str | None


# repository.update_video_task 的异步壳。service 注入进来,
# 让 dispatcher 解耦 DB —— 单元测试传 mock 即可。
UpdateFn = Callable[..., None]


def build_payload(task: VideoTask) -> dict:
    """对外 POST 的 JSON body。

    字段命名刻意保持 snake_case 不变,跟 yaonieyo callback 兼容
    (下游接收方可能写死 key 名)。
    """
    return {
        "task_id": task.id,
        "status": task.status,
        "result_url": task.result_url,
        "backup_result_url": task.backup_result_url,
        "cover_url": task.cover_url,
        "error_message": task.error_message,
        "conversation_id": task.conversation_id,
        "remote_task_id": task.remote_task_id,
    }


async def _post_once(client: httpx.AsyncClient, url: str, payload: dict) -> None:
    """单次 POST。返回即视为成功;HTTPError / 非 2xx 都抛。"""
    response = await client.post(
        url,
        content=json.dumps(payload, ensure_ascii=False),
        headers={"Content-Type": "application/json", "User-Agent": "DouPool-Callback/0.2.9"},
    )
    response.raise_for_status()


async def dispatch(
    task: VideoTask,
    update: UpdateFn,
    *,
    client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> CallbackOutcome:
    """回执主入口:失败重试,结果写 DB。

    不会抛异常 —— 任何意外都吞掉、转 callback_status='failed' + 错误
    描述写库,避免影响主任务状态(回执失败不能让 succeeded 变成 failed)。
    """
    url = (task.callback_url or "").strip()
    if not url:
        # 没设 callback_url → 任务里 callback_status 应该也是 None / disabled,
        # 跳过整段流程,标 disabled 留痕。
        if task.callback_status != "disabled":
            update(callback_status="disabled", callback_last_error=None)
        return CallbackOutcome(attempts=0, delivered=False, last_error=None)

    # scheme 验证:防止意外被注入 file:// / gopher:// 这种。
    if not (url.startswith("http://") or url.startswith("https://")):
        update(callback_status="failed", callback_last_error=f"unsupported scheme: {url[:20]}")
        return CallbackOutcome(attempts=0, delivered=False, last_error="unsupported scheme")

    payload = build_payload(task)
    update(callback_status="sending", callback_attempts=0, callback_last_error=None)

    def _factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=_CALLBACK_REQUEST_TIMEOUT, follow_redirects=False)

    client_factory = client_factory or _factory
    attempts = 0
    last_error: str | None = None
    # 第一次立刻发;失败则按 _RETRY_DELAYS 退避重试
    delays = (0.0,) + _RETRY_DELAYS

    for delay in delays:
        if delay > 0:
            await asyncio.sleep(delay)
        attempts += 1
        try:
            async with client_factory() as client:
                await _post_once(client, url, payload)
            update(
                callback_status="succeeded",
                callback_attempts=attempts,
                callback_last_error=None,
            )
            _LOG.info(
                "callback 投递成功",
                extra={"event": "callback_delivered", "task_id": task.id, "attempts": attempts},
            )
            return CallbackOutcome(attempts=attempts, delivered=True, last_error=None)
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            update(callback_attempts=attempts, callback_last_error=last_error)
            _LOG.warning(
                "callback 第 %d 次失败:%s",
                attempts,
                last_error,
                extra={"event": "callback_retry", "task_id": task.id},
            )
        except Exception as exc:  # noqa: BLE001 - 任意意外吞掉,不让主任务受牵连
            last_error = f"unexpected: {type(exc).__name__}: {exc}"
            update(callback_attempts=attempts, callback_last_error=last_error)
            _LOG.exception(
                "callback 出现未捕获异常",
                extra={"event": "callback_crashed", "task_id": task.id},
            )

    update(callback_status="failed", callback_last_error=last_error)
    return CallbackOutcome(attempts=attempts, delivered=False, last_error=last_error)