"""
zhuceka 去水印客户端

调用 https://api.zhuceka.cn/home/api?type=dsp&uid=<uid>&key=<key>&url=<share_url>
返回 data.video = 无水印直链。

典型返回结构(2026):
{
  "code": 200,
  "msg": "success",
  "data": {
    "title": "...",
    "cover": "https://...",
    "video": "https://...",
    "images": [...],
    "live_photo": null
  }
}
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx


logger = logging.getLogger("doustudio.watermark.zhuceka")


ZHUCEKA_ENDPOINT = "https://api.zhuceka.cn/home/api"
DEFAULT_TIMEOUT_SECONDS = 20.0
RETRY_DELAYS_SECONDS: tuple[float, ...] = (0, 5, 10, 20, 30)


class ZhucekaError(RuntimeError):
    """zhuceka 调用失败 / 返回异常时抛出"""


class ZhucekaConfigError(ZhucekaError):
    """未配置 uid 或 key"""


class ZhucekaResponseError(ZhucekaError):
    """HTTP 200 但业务失败 / 字段缺失"""


def _extract_video_url(payload: Any) -> str | None:
    """
    从返回 JSON 里取出无水印视频直链。
    - 成功: payload["data"]["video"]
    - 兜底: 任意字符串字段看起来像 .mp4 / .m3u8 URL 也认
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("code") not in (200, "200", 0, "0"):
        msg = payload.get("msg") or "zhuceka 接口返回非成功状态"
        raise ZhucekaResponseError(f"{msg} (code={payload.get('code')})")

    data = payload.get("data")
    if isinstance(data, dict):
        video = data.get("video")
        if isinstance(video, str) and video.startswith(("http://", "https://")):
            return video

    # 兜底遍历
    for value in _walk(payload):
        if isinstance(value, str) and value.startswith(("http://", "https://")):
            lower = value.lower()
            if any(ext in lower for ext in (".mp4", ".m3u8", "video_mp4", "video/mp4", "format=mp4")):
                return value
    return None


def _walk(payload: Any):
    """递归 yield 所有 dict/list 节点的值,避免无限递归"""
    if isinstance(payload, dict):
        for v in payload.values():
            yield v
            yield from _walk(v)
    elif isinstance(payload, list):
        for item in payload:
            yield item
            yield from _walk(item)


async def resolve_clean_url_once(
    video_url: str,
    *,
    uid: str,
    key: str,
    endpoint: str = ZHUCEKA_ENDPOINT,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    """单次调用 zhuceka,成功返回无水印直链,失败抛 ZhucekaError。"""
    if not (uid and key):
        raise ZhucekaConfigError("未配置 zhuceka uid 或 key,请在设置面板填写")
    if not video_url:
        raise ZhucekaError("video_url 不能为空")

    params = {"type": "dsp", "uid": uid, "key": key, "url": video_url}
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
    }
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
        response = await client.get(endpoint, params=params, headers=headers)
    if response.status_code != 200:
        snippet = response.text[:160].replace("\n", " ")
        raise ZhucekaError(f"zhuceka 接口 HTTP {response.status_code}: {snippet}")

    try:
        payload = response.json()
    except ValueError as exc:
        snippet = response.text[:160].replace("\n", " ")
        raise ZhucekaResponseError(f"zhuceka 返回非 JSON: {snippet}") from exc

    video = _extract_video_url(payload)
    if not video:
        raise ZhucekaResponseError(f"zhuceka 响应缺少 video 字段: {payload}")
    return video


async def resolve_clean_url(
    video_url: str,
    *,
    uid: str,
    key: str,
    retries: int | None = None,
) -> str:
    """
    带退避重试的 zhuceka 调用。retries=None 时按 RETRY_DELAYS_SECONDS 全跑一遍。
    可重试的错误: 网络异常 / 5xx / 非成功 code。
    不可重试: ZhucekaConfigError(用户没配 key)。
    """
    delays = RETRY_DELAYS_SECONDS[: retries if retries is not None else len(RETRY_DELAYS_SECONDS)]
    last_error: Exception | None = None
    for attempt, delay in enumerate(delays, start=1):
        if delay:
            await asyncio.sleep(delay)
        try:
            return await resolve_clean_url_once(video_url, uid=uid, key=key)
        except ZhucekaConfigError:
            raise  # 用户没配 key,不要重试
        except ZhucekaError as exc:
            last_error = exc
            logger.warning(
                "zhuceka 去水印第 %d 次失败: %s", attempt, exc,
                extra={"event": "watermark_retry"},
            )
    assert last_error is not None
    raise last_error
