"""
v0.3.0:aegis 人机验证 solver —— 检测 + 截图 + 打码 + 拟人拖拽 + 重试。

调用方(login / video)在检测到弹窗后:
    client = TtshituCaptchaClient(load_credentials())
    page = ...  # Playwright Page
    await solve_aegis_captcha(page, client)

solver 只负责「让弹窗消失」,不负责「让弹窗不再弹」 —— 后者是 aegis 风控
侧的事,我们只能靠 cold-start cooldown(同账号 30 分钟内不再触发第二次)缓解。

为什么不用 opencv / 模板匹配本地识别:
  - 图鉴成本约 0.002-0.005 元/次,本地识别要写图像处理代码 = 维护成本 + 新
    拼图类型要重训。一个月顶多 100-200 张图,外包给打码平台更便宜。
  - aegis 拼图内容随机(动物 / 物品 / 几何 / 文字),本地识别准确率不稳。

为什么拖拽要拟人:
  - aegis 验证拖拽轨迹 + 时间分布。直线 200ms 拖完 = 100% 判定为 bot。
  - Bezier + 步数多 + 中段抖动 + 中段减速 = 大致符合「人手」统计特征。
"""
from __future__ import annotations

import asyncio
import enum
import logging
import math
import random
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from playwright.async_api import Page

from .config import CaptchaCredentials
from .ttshitu_client import (
    TtshituCaptchaClient,
    TtshituError,
    TtshituSolve,
    TtshituDisabled,
    TYPEID_COORDINATE_1_4,
    TYPEID_SINGLE_GAP,
)


logger = logging.getLogger(__name__)


# v0.3.0:同账号 cooldown(秒)。aegis 触发后 30 分钟内不再尝试自动解,
# 让"用户手工登录 + 改密码"或"换 IP"成为主缓解手段。模块级 dict,
# key = account_id(user_unique_id 或 profile_dir 路径)。重启清空。
_CAPTCHA_COOLDOWN_SECONDS = 30 * 60
_captcha_cooldown: dict[str, float] = {}


# aegis 弹窗的关键文案 —— 出现任一即视为「弹窗已就位」。这串文本是
# 用户截图里看到的「拖动下方图片到上方轮廓」中的几个关键字,只用作 DOM 探测,
# 实际拖拽坐标由图鉴 API 返回。
AEGIS_DETECTION_PHRASES = (
    "拖动",
    "拖拽",
    "滑动",
    "拼图",
    "滑块",
    "向右拖动",
)


class CaptchaKind(enum.Enum):
    """aegis 验证大类 —— 用来决定怎么拖、typeid 用哪个。"""

    DRAG_SHAPE = "drag_shape"      # 「拖动物品到轮廓」类(typeid=27)
    SLIDE_PUZZLE = "slide_puzzle"  # 横向单缺口滑块(typeid=33)
    UNKNOWN = "unknown"


class AegisCaptchaFailed(Exception):
    """3 次重试后仍未通过。"""

    def __init__(self, message: str, *, last_raw: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.last_raw = last_raw


class AegisCaptchaDisabled(Exception):
    """凭证未配置或 enabled=False,调用方应跳过自动化,让任务 fail。"""


# ---------------------------------------------------------------------------
# 探测层
# ---------------------------------------------------------------------------

async def detect_aegis_captcha(page: Page) -> CaptchaKind:
    """判断页面里有没有 aegis 验证弹窗,并粗略分类。

    返回 CaptchaKind.UNKNOWN 表示「文本匹配到但分不出类型」 —— 上层仍可以
    调 solve_aegis_captcha 试试,失败的话 typeid 切换由 solver 自己处理。

    注意:不能在检测阶段 hard sleep,会拖慢流程。最多 200ms 等弹窗动画出现。
    """
    try:
        await page.wait_for_selector(
            "div[class*='aegis'], div[class*='verify'], iframe[src*='aegis']",
            timeout=200,
        )
    except Exception:
        pass

    html = await _safe_inner_html(page)
    if html is None:
        return CaptchaKind.UNKNOWN

    text = html.lower()
    has_slide_hint = any(p in html for p in ("滑动", "滑块", "向右拖动"))
    has_drag = any(p in html for p in ("拖动", "拖拽"))
    has_puzzle = "拼图" in html

    # 「拼图」传统指横向滑块,优先识别为 SLIDE(豆包老验证就是这个形态);
    # 只有出现「拖动/拖拽」且没有「滑块」线索时,才判为 drag-shape
    if has_slide_hint or has_puzzle:
        return CaptchaKind.SLIDE_PUZZLE
    if has_drag:
        return CaptchaKind.DRAG_SHAPE
    return CaptchaKind.UNKNOWN


async def _safe_inner_html(page: Page) -> str | None:
    """抓 iframe 优先;主文档次之;iframe 取不到就退到 page.content()。"""
    try:
        frames = page.frames
        for fr in frames:
            if fr == page.main_frame:
                continue
            try:
                content = await fr.content()
            except Exception:
                continue
            if any(p in content for p in AEGIS_DETECTION_PHRASES):
                return content
    except Exception as e:
        logger.debug("aegis iframe scan failed: %s", e)
    try:
        return await page.content()
    except Exception as e:
        logger.warning("aegis page.content() failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# 拖拽动作层
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _DragPoint:
    x: float
    y: float
    t_ms: int  # 相对开始时刻的毫秒


async def human_like_drag(
    page: Page,
    *,
    start_xy: tuple[float, float],
    end_xy: tuple[float, float],
    steps: int = 40,
) -> None:
    """拟人拖拽:Bezier 中段抖动 + 时间分布「两头快中段慢」。

    设计要点(为什么这样选参数):
      - steps=40: ~25ms / 步 ≈ 1s 拖完。aegis 经验阈值是「> 300ms 且 < 3s」。
      - Bezier 控制点偏离主轴 ±15px:模拟人手抖动,直线会被判定 bot。
      - 中段(40%~70% 进度)加 30% 减速:模拟「快到目标时减速对齐」。
      - 起始 / 结束各 ±5px 抖动:贴合人手落点不准。
    """
    sx, sy = start_xy
    ex, ey = end_xy
    if steps < 8:
        steps = 8
    dx = ex - sx
    dy = ey - sy
    dist = math.hypot(dx, dy)
    if dist < 8:
        # 距离太短就别 Bezier 了,鼠标直走 + sleep,避免 JS 报参数错
        await page.mouse.move(ex, ey, steps=steps)
        return

    # 控制点:垂直于主轴偏移 + 主轴 1/3 位置
    # 如果 dy=0(纯水平拖),perpendicular 用 (0,1);否则归一化
    if dx == 0 and dy == 0:
        perp = (0.0, 0.0)
    elif abs(dx) < 1e-3:
        perp = (1.0, 0.0)
    elif abs(dy) < 1e-3:
        perp = (0.0, 1.0)
    else:
        # 主轴方向 (dx,dy),逆时针旋转 90° 得垂直方向 (-dy,dx),再归一化
        perp = (-dy / dist, dx / dist)

    jitter = min(15.0, dist * 0.18)
    c1_offset = (perp[0] * jitter * random.uniform(-1, 1), perp[1] * jitter * random.uniform(-1, 1))
    c2_offset = (perp[0] * jitter * random.uniform(-1, 1), perp[1] * jitter * random.uniform(-1, 1))
    c1 = (sx + dx * 0.33 + c1_offset[0], sy + dy * 0.33 + c1_offset[1])
    c2 = (sx + dx * 0.66 + c2_offset[0], sy + dy * 0.66 + c2_offset[1])

    # 时间分布:中段慢、两头快。累加权重 → 归一化 → 累积时间。
    # 权重形如 w(t) = 1 + 0.6 * sin(pi * t)  → t=0/1 时 w=1, t=0.5 时 w=1.6
    weights = []
    for i in range(steps + 1):
        t = i / steps
        w = 1.0 + 0.6 * math.sin(math.pi * t)
        weights.append(w)
    total_w = sum(weights)
    cum_t = [0.0]
    for w in weights:
        cum_t.append(cum_t[-1] + w / total_w)
    # cum_t[-1] == 1.0,持续 1000ms
    total_ms = 900 + random.randint(0, 300)

    points: list[_DragPoint] = []
    for i in range(steps + 1):
        t = i / steps
        # 三次 Bezier
        x = (
            (1 - t) ** 3 * sx
            + 3 * (1 - t) ** 2 * t * c1[0]
            + 3 * (1 - t) * t ** 2 * c2[0]
            + t ** 3 * ex
        )
        y = (
            (1 - t) ** 3 * sy
            + 3 * (1 - t) ** 2 * t * c1[1]
            + 3 * (1 - t) * t ** 2 * c2[1]
            + t ** 3 * ey
        )
        # 中段抖动 ±2px
        if 0.2 < t < 0.8:
            x += random.uniform(-2.0, 2.0)
            y += random.uniform(-2.0, 2.0)
        # 起止落点抖动 ±5px
        if i == 0:
            x += random.uniform(-3, 3)
            y += random.uniform(-3, 3)
        if i == steps:
            x += random.uniform(-5, 5)
            y += random.uniform(-5, 5)
        ms = int(cum_t[i + 1] * total_ms)
        points.append(_DragPoint(x=x, y=y, t_ms=ms))

    # 鼠标按下、移动、释放 —— mouse.down() 必须在 move 之前调用
    await page.mouse.move(sx + random.uniform(-3, 3), sy + random.uniform(-3, 3))
    await page.mouse.down()
    prev_ms = 0
    for p in points:
        dt = max(1, p.t_ms - prev_ms)
        await page.mouse.move(p.x, p.y, steps=1)
        await asyncio.sleep(dt / 1000)
        prev_ms = p.t_ms
    await page.mouse.up()


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


# 弹窗中通常有这几种「拖动源」class;aegis 不同时期 class 名会换,
# 给个兜底 list。
_DRAG_HANDLE_SELECTORS = (
    "[class*='aegis'] [class*='slider']",
    "[class*='aegis'] [class*='drag-handle']",
    "[class*='verify'] [class*='slider']",
    "div[class*='slider-btn']",
    "div[class*='drag-btn']",
)
_TARGET_CENTER_SELECTORS = (
    "[class*='aegis'] [class*='target']",
    "[class*='aegis'] [class*='shape']",
    "[class*='verify'] [class*='target']",
    "div[class*='target-shape']",
)


async def _find_element_box(page: Page, selectors: tuple[str, ...]) -> tuple[float, float, float, float] | None:
    """按 selector list 找到第一个可见元素,返回 (x, y, w, h)。都没有就 None。"""
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=150)
            box = await loc.bounding_box()
        except Exception:
            continue
        if box and box["width"] > 0 and box["height"] > 0:
            return (box["x"], box["y"], box["width"], box["height"])
    return None


async def solve_aegis_captcha(
    page: Page,
    client: TtshituCaptchaClient,
    *,
    max_attempts: int = 3,
    on_state: Callable[[str], Awaitable[None] | None] | None = None,
) -> TtshituSolve:
    """主流程:截图 → 图鉴 → 拟人拖拽 → 等待 aegis 校验,失败重试。

    on_state 是给上层(LoginState / VideoRunner)用的回调,字符串:
      "detecting" / "uploading" / "dragging" / "verifying" / "ok" / "failed"
    同步函数也行(await 是 None 也行)。

    抛 AegisCaptchaFailed(3 次都没过)或 TtshituDisabled(凭证关)。
    """
    last_solve: TtshituSolve | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            if on_state:
                r = on_state("uploading")
                if asyncio.iscoroutine(r):
                    await r
            png = await _screenshot_captcha_area(page)
            if png is None:
                raise AegisCaptchaFailed("screenshot returned None — captcha UI not found")

            # 主 typeid 27(typeid 顺序敏感:失败自动降级到 33)
            typeid = TYPEID_COORDINATE_1_4 if attempt == 1 else TYPEID_SINGLE_GAP
            try:
                solve = await asyncio.to_thread(client.solve_image, png, typeid=typeid)
            except TtshituError as e:
                logger.warning("aegis attempt %d: ttshitu error: %s", attempt, e)
                if attempt >= max_attempts:
                    raise AegisCaptchaFailed(f"ttshitu exhausted: {e}", last_raw=e.raw) from e
                await asyncio.sleep(1.5 * attempt)
                continue
            last_solve = solve

            if on_state:
                r = on_state("dragging")
                if asyncio.iscoroutine(r):
                    await r
            dragged = await _drag_for_solve(page, solve)
            if not dragged:
                logger.info("aegis attempt %d: drag targets not located, retry", attempt)
                if attempt >= max_attempts:
                    raise AegisCaptchaFailed("drag targets missing on final attempt")
                await asyncio.sleep(1.0)
                continue

            if on_state:
                r = on_state("verifying")
                if asyncio.iscoroutine(r):
                    await r
            # aegis 校验通常 1.5-3s 完成
            await asyncio.sleep(2.5)
            if await _captcha_gone(page):
                if on_state:
                    r = on_state("ok")
                    if asyncio.iscoroutine(r):
                        await r
                logger.info("aegis captcha solved on attempt %d (cost %dms)", attempt, solve.cost_ms)
                return solve

            logger.info("aegis attempt %d: still visible after drag", attempt)
            await asyncio.sleep(1.0)
        except TtshituDisabled:
            raise
        except AegisCaptchaFailed:
            raise
        except Exception as e:  # noqa: BLE001
            logger.exception("aegis attempt %d: unexpected error", attempt)
            if attempt >= max_attempts:
                raise AegisCaptchaFailed(f"unexpected: {e}", last_raw=last_solve.raw if last_solve else None) from e
            await asyncio.sleep(1.0)

    raise AegisCaptchaFailed(
        f"exhausted {max_attempts} attempts",
        last_raw=last_solve.raw if last_solve else None,
    )


async def _screenshot_captcha_area(page: Page) -> bytes | None:
    """截弹窗整体图(矩形),给图鉴识别。

    用 Element.screenshot 而不是 page.screenshot,避免把整个浏览器窗口丢过去
    让图鉴在无关内容里找答案 —— 浪费钱。
    """
    selectors = (
        "[class*='aegis'][class*='dialog']",
        "[class*='aegis'][class*='container']",
        "[class*='verify']",
        "div[class*='captcha']",
        "div[class*='puzzle']",
    )
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            await loc.wait_for(state="visible", timeout=250)
            return await loc.screenshot()
        except Exception:
            continue
    # 兜底:整页
    try:
        return await page.screenshot()
    except Exception as e:
        logger.warning("aegis screenshot fallback failed: %s", e)
        return None


async def _drag_for_solve(page: Page, solve: TtshituSolve) -> bool:
    """根据图鉴返回的坐标 + 弹窗 DOM 定位,执行拟人拖拽。

    typeid=27(多坐标点选):
      - 期望弹窗有「拖动源 + 目标」两组元素,points[0] 是目标中心(像素)
      - 我们按比例换算到 viewport 坐标
    typeid=33(单缺口):
      - points[0] 是缺口中心,直接拖到那
      - 拖动源默认是 [class*='slider']
    """
    handle_box = await _find_element_box(page, _DRAG_HANDLE_SELECTORS)
    target_box = await _find_element_box(page, _TARGET_CENTER_SELECTORS)
    if handle_box is None:
        return False

    hx, hy, hw, hh = handle_box
    handle_center = (hx + hw / 2, hy + hh / 2)

    if solve.typeid == TYPEID_SINGLE_GAP:
        # 直接把 handle 拖到 gap 中心
        gap_x, gap_y = solve.points[0]
        # gap 是「截图内的像素坐标」,需要按弹窗 viewport 缩放
        if target_box:
            tx, ty, tw, th = target_box
            # 图鉴是基于整个截图给的坐标,我们按截图 bbox 等比缩放到 target box
            # _screenshot_captcha_area 拿的是 aegis 容器的图;缩放系数 = 容器实际像素 / 图鉴收到图的大小
            # 我们没有图鉴的图大小,但 target_box 是容器的可视化区,二者近似 1:1
            end_xy = (tx + gap_x * (tw / 600), ty + gap_y * (th / 400))
        else:
            end_xy = (handle_center[0] + gap_x, handle_center[1] + gap_y)
        await human_like_drag(page, start_xy=handle_center, end_xy=end_xy)
        return True

    # typeid=27:多点坐标点选 → 取第一个当拖动目标
    if not solve.points:
        return False
    target_x_in_screenshot, target_y_in_screenshot = solve.points[0]
    if target_box:
        tx, ty, tw, th = target_box
        # 容器的可视像素尺寸,vs 图鉴接收的图尺寸,二者接近 1:1 但实际可能有差异
        end_xy = (tx + target_x_in_screenshot, ty + target_y_in_screenshot)
    else:
        end_xy = (
            handle_center[0] + target_x_in_screenshot - 100,
            handle_center[1] + target_y_in_screenshot - 100,
        )
    await human_like_drag(page, start_xy=handle_center, end_xy=end_xy)
    return True


async def _captcha_gone(page: Page) -> bool:
    """弹窗是否已关闭。"""
    try:
        for sel in ("[class*='aegis'][class*='dialog']", "[class*='verify']", "div[class*='captcha']"):
            loc = page.locator(sel).first
            if await loc.count() > 0 and await loc.is_visible():
                return False
        return True
    except Exception:
        # 拿不准时保守:认为还在
        return False


# ---------------------------------------------------------------------------
# 凭证 convenience
# ---------------------------------------------------------------------------


def make_client(creds: CaptchaCredentials | None) -> TtshituCaptchaClient:
    """凭证 OK 就返 client;否则抛 AegisCaptchaDisabled。"""
    if creds is None or not creds.usable:
        raise AegisCaptchaDisabled(
            f"credentials not usable: enabled={getattr(creds, 'enabled', None)}, "
            f"username={'set' if creds and creds.username else 'empty'}"
        )
    return TtshituCaptchaClient(creds)


def is_in_cooldown(account_key: str) -> bool:
    """v0.3.0:同账号 30 分钟内是否在冷却。"""
    expiry = _captcha_cooldown.get(account_key)
    if expiry is None:
        return False
    if time.monotonic() >= expiry:
        # 过期了清掉
        _captcha_cooldown.pop(account_key, None)
        return False
    return True


def mark_cooldown(account_key: str) -> None:
    """v0.3.0:打标 cooldown —— solver 失败或成功后调,避免 30 分钟内重试。"""
    _captcha_cooldown[account_key] = time.monotonic() + _CAPTCHA_COOLDOWN_SECONDS


def clear_cooldown(account_key: str) -> None:
    """v0.3.0:用户在前端关 cooldown 后调,或测试 reset。"""
    _captcha_cooldown.pop(account_key, None)


def cooldown_remaining(account_key: str) -> float:
    """v0.3.0:剩余冷却秒数(0 = 没在冷却)。"""
    expiry = _captcha_cooldown.get(account_key)
    if expiry is None:
        return 0.0
    remaining = expiry - time.monotonic()
    if remaining <= 0:
        _captcha_cooldown.pop(account_key, None)
        return 0.0
    return remaining