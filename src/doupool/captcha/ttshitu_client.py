"""
v0.3.0:图鉴打码平台 HTTP client。

只负责「发图 + 拿点」,不负责拖拽、不负责检测弹窗。solver 调用本 client 后
拿到坐标自己跑 Playwright 鼠标事件。这么切分是因为:
  - 单测本 client 不需要 Playwright,纯 httpx + 假服务器就行
  - 以后换平台(若图鉴涨价或挂了)只换本文件,solver 不动
  - 失败语义明确:网络错 / 鉴权错 / 识别不出 = 三种不同 exception,上层各自处理

typeid 选择:
  - 27 = 1-4 个坐标点选(主,适配「拖动对应物品到对应轮廓」类 aegis 验证)
  - 33 = 单缺口识别(兜底,适配传统横向滑块)
  - 22 = 5-8 坐标点选(更复杂的拼图,目前没见过但留 typeid 切换)
  - 53 = 拼图 / 48 = 轨迹 —— 暂不接,aegis 没出这种
"""
from __future__ import annotations

import base64
import logging
import random
import time
from dataclasses import dataclass
from typing import Any

import httpx

from .config import CaptchaCredentials


logger = logging.getLogger(__name__)


# typeid 常量 —— 提到模块级,方便其他模块 import + 测试 mock
TYPEID_COORDINATE_1_4 = 27   # 1-4 个坐标点选
TYPEID_SINGLE_GAP = 33      # 单缺口识别(横向滑块)
TYPEID_COORDINATE_5_8 = 22  # 5-8 个坐标点选
TYPEID_PUZZLE = 53          # 拼图(未使用)
TYPEID_TRAJECTORY = 48      # 轨迹(未使用)


@dataclass(frozen=True, slots=True)
class TtshituSolve:
    """图鉴识图结果。

    points: 图鉴给的可点击坐标列表(像素,相对原图左上角)。typeid=27 通常 1-4 个;
            typeid=33 是单缺口识别,会返回 1 个点(滑块目标 x,y)。
    cost_ms: 从发出 HTTP 请求到收到响应(含 base64 编码)的本地耗时,供上层观察服务质量
    raw: 图鉴原始 JSON 响应,debug / 日志用
    typeid: 实际用的 typeid,solver 用它判断「拖一个 / 拖 N 个」
    """

    points: list[tuple[int, int]]
    cost_ms: int
    raw: dict[str, Any]
    typeid: int

    @property
    def primary(self) -> tuple[int, int] | None:
        """主要操作点(typeid=27 取第一个 / typeid=33 取缺口中心)。"""
        if not self.points:
            return None
        x, y = self.points[0]
        return (x, y)


class TtshituError(Exception):
    """图鉴识别失败(网络 / 鉴权 / 服务器错 / 余额不足)。"""

    def __init__(self, message: str, *, raw: dict[str, Any] | None = None, typeid: int | None = None) -> None:
        super().__init__(message)
        self.raw = raw
        self.typeid = typeid


class TtshituDisabled(Exception):
    """凭证未配置或 enabled=False —— 不重试,直接告诉上层「别再调」。"""


class TtshituCaptchaClient:
    """图鉴 client。无状态,只暴露 solve_image() 一个方法。

    线程安全:httpx.Client 是线程安全的,可以共享一个 client 给多个 solver 并发调用。
    但实际上 aegis 弹窗同一时刻只会有一个,串行调用就够了,不开连接池。
    """

    DEFAULT_TIMEOUT = 12.0  # 秒,aegis 检测到 captcha 后给的时间窗大约 30s
    API_URL = "https://api.ttshitu.com/base64"

    def __init__(
        self,
        credentials: CaptchaCredentials,
        *,
        timeout: float = DEFAULT_TIMEOUT,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not credentials.usable:
            raise TtshituDisabled(
                f"图鉴凭证不可用:enabled={credentials.enabled}, "
                f"username={'set' if credentials.username else 'empty'}"
            )
        self._credentials = credentials
        self._owns_client = http_client is None
        self._http = http_client or httpx.Client(timeout=timeout)
        self._timeout = timeout

    def close(self) -> None:
        if self._owns_client:
            self._http.close()

    def __enter__(self) -> "TtshituCaptchaClient":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def solve_image(
        self,
        png_bytes: bytes,
        *,
        typeid: int = TYPEID_COORDINATE_1_4,
    ) -> TtshituSolve:
        """发图到图鉴,返回识别坐标。

        typeid=27(图鉴默认「1-4 坐标点选」)覆盖拖动物品到轮廓;
        typeid=33(单缺口)兜底横向滑块。

        抛 TtshituError 区分情况:
          - 网络错(connect / read timeout / DNS):message 形如 "network: ..."
          - 鉴权错(密码错 / 余额不足):code != 0 且 message 含 "用户"/"密码"/"余额"
          - 服务器错(5xx):message 形如 "http: 503"
        """
        if not png_bytes:
            raise TtshituError("empty png_bytes", typeid=typeid)
        b64 = base64.b64encode(png_bytes).decode("ascii")
        payload = {
            "username": self._credentials.username,
            "password": self._credentials.password,
            "typeid": typeid,
            "image": b64,
        }
        # 图鉴接口偶尔返回慢,加 1s 抖动避免被当作脚本批量
        time.sleep(random.uniform(0.05, 0.25))

        t0 = time.monotonic()
        try:
            resp = self._http.post(self.API_URL, json=payload, timeout=self._timeout)
        except httpx.HTTPError as e:
            raise TtshituError(f"network: {type(e).__name__}: {e}", typeid=typeid) from e
        cost_ms = int((time.monotonic() - t0) * 1000)

        if resp.status_code != 200:
            raise TtshituError(
                f"http: {resp.status_code}",
                raw={"status": resp.status_code, "body": resp.text[:200]},
                typeid=typeid,
            )

        try:
            data = resp.json()
        except ValueError as e:
            raise TtshituError(f"json: {e}", raw={"body": resp.text[:200]}, typeid=typeid) from e

        return self._parse(data, typeid=typeid, cost_ms=cost_ms)

    def _parse(self, data: dict[str, Any], *, typeid: int, cost_ms: int) -> TtshituSolve:
        """解析图鉴响应。

        图鉴 v2 协议约定:
          成功:  {"code": 0, "msg": "success", "data": {"result": "...", "id": "..."}}
                 或者旧版 {"success": true, "data": {"point": [{"x":..,"y":..}]}}
          失败:  {"code": "1001", "msg": "用户不存在"} 或 {"success": false, "message": "..."}

        result 字段(新版)是字符串,需要按 typeid 拆:
          - typeid 27: "x1,y1|x2,y2|..."  多个坐标
          - typeid 33: "x,y"                单缺口
          - typeid 22: "x1,y1|x2,y2|..."  5-8 个坐标
        """
        code = data.get("code")
        if code not in (0, "0", 200, "200", None) and not data.get("success"):
            msg = data.get("msg") or data.get("message") or "unknown"
            raise TtshituError(f"server: code={code} msg={msg}", raw=data, typeid=typeid)

        inner = data.get("data") or {}
        # 新版: data.result 是 "x,y|x,y"
        if isinstance(inner, dict) and "result" in inner:
            raw_points = self._parse_points_str(str(inner["result"]))
        # 旧版: data.point 是 [{"x":..,"y":..}, ...]
        elif isinstance(inner, dict) and "point" in inner:
            raw_points = [(int(p["x"]), int(p["y"])) for p in inner["point"]]
        # 另一种变体: data 直接是 "x,y|x,y"
        elif isinstance(inner, str):
            raw_points = self._parse_points_str(inner)
        else:
            raise TtshituError(
                f"parse: unknown response shape: keys={list(inner.keys()) if isinstance(inner, dict) else type(inner).__name__}",
                raw=data,
                typeid=typeid,
            )

        if not raw_points:
            raise TtshituError("parse: empty points", raw=data, typeid=typeid)

        return TtshituSolve(points=raw_points, cost_ms=cost_ms, raw=data, typeid=typeid)

    @staticmethod
    def _parse_points_str(s: str) -> list[tuple[int, int]]:
        """'120,45|230,80' -> [(120, 45), (230, 80)]。容错:空白 / 单点。"""
        s = s.strip()
        if not s:
            return []
        if "|" not in s:
            # 单点
            parts = [p.strip() for p in s.split(",")]
            if len(parts) >= 2:
                try:
                    return [(int(parts[0]), int(parts[1]))]
                except ValueError:
                    return []
            return []
        out: list[tuple[int, int]] = []
        for token in s.split("|"):
            token = token.strip()
            if not token:
                continue
            parts = [p.strip() for p in token.split(",")]
            if len(parts) < 2:
                continue
            try:
                out.append((int(parts[0]), int(parts[1])))
            except ValueError:
                continue
        return out