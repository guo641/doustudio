from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import logging
import mimetypes
import random
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from playwright.async_api import BrowserContext, Page, async_playwright

from ..prompt_reviser import classify_failure, revise_prompt
from .aegis_probe import aegis_popup_present
from .protocol import (
    EXTRA_CLIENT_META_KEYS,
    DoubaoContentRejected,
    build_completion_payload,
    find_creation_directory,
    find_video_node,
    parse_creation_result,
    parse_sse_ack,
    parse_download_info,
)

# v0.3.2:UI click 路径 selector 常量。类名稳定优先,SVG path 兜底。
# 视频 tab 用 a11y role + 文本(豆包前端 aria 标签就是「视频」)。
VIDEO_TAB_SEL = "[role='tab']:has-text('视频')"
_VIDEO_TAB_SELECTORS = (
    VIDEO_TAB_SEL,
    "[role='tab'][aria-label*='视频']",
    "button:has-text('视频')",
    "button[aria-label*='视频']",
    ".semi-tabs-tab:has-text('视频')",
)
_VIDEO_TAB_MOUNT_WAIT_MS = 800
_VIDEO_TAB_RATIO_WAIT_MS = 2_000
EDITOR_SEL = "[contenteditable='true'][role='textbox']"
SEND_BTN_SEL = "#flow-end-msg-send"
SEND_BTN_FALLBACK_SEL = ".send-btn-wrapper button"
_SEND_BTN_LEGACY_SVG_SEL = "button:has(svg path[d^='M4.93934 10.2598'])"
CREATE_IMAGE_URL = "https://www.doubao.com/chat/create-image"
_NEW_CONVERSATION_SEL = "div[class*='group/sidebar_nav_item']"
_VIDEO_TOOLBAR_MORE_SEL = "button.skill-bar-button"
_VIDEO_GENERATION_SKILL_SEL = (
    "button[data-component-type='skill-item']"
    "[data-skill-id='skill_bar_button_17']"
)
_VIDEO_GENERATION_SKILL_FALLBACK_SEL = "button[data-component-type='skill-item']"
# At narrow desktop widths the overflow menu renders its skill entries as
# ordinary actionbar buttons without the skill-item metadata used by the
# wide-layout toolbar.
_VIDEO_GENERATION_ACTIONBAR_ITEM_SEL = (
    "button[data-input-engine-action-source='actionbar']"
)
_VIDEO_MODE_PLACEHOLDER_SEL = "[data-placeholder='描述你想要的视频']"
_VIDEO_MODE_CHIP_SEL = (
    "[data-input-engine-action-source='actionbar']"
    "[data-value='17'][contenteditable='false']"
)
_VIDEO_MODE_SKILL_BUTTON_SEL = (
    "button[data-component-type='skill-item']"
    "[data-input-engine-action-source='actionbar']"
    "[data-skill-id='skill_bar_button_17']"
)
_VIDEO_MODE_CHIP_CANDIDATE_SELS = (
    # Layout B fresh chat: the clickable actionbar entry is a skill button.
    _VIDEO_MODE_SKILL_BUTTON_SEL,
    # Layout B after mode activation: the selected chip is a data-value node.
    _VIDEO_MODE_CHIP_SEL,
    "[data-input-engine-action-source='actionbar'][data-value='17']",
    "[data-input-engine-action-source='actionbar'][data-skill-id='skill_bar_button_17']",
)
_VIDEO_MODEL_BUTTON_SEL = (
    "button[data-input-engine-actionbar-control-key='video-model']"
)
_VIDEO_MODEL_MENU_ITEM_SEL = "[role='menuitem'][data-slot='dropdown-menu-item']"
_VIDEO_MODE_READY_WAIT_MS = 5_000
VIDEO_MODEL_LABELS = {
    "seedance_v2.0_mini": "Seedance 2.0 Mini",
    "seedance_v2.0_std": "Seedance 2.0 Fast",
    "seedance_v2.0": "Seedance 2.0",
}
_VIDEO_RATIO_OPTIONS = ("3:4", "4:3", "9:16", "16:9", "1:1", "21:9")
_VIDEO_DURATION_MIN_SECONDS = 4
_VIDEO_DURATION_MAX_SECONDS = 15
_VIDEO_OPTIONS_TRIGGER_RE = re.compile(
    r"(?:自动|3:4|4:3|9:16|16:9|1:1|21:9)\s*·\s*(?:[4-9]|1[0-5])s"
)
_VIDEO_MORE_OPTIONS_SELECTORS = (
    # 部分灰度账号把视频参数收进模型选择器右侧的三点按钮。优先走
    # 相邻兄弟关系，避免误点页面右上角同样名为「更多」的全局按钮。
    "button:has-text('模型') + button",
    "[role='button']:has-text('模型') + [role='button']",
    "button:has-text('Seedance') + button",
    "[role='button']:has-text('Seedance') + [role='button']",
    "button:has-text('⋯'), [role='button']:has-text('⋯')",
    "button:has-text('…'), [role='button']:has-text('…')",
    "button:has-text('...'), [role='button']:has-text('...')",
    "button:has-text('•••'), [role='button']:has-text('•••')",
    "button[aria-label*='更多'], [role='button'][aria-label*='更多']",
    "button[aria-label*='more' i], [role='button'][aria-label*='more' i]",
    "button[aria-label*='option' i], [role='button'][aria-label*='option' i]",
    "button:has(svg[aria-label*='更多']), "
    "[role='button']:has(svg[aria-label*='更多'])",
    "button:has(svg[aria-label*='more' i]), "
    "[role='button']:has(svg[aria-label*='more' i])",
    "button[class*='ellipsis' i], [role='button'][class*='ellipsis' i]",
    "button[class*='more' i], [role='button'][class*='more' i]",
)
_VIDEO_MORE_OPTIONS_GEOMETRY_SELECTORS = (
    "button:has(svg circle:nth-of-type(3)), "
    "[role='button']:has(svg circle:nth-of-type(3))",
    "button:has(svg rect:nth-of-type(3)), "
    "[role='button']:has(svg rect:nth-of-type(3))",
    "button:has(svg path:nth-of-type(3)), "
    "[role='button']:has(svg path:nth-of-type(3))",
    "button:has(svg use[href*='more' i]), "
    "[role='button']:has(svg use[href*='more' i])",
    "button:has(svg use[href*='ellipsis' i]), "
    "[role='button']:has(svg use[href*='ellipsis' i])",
    "button:has(svg use[href*='kebab' i]), "
    "[role='button']:has(svg use[href*='kebab' i])",
    "button:has(svg[class*='more' i]), "
    "button:has(svg[class*='ellipsis' i]), "
    "button:has(svg[class*='kebab' i]), "
    "[role='button']:has(svg[class*='more' i]), "
    "[role='button']:has(svg[class*='ellipsis' i]), "
    "[role='button']:has(svg[class*='kebab' i])",
    "button:has(svg), [role='button']:has(svg), .semi-button:has(svg)",
)
_VIDEO_MORE_OPTIONS_ALL_SELECTORS = (
    *_VIDEO_MORE_OPTIONS_SELECTORS,
    *_VIDEO_MORE_OPTIONS_GEOMETRY_SELECTORS,
)
_VIDEO_MORE_OPTIONS_ANCHOR_RE = re.compile(
    r"(?:模型|model|seedance)",
    re.IGNORECASE,
)
_VIDEO_MORE_OPTIONS_TEXT_RE = re.compile(r"^\s*(?:⋯|…|\.{3}|•••)\s*$")
_VIDEO_MORE_OPTIONS_MAX_ANCHOR_DISTANCE_PX = 400
_VIDEO_MORE_OPTIONS_MAX_HORIZONTAL_GAP_PX = 160
_VIDEO_MORE_OPTIONS_MAX_VERTICAL_DELTA_PX = 32
_VIDEO_MORE_OPTIONS_MAX_ICON_SIZE_PX = 64
_VIDEO_OPTIONS_MENU_WAIT_MS = 1_000
_VIDEO_OPTIONS_READBACK_WAIT_MS = 3_000
_VIDEO_OPTIONS_CLOSE_WAIT_MS = 1_500
_VIDEO_OPTIONS_CLOSED_STABLE_MS = 200
_VIDEO_OPTIONS_TRIGGER_WAIT_MS = 3_000
_VIDEO_OPTIONS_MIN_VISIBLE_RATIOS = 4
# UI click 路径下的弹窗缓冲。实测 aegis 弹窗在导航后 3-5s 才出现，
# 因此提交前保留完整探测窗口；命中后立即中止，不再自动求解。
_UI_AEGIS_WAIT_SECONDS = 6.0  # 弹窗出现最长的等待(用户实测 3-5s)
_UI_AEGIS_DETECT_POLL_INTERVAL = 0.5  # 弹窗轮询间隔
# 拦截 /chat/completion 响应的最长时间,跟 click → POST → SSE 飞出去对齐
_UI_ACK_WAIT_SECONDS = 30.0

# v0.2.22:模块级 logger,retry loop 用 warn 级记「revise 重试」事件
_LOGGER = logging.getLogger(__name__)


PC_VERSION = "3.27.4"
# v0.2.17:模块级常量保留向后兼容;真实读取走 settings_service.get("pc_version")。
# 这里只是个 fallback,settings 没读到 / 写坏时用。

# v0.2.17:Chrome 启动参数压下「自动化」指纹。--disable-blink-features=AutomationControlled
# 把 navigator.webdriver 反探测盖掉,site-per-process 关掉是字节前端用到的隔离行为。
_STEALTH_LAUNCH_ARGS = (
    "--disable-blink-features=AutomationControlled",
    "--disable-features=IsolateOrigins,site-per-process",
)

# 浏览器 locale / 时区跟真人中文用户对齐,避免 fbp(fingerprint by proxy)命中海外标签。
_BROWSER_LOCALE = "zh-CN"
_BROWSER_TIMEZONE = "Asia/Shanghai"


class TokenBundleUnavailable(RuntimeError):
    """v0.2.17:从登录 profile 抽不到完整 WebMSSDK token bundle。

    通常因为:刚 login 完没让真人用户访问 doubao.com/chat/ 主页,leveldb 里
    还没有 msToken / web_id 缓存。CHANGELOG 要求用户「登录后先在浏览器里
    手动访问 doubao.com/chat/ 主页 5-10 秒」,再点 UI 「刷新 token」按钮。
    """


@dataclass(slots=True)
class TokenBundle:
    """v0.2.17:登录 profile 里抽出的 WebMSSDK / TeaSDK 真实指纹。

    字段都来自登录后持久化的 Chromium profile(Cookies SQLite + Local Storage leveldb),
    **不要逆向生成**(用户决策)。视频提交时把 `to_client_meta()` 当 kwargs 透传给
    `build_completion_payload`,payload.client_meta 收到后豆包会用它签 a_bogus。
    """

    ms_token: str = ""
    web_id: str = ""
    web_id_signature: str = ""
    device_id: str = ""
    tea_uuid: str = ""
    pc_version: str = PC_VERSION
    fetched_at: float = field(default_factory=time.time)

    def to_client_meta(self) -> dict[str, str]:
        """返回白名单(EXTRA_CLIENT_META_KEYS)过滤后的 dict,空值丢弃。

        pc_version 用 dataclass 默认(永不空),即使 TokenBundle 没显式给也会
        出现在 dict 里 — 字节风控看到空 pc_version 直接风控。
        """
        return {
            k: v
            for k, v in {
                "web_id": self.web_id,
                "tea_uuid": self.tea_uuid,
                "device_id": self.device_id,
                "pc_version": self.pc_version,
                "web_id_signature": self.web_id_signature,
            }.items()
            if v
        }

    def age_seconds(self) -> float:
        return max(0.0, time.time() - self.fetched_at)


def _read_cookies_from_json(profile_dir: Path) -> dict[str, str] | None:
    """v0.2.37.2:读我们登录时主动导出的 cookies.json(首选数据源)。

    为什么首选 cookies.json 而不是 Chromium SQLite:
      - login/browser.py 在扫码登录成功时主动调用 `context.cookies()` 把
        doubao.com 域 cookie 写到 profile_dir/cookies.json,这是**实时从
        Chromium 进程里拉出的明文**,没有 DPAPI 加密问题。
      - 之后任何时刻读 cookies.json 都能拿到当前的登录态。
      - 而 `Default/Cookies` SQLite 在我们的 profile 里**根本不存在**(我们的
        profile 是 Playwright 启动时按需创建的,正常情况下登录流程结束 context
        关闭后 SQLite 才会落地;如果用户关掉软件太快或 process kill,SQLite
        可能没刷盘)。

    返回 {name: value} dict 或 None(文件不存在 / 解析失败 → 让上层走 SQLite fallback)。
    """
    target = profile_dir / "cookies.json"
    if not target.exists():
        return None
    try:
        raw = target.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, json.JSONDecodeError) as exc:
        _LOGGER.warning("读 cookies.json 失败(%s): %s", target, exc)
        return None
    if not isinstance(data, list):
        return None
    out: dict[str, str] = {}
    for entry in data:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        value = entry.get("value")
        if name and value:
            out[str(name)] = str(value)
    return out


def _read_chromium_cookies(profile_dir: Path) -> dict[str, str]:
    """读 Chromium Cookies SQLite(Default/Cookies 或 Network/Cookies)。

    返回 {cookie_name: value},失败抛 TokenBundleUnavailable。Chromium 在 Windows
    下用 SQLite 存 cookie,内部 hosts 表里 doubao.com 一行一行都展开。lock 文件
    临时库 profile 锁住读不到 → 复制到 tmp 再读。

    v0.2.37.2:这步现在是**次选**,首选走 `_read_cookies_from_json`(我们登录时
    主动导出的明文备份)。如果 cookies.json 都没有,再尝试 SQLite 直读。
    Chromium v100+ 在 Windows 上 `value` 列是 DPAPI 加密后的空串、真正值在
    `encrypted_value` BLOB 里 —— 我们没有 DPAPI key 解不出来,这种情况就返回空
    dict(让上层 hint "请点重新导出 cookies"让 Playwright 帮我们拉明文回写)。
    """
    candidates = [
        profile_dir / "Default" / "Cookies",
        profile_dir / "Default" / "Network" / "Cookies",
    ]
    db_path = next((p for p in candidates if p.exists()), None)
    if db_path is None:
        return {}

    tmp = db_path.with_suffix(".doupool.read.tmp")
    # v0.2.36:Chromium 在用时 read_bytes / connect 可能抛 PermissionError /
    # DatabaseError / OperationalError(损坏文件 / 文件锁 / io error),全都归到
    # TokenBundleUnavailable,让上层 endpoint 一处 catch 就能拿到真实原因。
    try:
        try:
            tmp.write_bytes(db_path.read_bytes())
            conn = sqlite3.connect(f"file:{tmp}?mode=ro", uri=True)
        except sqlite3.OperationalError:
            # ro 失败 → 普通只读连接
            conn = sqlite3.connect(str(db_path))
            conn.execute("PRAGMA query_only = ON")
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT name, value, host_key FROM cookies "
                "WHERE host_key LIKE '%doubao.com%'"
            )
            cookies: dict[str, str] = {}
            for name, value, host in cur.fetchall():
                if name and value:
                    cookies[name] = value
            return cookies
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        raise TokenBundleUnavailable(
            f"无法读取 Chromium Cookies({db_path.name}):{exc.__class__.__name__}:{exc}; "
            "通常是 Chromium 正在使用该 profile(关闭浏览器窗口后再试)或 Cookies 文件损坏"
        ) from exc
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _read_chromium_local_storage(profile_dir: Path) -> dict[str, str]:
    """v0.2.37.2:读 Local Storage leveldb,挑出 web_id / tea_uuid / device_id。

    leveldb .log 是二进制格式,每条记录是 varint length + key + JSON value。
    直接用正则扫 `__tea_cache_tokens_497858` / `samantha_web_web_id` 后面
    跟着的 JSON 字串,挑出 web_id / user_unique_id 字段。

    v0.2.37.2 修了 v0.2.17 的 bug: 之前 regex 用 `(.+?)</script>` 期待 HTML script
    边界,但 leveldb .log 不是 HTML,根本不会有 `</script>` — 那个分支从未命中。
    改成 `(\{[^{}]*?"web_id"[^{}]*?\})` 抓最近的 JSON object。

    实测冷启动下 web_id 会被压到 .log 文件;如果没读到就用 cookies 兜底。
    """
    log_path = profile_dir / "Default" / "Local Storage" / "leveldb" / "000003.log"
    if not log_path.exists():
        return {}
    try:
        raw = log_path.read_bytes()
    except OSError:
        return {}
    text = raw.decode("latin-1", errors="replace")

    out: dict[str, str] = {}
    # v0.2.36:把所有 raw decode + regex + json.loads 异常都吞掉(leveldb .log 可能
    # 在 Chromium 写入时被截断,读到半个 JSON → json.loads 抛 ValueError)。
    # v0.2.37.2:修了一个更老的 bug——之前 regex 是 `(.+?)</script>`,但 leveldb
    # .log 文件不是 HTML,根本不会有 `</script>` → 那个分支永远不会命中,导致
    # web_id 永远从 storage 拿不到,只能 fall back 到 cookies.samantha_web_web_id。
    # 现在改用 `(\{[^{}]*?"web_id"[^{}]*?\})` 抓最近的 JSON object。
    try:
        # __tea_cache_tokens_497858 是一个 JSON 串:{user_unique_id, web_id, ...}
        tea_match = re.search(
            rb'__tea_cache_tokens_497858[^a-zA-Z0-9_]?(\{[^{}]{0,800}?"web_id"[^{}]{0,400}?\})',
            raw,
            re.DOTALL,
        )
        if tea_match:
            try:
                obj = json.loads(tea_match.group(1).decode("utf-8", errors="replace"))
                if isinstance(obj, dict):
                    if obj.get("web_id"):
                        out["web_id"] = str(obj["web_id"])
                    if obj.get("user_unique_id"):
                        out["tea_uuid"] = str(obj["user_unique_id"])
            except (ValueError, TypeError):
                pass
        # samantha_web_web_id 是另一个 JSON:{web_id, ...}
        sam_match = re.search(
            rb'samantha_web_web_id[^a-zA-Z0-9_]?(\{[^{}]{0,800}?"web_id"[^{}]{0,400}?\})',
            raw,
            re.DOTALL,
        )
        if sam_match:
            try:
                obj = json.loads(sam_match.group(1).decode("utf-8", errors="replace"))
                if isinstance(obj, dict) and obj.get("web_id"):
                    out["device_id"] = str(obj["web_id"])
            except (ValueError, TypeError):
                pass
        # 备用:直接 regex 抓 web_id / user_unique_id 的 string value(最后兜底)
        if "web_id" not in out:
            m = re.search(r'"web_id"\s*:\s*"([A-Za-z0-9_\-]{8,80})"', text)
            if m:
                out["web_id"] = m.group(1)
        if "tea_uuid" not in out:
            m = re.search(r'"user_unique_id"\s*:\s*"([A-Za-z0-9_\-]{8,80})"', text)
            if m:
                out["tea_uuid"] = m.group(1)
    except Exception:
        # 任何 decode / regex 异常都吞掉,返回部分结果(可能 web_id 缺失 → 上层 raise TokenBundleUnavailable)
        pass
    return out


def extract_webmssdk_tokens(profile_dir: Path) -> TokenBundle:
    """v0.2.37.2:从登录后持久化的 Chromium profile 抽 WebMSSDK / TeaSDK 真实指纹。

    数据源优先级:
      1. cookies.json —— login 流程主动导出的明文 cookie 备份(首选,实时从
         Chromium 进程拉取,无 DPAPI 加密问题)
      2. Chromium SQLite(Default/Cookies) —— 兜底;v100+ Windows 下可能
         被 DPAPI 加密导致 value 列为空,这种情况就拿不到明文
      3. Local Storage/leveldb/000003.log —— 抽 web_id / tea_uuid (leveldb
         二进制文件,正则扫 JSON 字段)

    任意一个关键字段缺失 → 抛 TokenBundleUnavailable,UI 引导用户点「重新导出
    cookies」按钮让 Playwright 重新拉一次 cookie 写回 cookies.json(自动刷新
    整条链)。
    """
    profile_dir = Path(profile_dir)

    # 1) 首选:cookies.json(login 流程主动导出的明文)
    cookies_from_json = _read_cookies_from_json(profile_dir)

    # 2) 兜底:Chromium SQLite(可能是 DPAPI 加密 → 拿到空 dict,正常)
    cookies_from_sqlite = _read_chromium_cookies(profile_dir)

    cookies = cookies_from_json if cookies_from_json is not None else cookies_from_sqlite
    if not cookies:
        # 两个源都没拿到 doubao.com cookie —— profile 真的没数据,提示用户重新登录
        cookies_json_exists = (profile_dir / "cookies.json").exists()
        raise TokenBundleUnavailable(
            "profile 里读不到 doubao.com cookie。"
            + (" (cookies.json 存在但解析失败)" if cookies_json_exists else "")
            + " 请点「重新导出 cookies」按钮,软件会重新打开浏览器拉一次当前 cookie。"
        )

    storage = _read_chromium_local_storage(profile_dir)

    ms_token = cookies.get("msToken", "") or cookies.get("ms_token", "")
    web_id_signature = cookies.get("_signature", "") or cookies.get("samantha_web_id_signature", "")
    web_id = storage.get("web_id", "") or cookies.get("samantha_web_web_id", "")
    device_id = storage.get("device_id", "") or cookies.get("s_v_web_id", "")
    tea_uuid = storage.get("tea_uuid", "") or cookies.get("user_unique_id", "")

    # 必要字段都齐全才算成功。任何一个缺失 → 让上层走「重新导出 cookies」流程
    if not (web_id and ms_token):
        missing = []
        if not web_id:
            missing.append("web_id")
        if not ms_token:
            missing.append("ms_token")
        raise TokenBundleUnavailable(
            f"cookie 里缺少关键字段: {missing}。"
            " 请点「重新导出 cookies」按钮重新拉一次,"
            "或在浏览器里访问 https://www.doubao.com/chat/ 主页 5 秒后再点刷新。"
        )

    return TokenBundle(
        ms_token=ms_token,
        web_id=web_id,
        web_id_signature=web_id_signature,
        device_id=device_id,
        tea_uuid=tea_uuid,
        pc_version=PC_VERSION,
    )

COMMON_QUERY_JS = r"""
() => {
  const read = key => { try { return JSON.parse(localStorage.getItem(key) || '{}') } catch { return {} } };
  const device = read('samantha_web_web_id');
  const tea = read('__tea_cache_tokens_497858');
  const fp = decodeURIComponent((document.cookie.match(/(?:^|;\s*)s_v_web_id=([^;]+)/) || [,''])[1]);
  const query = new URLSearchParams({
    version_code:'20800', language:'zh', device_platform:'web', doubao_device_platform:'web',
    aid:'497858', real_aid:'497858', pkg_type:'release_version', device_id:device.web_id || '',
    pc_version:'3.27.4', doubao_pc_version:'3.27.4', web_id:tea.web_id || tea.user_unique_id || '',
    tea_uuid:tea.user_unique_id || tea.web_id || '', region:'CN', sys_region:'CN',
    samantha_web:'1', web_platform:'browser', web_tab_id:crypto.randomUUID()
  });
  query.set('use-olympus-account', '1');
  if (fp) query.set('fp', fp);
  return Object.fromEntries(query.entries());
}
"""

COMPLETION_SCRIPT = r"""
async ({payload}) => {
  const read = key => { try { return JSON.parse(localStorage.getItem(key) || '{}') } catch { return {} } };
  const device = read('samantha_web_web_id');
  const tea = read('__tea_cache_tokens_497858');
  const query = new URLSearchParams({
    aid:'497858', device_id:device.web_id || '', device_platform:'web',
    doubao_device_platform:'web', doubao_pc_version:'3.27.4', fp:payload.ext.fp || '',
    language:'zh', pc_version:'3.27.4', pkg_type:'release_version', real_aid:'497858',
    region:'CN', samantha_web:'1', sys_region:'CN', tea_uuid:tea.user_unique_id || tea.web_id || '',
    version_code:'20800', web_id:tea.web_id || tea.user_unique_id || '', web_platform:'browser', web_tab_id:crypto.randomUUID()
  });
  query.set('use-olympus-account', '1');
  const hex = count => Array.from(crypto.getRandomValues(new Uint8Array(count)), value => value.toString(16).padStart(2, '0')).join('');
  const response = await fetch('/chat/completion?' + query, {
    method:'POST', credentials:'include',
    headers:{
      'content-type':'application/json', 'agw-js-conv':'str, str', 'last-event-id':'undefined',
      'x-flow-trace':`04-${hex(16)}-${hex(8)}-01`
    },
    body:JSON.stringify(payload)
  });
  return {status:response.status, text:await response.text()};
}
"""

CHAIN_SCRIPT = r"""
async ({conversationId}) => {
  const read = key => { try { return JSON.parse(localStorage.getItem(key) || '{}') } catch { return {} } };
  const device = read('samantha_web_web_id');
  const tea = read('__tea_cache_tokens_497858');
  const fp = decodeURIComponent((document.cookie.match(/(?:^|;\s*)s_v_web_id=([^;]+)/) || [,''])[1]);
  const query = new URLSearchParams({
    version_code:'20800', language:'zh', device_platform:'web', doubao_device_platform:'web',
    aid:'497858', real_aid:'497858', pkg_type:'release_version', device_id:device.web_id || '',
    pc_version:'3.27.4', doubao_pc_version:'3.27.4', web_id:tea.web_id || tea.user_unique_id || '', fp,
    tea_uuid:tea.user_unique_id || tea.web_id || '', region:'CN', sys_region:'CN',
    samantha_web:'1', web_platform:'browser', web_tab_id:crypto.randomUUID()
  });
  query.set('use-olympus-account', '1');
  const body = {
    cmd:3100,
    uplink_body:{pull_singe_chain_uplink_body:{
      conversation_id:conversationId, anchor_index:9007199254740991, conversation_type:3,
      direction:1, limit:20, ext:{}, filter:{index_list:[]},
      evaluate_ab_params:'', evaluate_common_params:''
    }},
    sequence_id:crypto.randomUUID(), channel:2, version:'1'
  };
  const response = await fetch('/im/chain/single?' + query, {
    method:'POST', credentials:'include',
    headers:{'content-type':'application/json; encoding=utf-8'}, body:JSON.stringify(body)
  });
  return {status:response.status, data:await response.json()};
}
"""

AISPACE_SCRIPT = r"""
async ({endpoint, body}) => {
  const read = key => { try { return JSON.parse(localStorage.getItem(key) || '{}') } catch { return {} } };
  const device = read('samantha_web_web_id');
  const tea = read('__tea_cache_tokens_497858');
  const fp = decodeURIComponent((document.cookie.match(/(?:^|;\s*)s_v_web_id=([^;]+)/) || [,''])[1]);
  const query = new URLSearchParams({
    version_code:'20800', language:'zh', device_platform:'web', doubao_device_platform:'web',
    aid:'497858', real_aid:'497858', pkg_type:'release_version', device_id:device.web_id || '',
    pc_version:'3.27.4', doubao_pc_version:'3.27.4', web_id:tea.web_id || tea.user_unique_id || '', fp,
    tea_uuid:tea.user_unique_id || tea.web_id || '', region:'CN', sys_region:'CN',
    samantha_web:'1', web_platform:'browser', web_tab_id:crypto.randomUUID()
  });
  query.set('use-olympus-account', '1');
  const response = await fetch(endpoint + '?' + query, {
    method:'POST', credentials:'include', headers:{'content-type':'application/json'},
    body:JSON.stringify(body)
  });
  return {status:response.status, data:await response.json()};
}
"""

UPLOAD_IMAGE_SCRIPT = r"""
async ({name, mime, base64Data}) => {
  const read = key => { try { return JSON.parse(localStorage.getItem(key) || '{}') } catch { return {} } };
  const device = read('samantha_web_web_id');
  const tea = read('__tea_cache_tokens_497858');
  const fp = decodeURIComponent((document.cookie.match(/(?:^|;\s*)s_v_web_id=([^;]+)/) || [,''])[1]);
  const common = () => {
    const query = new URLSearchParams({
      version_code:'20800', language:'zh', device_platform:'web', doubao_device_platform:'web',
      aid:'497858', real_aid:'497858', pkg_type:'release_version', device_id:device.web_id || '',
      pc_version:'3.27.4', doubao_pc_version:'3.27.4', web_id:tea.web_id || tea.user_unique_id || '',
      tea_uuid:tea.user_unique_id || tea.web_id || '', region:'CN', sys_region:'CN',
      samantha_web:'1', web_platform:'browser', web_tab_id:crypto.randomUUID()
    });
    query.set('use-olympus-account', '1');
    if (fp) query.set('fp', fp);
    return query;
  };
  const bytes = Uint8Array.from(atob(base64Data), c => c.charCodeAt(0));
  const extension = (name && name.includes('.')) ? ('.' + name.split('.').pop()) : (mime === 'image/jpeg' ? '.jpg' : '.png');

  // CRC32 for ImageX-style uploads
  const crcTable = (() => {
    const table = new Uint32Array(256);
    for (let i = 0; i < 256; i++) {
      let c = i;
      for (let k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320 ^ (c >>> 1)) : (c >>> 1);
      table[i] = c >>> 0;
    }
    return table;
  })();
  let crc = 0xFFFFFFFF;
  for (let i = 0; i < bytes.length; i++) crc = crcTable[(crc ^ bytes[i]) & 0xFF] ^ (crc >>> 8);
  const crcHex = ((crc ^ 0xFFFFFFFF) >>> 0).toString(16).padStart(8, '0');

  const prepareQuery = common();
  prepareQuery.set('msToken', '');
  const prepareResp = await fetch('/alice/resource/prepare_upload?' + prepareQuery, {
    method:'POST', credentials:'include', headers:{'content-type':'application/json'},
    body: JSON.stringify({tenant_id:'5', scene_id:'5', resource_type:2})
  });
  const prepareJson = await prepareResp.json();
  if (prepareResp.status !== 200 || prepareJson.code !== 0) {
    throw new Error('prepare_upload failed: ' + (prepareJson.msg || prepareResp.status));
  }
  const serviceId = prepareJson.data.service_id;

  const applyQuery = new URLSearchParams({
    Action:'ApplyImageUpload', Version:'2018-08-01', ServiceId:serviceId,
    NeedFallback:'true', FileSize:String(bytes.length), FileExtension:extension,
    s: Math.random().toString(36).slice(2, 12)
  });
  const applyResp = await fetch('/top/v1?' + applyQuery, {method:'GET', credentials:'include'});
  const applyJson = await applyResp.json();
  if (applyResp.status !== 200 || !applyJson.Result) {
    throw new Error('ApplyImageUpload failed: HTTP ' + applyResp.status);
  }
  const uploadAddress = applyJson.Result.UploadAddress || {};
  const store = (uploadAddress.StoreInfos || [])[0];
  if (!store) throw new Error('ApplyImageUpload missing StoreInfos');
  const uploadHost = (uploadAddress.UploadHosts || [])[0]
    || ((applyJson.Result.InnerUploadAddress || {}).UploadNodes || [])[0]?.UploadHost;
  if (!uploadHost) throw new Error('ApplyImageUpload missing UploadHost');
  const sessionKey = uploadAddress.SessionKey;
  const storeUri = store.StoreUri;
  const auth = store.Auth;

  // Prefer direct host upload with Authorization + Content-CRC32 (ImageX pattern).
  const uploadUrl = `https://${uploadHost}/upload/v1/${storeUri}`;
  let uploaded = false;
  let lastError = '';
  for (const url of [
    uploadUrl,
    `https://${uploadHost}/${storeUri}`,
  ]) {
    try {
      const uploadResp = await fetch(url, {
        method:'POST',
        headers:{
          'Authorization': auth,
          'Content-CRC32': crcHex,
          'Content-Type': mime || 'application/octet-stream',
        },
        body: bytes
      });
      if (uploadResp.ok || uploadResp.status === 200) {
        uploaded = true;
        break;
      }
      // some hosts expect PUT
      const putResp = await fetch(url, {
        method:'PUT',
        headers:{
          'Authorization': auth,
          'Content-CRC32': crcHex,
          'Content-Type': mime || 'application/octet-stream',
        },
        body: bytes
      });
      if (putResp.ok || putResp.status === 200) {
        uploaded = true;
        break;
      }
      lastError = `upload HTTP ${uploadResp.status}/${putResp.status} @ ${url}`;
    } catch (err) {
      lastError = String(err);
    }
  }
  if (!uploaded) throw new Error('image binary upload failed: ' + lastError);

  const commitQuery = new URLSearchParams({
    Action:'CommitImageUpload', Version:'2018-08-01', ServiceId:serviceId
  });
  const commitResp = await fetch('/top/v1?' + commitQuery, {
    method:'POST', credentials:'include', headers:{'content-type':'application/json'},
    body: JSON.stringify({SessionKey: sessionKey})
  });
  const commitJson = await commitResp.json();
  if (commitResp.status !== 200 || !commitJson.Result) {
    throw new Error('CommitImageUpload failed: HTTP ' + commitResp.status);
  }
  const plugin = ((commitJson.Result.PluginResult || [])[0]) || {};
  const uri = (commitJson.Result.Results || [])[0]?.Uri || plugin.ImageUri || storeUri;

  const identifier = (crypto.randomUUID ? crypto.randomUUID() : `${Date.now()}-${Math.random()}`);
  // uuid1-like not required; capture used time-based uuid1, uuid4 is accepted by pre_handle.
  const localMessageId = crypto.randomUUID ? crypto.randomUUID() : identifier;
  const preQuery = common();
  const preResp = await fetch('/alice/message/pre_handle_v2_without_conv?' + preQuery, {
    method:'POST', credentials:'include', headers:{'content-type':'application/json'},
    body: JSON.stringify({
      uplink_entity:{
        entity_type:2,
        entity_content:{image:{key: uri}},
        identifier
      },
      bot_id:'7338286299411103781',
      local_message_id: localMessageId
    })
  });
  const preJson = await preResp.json();
  if (preResp.status !== 200 || preJson.code !== 0) {
    throw new Error('pre_handle failed: ' + (preJson.msg || preResp.status));
  }

  return {
    identifier,
    uri,
    name: name || plugin.FileName || ('image' + extension),
    width: plugin.ImageWidth || null,
    height: plugin.ImageHeight || null,
    url: '',
    pre_generate_id: (preJson.data && preJson.data.pre_generate_id) || ''
  };
}
"""


async def read_browser_fingerprint(page, context) -> str:
    """旧版 fp(只取 s_v_web_id cookie)— v0.2.17 之前 main path,现在保留
    是为了 login 模块和外部脚本不破。视频提交已切到 load_browser_context。

    v0.2.19:async Playwright 重构后统一 async — login 模块的 sync 调用点
    也已迁移到 login/browser.py,本函数对外是 async。
    """
    await page.wait_for_function(
        "JSON.parse(localStorage.getItem('__tea_cache_tokens_497858') || '{}').user_unique_id",
        timeout=15_000,
    )
    cookies = await context.cookies(["https://www.doubao.com"])
    fingerprint = next(
        (cookie["value"] for cookie in cookies if cookie["name"] == "s_v_web_id"),
        "",
    )
    if not fingerprint:
        raise RuntimeError("豆包浏览器指纹不可用，请重新登录")
    return fingerprint


async def load_browser_context(page, context, *, pc_version: str | None = None) -> TokenBundle:
    """v0.2.17:从已登录 page + context 抽完整 TokenBundle。

    跟 read_browser_fingerprint 行为差:除了 fp cookie 还读 localStorage 的
    web_id / tea_uuid / device_id,组成 TokenBundle 给 payload.client_meta 透传。
    pc_version 优先用 settings 传进来的(从 SettingsService.get("pc_version")
    读),fallback 到模块级 PC_VERSION 常量。

    v0.2.19:async Playwright,所有 page.evaluate / context.cookies 前加 await。
    """
    effective_pc_version = pc_version or PC_VERSION
    await page.wait_for_function(
        "JSON.parse(localStorage.getItem('__tea_cache_tokens_497858') || '{}').user_unique_id",
        timeout=15_000,
    )
    cookies = await context.cookies(["https://www.doubao.com"])
    cookie_map = {cookie["name"]: cookie["value"] for cookie in cookies if cookie.get("name")}

    storage = await page.evaluate(
        "() => {"
        "  const read = (k) => { try { return JSON.parse(localStorage.getItem(k) || '{}') } catch { return {} } };"
        "  const tea = read('__tea_cache_tokens_497858');"
        "  const device = read('samantha_web_web_id');"
        "  return {"
        "    web_id: tea.web_id || '',"
        "    tea_uuid: tea.user_unique_id || '',"
        "    device_id: device.web_id || '',"
        "  };"
        "}"
    )
    if not isinstance(storage, dict):
        storage = {}

    web_id = storage.get("web_id") or cookie_map.get("samantha_web_web_id") or ""
    tea_uuid = storage.get("tea_uuid") or cookie_map.get("user_unique_id") or ""
    device_id = storage.get("device_id") or cookie_map.get("s_v_web_id") or ""
    fingerprint = cookie_map.get("s_v_web_id") or device_id

    if not fingerprint:
        raise RuntimeError("豆包浏览器指纹不可用，请重新登录")
    if not web_id:
        # 抽不到 web_id → 风控无解,UI 显示「去浏览器手动访问主页后点刷新 token」
        raise TokenBundleUnavailable(
            "profile 中缺少 web_id,请在浏览器里访问 https://www.doubao.com/chat/ "
            "主页 5-10 秒后点「刷新 token」"
        )

    return TokenBundle(
        ms_token=cookie_map.get("msToken", "") or cookie_map.get("ms_token", ""),
        web_id=web_id,
        web_id_signature=cookie_map.get("_signature", "") or cookie_map.get("samantha_web_id_signature", ""),
        device_id=device_id,
        tea_uuid=tea_uuid,
        pc_version=effective_pc_version,
    )


def _build_launch_kwargs(*, window_visible: bool = False) -> dict:
    """v0.2.17:launch_persistent_context 的「拟人化」参数。

    v0.2.22:加 `window_visible` 开关 —— 默认 False 保持 v0.2.21 隐身行为
    (窗口放到屏幕外 -2000,-2000);开启后窗口显示在 (80,80),与手动
    `POST /api/accounts/{id}/open-browser` 同位置。launch 后无法动态改
    位置,所以这个开关只在 BrowserContext 第一次创建时生效(同 profile
    重启进程才能换位置)。
    """
    position = "80,80" if window_visible else "-2000,-2000"
    return {
        "headless": False,
        "viewport": {
            "width": 940 + random.randint(-3, 3),
            "height": 650 + random.randint(-3, 3),
        },
        "args": [
            "--window-size=1000,720",
            f"--window-position={position}",
            *_STEALTH_LAUNCH_ARGS,
        ],
        "locale": _BROWSER_LOCALE,
        "timezone_id": _BROWSER_TIMEZONE,
        # v0.3.2.2:必须授 clipboard-read + clipboard-write,否则
        # navigator.clipboard.writeText() 抛 NotAllowedError,prompt
        # paste 路径整个挂掉。v0.3.2.1 写过 ["clipboard-read-write"]
        # —— **错的**,Playwright 校验严格,launch 立刻抛
        # `BrowserType.launch_persistent_context: Unknown permission:
        # clipboard-read-write`,浏览器闪退。Playwright 只接受
        # clipboard-read / clipboard-write 两个独立 permission(见
        # playwright/driver/.../coreBundle.js 的 permission map),
        # 所以这里必须拆成两条。
        # grant_permissions 走 launch_persistent_context 而不是
        # context.grant_permissions(后者的 origin 不是 https://www.doubao.com)。
        "permissions": ["clipboard-read", "clipboard-write"],
        "extra_http_headers": {
            "Referer": "https://www.doubao.com/chat/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    }


def _is_context_alive(context) -> bool:
    """v0.2.26:安全判断 BrowserContext 是否还活着。

    Playwright 的 `BrowserContext.is_closed()` 是 sync 调用,理论上不会抛,
    但旧版本 / 远程 driver / context 已被 GC 时偶尔抛 RuntimeError。这里
    统一用 try/except 兜底,异常一律视作「已关闭」,让调用方走清缓存 + 报错
    路径,不要把底层异常原文透出去到 UI。

    返回 True = 还能 new_page();False = context 已死,别再操作。
    """
    try:
        return not context.is_closed()
    except Exception:
        return False


def load_image_base64(path: Path) -> tuple[str, str, str]:
    data = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return path.name, mime, base64.b64encode(data).decode("ascii")


async def _handle_aegis_in_poll(
    page: Page,
    profile_dir: Path,
    update: Callable[..., None],
) -> bool:
    """轮询期只读探测 aegis；命中后立即中止当前 profile 的任务。"""
    del profile_dir
    if not await aegis_popup_present(page):
        return False
    try:
        update(error_message=AEGIS_BLOCKED_MESSAGE)
    except Exception:
        pass
    raise AegisBlocked(AEGIS_BLOCKED_MESSAGE)


AEGIS_BLOCKED_MESSAGE = "当前任务已停止，请在账号管理中打开该账号浏览器完成验证后重提"


class AegisBlocked(RuntimeError):
    """检测到 aegis；调用方必须停止任务并释放对应 profile。"""


class _AckWaitTimeout(RuntimeError):
    """提交后没有在时限内收到 /chat/completion 响应。"""

    def __init__(self, timeout: float, *, request_seen: bool) -> None:
        self.request_seen = request_seen
        super().__init__(
            f"等待 /chat/completion 响应超时 ({timeout}s; "
            f"request_seen={request_seen})"
        )


# v0.3.5.13:保留旧内部名称，避免外部集成/旧测试在升级时导入失败。
_AegisUnresolvableInPoll = AegisBlocked


async def _pre_submit_aegis_gate(
    page: Page,
    profile_dir: Path,
    update: Callable[..., None],
) -> None:
    """提交前轮询 aegis；无弹窗放行，命中则立即中止。"""
    del profile_dir
    deadline = time.monotonic() + _UI_AEGIS_WAIT_SECONDS
    while time.monotonic() < deadline:
        try:
            popup_present = await aegis_popup_present(page)
        except Exception as exc:
            _LOGGER.debug("aegis gate detect failed: %s", exc)
            popup_present = False
        if popup_present:
            try:
                update(error_message=AEGIS_BLOCKED_MESSAGE)
            except Exception:
                pass
            raise AegisBlocked(AEGIS_BLOCKED_MESSAGE)
        await asyncio.sleep(_UI_AEGIS_DETECT_POLL_INTERVAL)
    _LOGGER.debug("aegis gate: no popup within %.1fs, allow submit", _UI_AEGIS_WAIT_SECONDS)


# v0.3.2:真实 UI click 路径的核心 helper —— 替换掉 `page.evaluate(fetch /chat/completion)`。
# 思路:打开 `/chat/create-image` → 点「视频」tab → type prompt → 点 submit 按钮,
# 让豆包前端认为这是真实用户点击,绕过 shark_admin 服务端对 page.evaluate POST 的
# 识别(`sec-fetch-mode` / `request initiator` / `navigation params`)。
async def try_click(
    page: Page,
    selectors: tuple[str, ...],
    *,
    timeout: float = 10.0,
) -> None:
    """v0.3.2:按 selector 顺序试,首个可见 + 可点击的就点。

    使用 locator().first.wait_for(state='visible') → bounding_box →
    mouse.move/down/up 的真人点击路径。
    click 间隔用 random.randint(0, 80) 抖动 50-130ms,防止 aegis 时序风控
    识别出「匀速间隔 = 自动化」。click 后再 wait_for_timeout(150) 让 React
    处理完 click event,否则立刻读 ProseMirror 可能拿到旧节点。
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    for sel in selectors:
        if time.monotonic() >= deadline:
            break
        try:
            loc = page.locator(sel).first
            await loc.wait_for(
                state="visible",
                timeout=int((deadline - time.monotonic()) * 1000),
            )
            box = await loc.bounding_box()
            if not box or box["width"] <= 0 or box["height"] <= 0:
                continue
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            await page.mouse.move(cx, cy, steps=3)
            await page.mouse.down()
            await page.wait_for_timeout(50 + random.randint(0, 80))
            await page.mouse.up()
            await page.wait_for_timeout(150)
            return
        except Exception as exc:
            last_error = exc
            continue
    raise RuntimeError(
        f"try_click all selectors failed: {selectors} (last={last_error})"
    )


async def clear_prose_mirror(page: Page) -> None:
    """v0.3.2:清空 ProseMirror 输入框(retry 路径 revise 后用)。

    不能用 page.evaluate("el => el.innerHTML = ''"):ProseMirror 是 transactional
    编辑器,内部 model state 跟 DOM 同步靠 MutationObserver;直接改 innerHTML
    会让 state 错乱,下次 type 不生效。改用真人操作流程:click → Ctrl+A → Delete。
    """
    loc = page.locator(EDITOR_SEL).first
    await loc.click()
    await page.keyboard.press("Control+A")
    await page.keyboard.press("Delete")
    await page.wait_for_timeout(100)


@contextlib.asynccontextmanager
async def _ack_interceptor(page: Page):
    """v0.3.2:拦截 /chat/completion 响应的 async context manager。

    进入:装 page.on('response', _on_response) 监听
    退出:自动 remove_listener,避免 leak

    为什么用 page.on 而不是 context.on:
    - context.on 会拦截所有 page 的响应,跨 task 串台(同 account 多 task)
    - 一次 submit 只对应一个 page 的 /chat/completion,page.on 更精准
    - context manager 保证 listener 不 leak(每 task 独立,exit 时清理)

    v0.3.3:同时缓存解析后的 SSE_ACK payload(`state["ack_payload"]`),供
    race 防御读 `local_message_ids` 用 —— 浏览器侧生成的 id 我们 Python
    侧没法预生成,只能从 server echo 的 ack 里抓。
    """
    state: dict[str, object] = {"request_seen": False}

    def _on_request(request) -> None:
        if "/chat/completion" not in request.url:
            return
        state["request_seen"] = True
        state["request_ts"] = time.time()

    async def _on_response(response):
        url = response.url
        if "/chat/completion" not in url:
            return
        # response 存在必然意味着 request 已发出。这个兜底也让只提供
        # response 事件的测试替身维持真实语义。
        state["request_seen"] = True
        try:
            text = await response.text()
        except Exception as exc:
            _LOGGER.debug("ack interceptor read body failed: %s", exc)
            return
        state["text"] = text
        state["ts"] = time.time()
        # v0.3.3:从 SSE 流里抽 SSE_ACK 包的 data(整段 JSON),喂给
        # _extract_local_message_ids_from_ack_payload。失败不挂流程
        # —— race 防御是 best-effort,字段缺失就 fall through。
        try:
            ack_payload = _parse_sse_ack_payload_from_text(text)
            if ack_payload is not None:
                state["ack_payload"] = ack_payload
        except Exception:
            pass

    page.on("request", _on_request)
    page.on("response", _on_response)
    try:
        yield state
    finally:
        try:
            page.remove_listener("request", _on_request)
        except Exception:
            pass
        try:
            page.remove_listener("response", _on_response)
        except Exception:
            pass


async def _wait_for_ack(state: dict, *, timeout: float = _UI_ACK_WAIT_SECONDS) -> str:
    """v0.3.2:等拦截器抓到 /chat/completion 响应原文,返给 parse_sse_ack。

    3 字段契约(`conversation_id / section_id / question_id`)由 parse_sse_ack
    保证,service.py `update(**ack)` 解包不变。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if "text" in state:
            return str(state["text"])
        await asyncio.sleep(0.1)
    raise _AckWaitTimeout(
        timeout,
        request_seen=bool(state.get("request_seen", False)),
    )


def _parse_sse_ack_payload_from_text(text: str) -> dict | None:
    """v0.3.3:从完整 SSE 流里抽 SSE_ACK 包的 data JSON(整段)。

    与 `protocol.parse_sse_ack` 不同 —— 那个函数最后 return 一个 3 字段字典
    并丢掉 ack_payload。本函数只取 ack_payload 原文,不做拒绝文案扫描
    (由 parse_sse_ack 自己做)。

    返回 None = 没拿到 / JSON 解析失败。race 防御是 best-effort,失败直接
    走 fall through,不影响主流程。
    """
    try:
        for packet in text.replace("\r\n", "\n").split("\n\n"):
            event = ""
            data = ""
            for line in packet.splitlines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    data += line[5:].strip()
            if event == "SSE_ACK" and data:
                return json.loads(data)
    except (ValueError, json.JSONDecodeError):
        return None
    return None


def _extract_local_message_ids_from_ack_payload(ack_payload: dict) -> set[str]:
    """v0.3.3:从 SSE_ACK payload 里抽本任务 submit 时用过的 local_message_id。

    浏览器 UI 路径(`use_real_browser=True`)下 id 由前端 crypto.randomUUID
    生成,Python 侧拿不到。服务端 echo 回来的 ack_payload 通常在以下位置
    带 id(字节不同版本字段命名略有差异,只接受显式 local 字段):
      - `ack_client_meta.local_message_id`(单值)
      - `ack_client_meta.local_message_ids`(列表)
      - `query_list[].local_message_id`(每条 query 一个)
      - `query_list[].local_message_ids`(列表)

    通用 `message_id` 是服务端消息 ID(UUIDv1),不能当作浏览器生成的
    `local_message_id`(UUIDv4),否则并发 task 会拿到错误的 expected identity。

    字段缺失 / 全部空 → 返回空 set,调用方走 fall through 不阻塞正常任务。
    """
    ids: set[str] = set()

    def _ingest(value) -> None:
        if isinstance(value, str) and value:
            ids.add(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item:
                    ids.add(item)
                elif isinstance(item, dict):
                    inner = item.get("local_message_id")
                    _ingest(inner)

    meta = ack_payload.get("ack_client_meta") or {}
    _ingest(meta.get("local_message_id"))
    _ingest(meta.get("local_message_ids"))

    for query in ack_payload.get("query_list") or []:
        if not isinstance(query, dict):
            continue
        _ingest(query.get("local_message_id"))
        _ingest(query.get("local_message_ids"))

    return ids


def _extract_remote_task_ids_from_ack_payload(ack_payload: dict) -> set[str]:
    """v0.3.5:从 SSE_ACK payload 里抽本任务 submit 时分配的服务端 creation.id。

    与 `_extract_local_message_ids_from_ack_payload` 配对 —— 后者抽客户端
    local_message_id,本函数抽服务端 `creation.id`(被记为 remote_task_id,
    `parse_creation_result` payload 里的 `creation.id` 字段)。

    字节不同版本可能 echo 在不同位置,做宽口径 fallback:
      - 顶层 `remote_task_id` / `task_id` / `creation_id`(部分版本)
      - `ack_client_meta.remote_task_id` / `task_id` / `creation_id`
      - `query_list[].remote_task_id` / `task_id` / `creation_id`

    字段缺失 / 全部空 → 返回空 set,parse_creation_result 走兜底 candidates
    + 30s cooldown(由 `expected_local_message_ids` 这层兜底)。本函数是
    best-effort,失败不挂流程。
    """
    ids: set[str] = set()

    def _ingest(value) -> None:
        if isinstance(value, str) and value:
            ids.add(value)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str) and item:
                    ids.add(item)
                elif isinstance(item, dict):
                    inner = (
                        item.get("remote_task_id")
                        or item.get("task_id")
                        or item.get("creation_id")
                    )
                    _ingest(inner)

    # 顶层
    _ingest(ack_payload.get("remote_task_id"))
    _ingest(ack_payload.get("task_id"))
    _ingest(ack_payload.get("creation_id"))

    # ack_client_meta 子树
    meta = ack_payload.get("ack_client_meta") or {}
    _ingest(meta.get("remote_task_id"))
    _ingest(meta.get("task_id"))
    _ingest(meta.get("creation_id"))

    # query_list[].*
    for query in ack_payload.get("query_list") or []:
        if not isinstance(query, dict):
            continue
        _ingest(query.get("remote_task_id"))
        _ingest(query.get("task_id"))
        _ingest(query.get("creation_id"))

    return ids


async def _first_visible_locator(locator):
    """返回 locator 集合中的第一个可见元素。"""
    visible = await _visible_locators(locator)
    return visible[0] if visible else None


async def _visible_locators(locator) -> list:
    visible: list = []
    for index in range(await locator.count()):
        candidate = locator.nth(index)
        try:
            if await candidate.is_visible():
                visible.append(candidate)
        except Exception:
            continue
    return visible


async def _visible_button_texts(page: Page, *, limit: int = 20) -> list[str]:
    """返回当前页面前几个可见按钮文本，供 selector 失败诊断使用。"""
    texts: list[str] = []
    try:
        locator = page.locator("button, [role='button']")
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            try:
                if not await candidate.is_visible():
                    continue
                text = (await candidate.inner_text()).strip()
                if not text:
                    for attribute in ("aria-label", "title"):
                        value = await candidate.get_attribute(attribute)
                        if value and value.strip():
                            text = f"[{attribute}={value.strip()}]"
                            break
            except Exception:
                continue
            if text and text not in texts:
                texts.append(text)
            if len(texts) >= limit:
                break
    except Exception:
        pass
    return texts


def _exact_video_option_button(page: Page, text: str):
    return page.locator("button").filter(
        has_text=re.compile(rf"^\s*{re.escape(text)}\s*$")
    )


async def _visible_video_ratio_options(page: Page) -> list[str]:
    visible: list[str] = []
    for value in _VIDEO_RATIO_OPTIONS:
        locator = _exact_video_option_button(page, value)
        if await _first_visible_locator(locator) is not None:
            visible.append(value)
    return visible


async def _video_options_visibility_snapshot(
    page: Page,
) -> tuple[int | None, int | None]:
    """Read-only visibility counts used by the video-options diagnostics."""
    try:
        range_inputs = await _visible_locators(page.locator("input[type='range']"))
        aria_sliders = await _visible_locators(page.locator("[role='slider']"))
        duration_visible: int | None = len(range_inputs) + len(aria_sliders)
    except Exception:
        duration_visible = None
    try:
        ratio_visible: int | None = len(await _visible_video_ratio_options(page))
    except Exception:
        ratio_visible = None
    return duration_visible, ratio_visible


async def _wait_for_video_ratio_options(
    page: Page,
    *,
    timeout_ms: int = _VIDEO_OPTIONS_MENU_WAIT_MS,
) -> list[str]:
    step_ms = 50
    attempts = max(1, (timeout_ms + step_ms - 1) // step_ms)
    visible: list[str] = []
    for attempt in range(attempts):
        visible = await _visible_video_ratio_options(page)
        if len(visible) >= _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS:
            return visible
        if attempt + 1 < attempts:
            await page.wait_for_timeout(step_ms)
    return visible


async def _find_video_options_summary_trigger(page: Page):
    # 新版页面可能把组合控件渲染成 div[role=button]，优先走 a11y role，
    # 再用原生 button / role CSS 组合选择器兼容旧版和嵌套 span 文本。
    try:
        candidates = page.get_by_role("button").filter(
            has_text=_VIDEO_OPTIONS_TRIGGER_RE,
        )
        trigger = await _first_visible_locator(candidates)
        if trigger is not None:
            return trigger
    except Exception:
        pass

    candidates = page.locator("[role='button'], button").filter(
        has_text=_VIDEO_OPTIONS_TRIGGER_RE,
    )
    return await _first_visible_locator(candidates)


async def _video_options_locator_signature(locator) -> tuple:
    """为三点候选生成稳定签名，避免多个 selector 重复点击同一元素。"""
    try:
        text = " ".join((await locator.inner_text()).split())
    except Exception:
        text = ""
    attributes: list[str] = []
    for name in ("aria-label", "title", "class"):
        try:
            attributes.append((await locator.get_attribute(name)) or "")
        except Exception:
            attributes.append("")
    try:
        box = await locator.bounding_box()
    except Exception:
        box = None
    position = (
        tuple(
            round(float(box.get(key, 0)), 1)
            for key in ("x", "y", "width", "height")
        )
        if box
        else ()
    )
    return text, *attributes, position


async def _video_options_locator_signature_without_text(locator) -> tuple:
    """Read-only signature that avoids an extra inner_text() observation."""
    attributes: list[str] = []
    for name in ("aria-label", "title", "class"):
        try:
            attributes.append((await locator.get_attribute(name)) or "")
        except Exception:
            attributes.append("")
    try:
        box = await locator.bounding_box()
    except Exception:
        box = None
    position = (
        tuple(
            round(float(box.get(key, 0)), 1)
            for key in ("x", "y", "width", "height")
        )
        if box
        else ()
    )
    return "", *attributes, position


async def _find_video_more_options_trigger(
    page: Page,
    *,
    excluded_signatures: set[tuple] | None = None,
):
    """找到视频工具栏的三点参数入口，并避开页头「更多」和发送按钮。"""
    excluded_signatures = excluded_signatures or set()

    anchor_entries: list[tuple[tuple, dict[str, float]]] = []
    seen_anchor_signatures: set[tuple] = set()

    async def collect_anchors(locator) -> None:
        try:
            anchors = await _visible_locators(locator)
        except Exception:
            return
        for anchor in anchors:
            try:
                box = await anchor.bounding_box()
            except Exception:
                box = None
            if not box:
                continue
            signature = await _video_options_locator_signature(anchor)
            if signature in seen_anchor_signatures:
                continue
            seen_anchor_signatures.add(signature)
            anchor_entries.append((signature, box))

    try:
        await collect_anchors(
            page.locator("button, [role='button']").filter(
                has_text=_VIDEO_MORE_OPTIONS_ANCHOR_RE,
            )
        )
    except Exception:
        pass
    try:
        await collect_anchors(page.get_by_text(_VIDEO_MORE_OPTIONS_ANCHOR_RE))
    except Exception:
        pass

    ranked_by_signature: dict[tuple, tuple[tuple[float, ...], object]] = {}

    def remember_candidate(
        signature: tuple,
        rank: tuple[float, ...],
        candidate,
    ) -> None:
        current = ranked_by_signature.get(signature)
        if current is None or rank < current[0]:
            ranked_by_signature[signature] = (rank, candidate)

    selector_sources = [
        (selector_index, selector, False)
        for selector_index, selector in enumerate(
            _VIDEO_MORE_OPTIONS_SELECTORS
        )
    ]
    selector_sources.extend(
        (
            len(_VIDEO_MORE_OPTIONS_SELECTORS) + selector_index,
            selector,
            True,
        )
        for selector_index, selector in enumerate(
            _VIDEO_MORE_OPTIONS_GEOMETRY_SELECTORS
        )
    )
    weak_geometry_indexes = {
        2,  # svg path:nth-of-type(3)
        len(_VIDEO_MORE_OPTIONS_GEOMETRY_SELECTORS) - 1,
    }
    for selector_index, selector, geometry_candidate in selector_sources:
        try:
            candidates = await _visible_locators(page.locator(selector))
        except Exception:
            continue
        for candidate in candidates:
            try:
                candidate_text = " ".join(
                    (await candidate.inner_text()).split()
                )
            except Exception:
                candidate_text = ""
            try:
                candidate_label = (
                    (await candidate.get_attribute("aria-label")) or ""
                )
            except Exception:
                candidate_label = ""
            try:
                candidate_class = (
                    (await candidate.get_attribute("class")) or ""
                )
            except Exception:
                candidate_class = ""

            if not geometry_candidate and selector_index >= 4:
                label_lower = candidate_label.lower()
                class_lower = candidate_class.lower()
                semantic_match = bool(
                    _VIDEO_MORE_OPTIONS_TEXT_RE.fullmatch(candidate_text)
                    or "更多" in candidate_label
                    or "more" in label_lower
                    or "option" in label_lower
                    or "more" in class_lower
                    or "ellipsis" in class_lower
                    or (
                        ":has(svg" in selector
                        and not candidate_text
                    )
                )
                if not semantic_match:
                    continue
            try:
                excluded = await candidate.evaluate(
                    """el => Boolean(
                        el.closest('.send-btn-wrapper') ||
                        el.closest('[role="tab"]') ||
                        el.querySelector(
                            "svg path[d^='M4.93934 10.2598']"
                        )
                    )"""
                )
            except Exception:
                excluded = False
            if excluded:
                continue

            signature = await _video_options_locator_signature(candidate)
            if signature in excluded_signatures:
                continue

            if geometry_candidate:
                # 匿名 SVG 只能在存在「模型 / Seedance」锚点时启用；
                # 否则页面全局图标按钮太多，误点风险不可接受。
                if not anchor_entries or signature in seen_anchor_signatures:
                    continue
                candidate_box = signature[-1]
                if not candidate_box:
                    continue
                candidate_x, candidate_y, candidate_width, candidate_height = (
                    float(value) for value in candidate_box
                )
                if (
                    candidate_width <= 0
                    or candidate_height <= 0
                    or candidate_width > _VIDEO_MORE_OPTIONS_MAX_ICON_SIZE_PX
                    or candidate_height > _VIDEO_MORE_OPTIONS_MAX_ICON_SIZE_PX
                ):
                    continue

                placements: list[tuple[float, float]] = []
                candidate_center_y = candidate_y + candidate_height / 2
                for _, anchor_box in anchor_entries:
                    anchor_right = float(anchor_box.get("x", 0)) + float(
                        anchor_box.get("width", 0)
                    )
                    anchor_center_y = float(anchor_box.get("y", 0)) + float(
                        anchor_box.get("height", 0)
                    ) / 2
                    horizontal_gap = candidate_x - anchor_right
                    vertical_delta = abs(candidate_center_y - anchor_center_y)
                    if (
                        -8 <= horizontal_gap
                        <= _VIDEO_MORE_OPTIONS_MAX_HORIZONTAL_GAP_PX
                        and vertical_delta
                        <= _VIDEO_MORE_OPTIONS_MAX_VERTICAL_DELTA_PX
                    ):
                        placements.append((horizontal_gap, vertical_delta))
                if not placements:
                    continue
                horizontal_gap, vertical_delta = min(placements)
                geometry_index = (
                    selector_index - len(_VIDEO_MORE_OPTIONS_SELECTORS)
                )
                # 三圆/三矩形/use/class 等结构信号优先于最终的
                # 任意 SVG 兜底；单纯 path 数量区分不了麦克风等复杂
                # 图标，因此与 generic 通道同级。同一可信级别内再
                # 选择离模型最近者。
                geometry_priority = (
                    1.0 if geometry_index in weak_geometry_indexes else 0.0
                )
                remember_candidate(
                    signature,
                    (
                        2.0 + geometry_priority,
                        horizontal_gap,
                        vertical_delta,
                        float(selector_index),
                    ),
                    candidate,
                )
                continue

            candidate_box = signature[-1]
            distance = float("inf")
            if anchor_entries and candidate_box:
                candidate_x = candidate_box[0] + candidate_box[2] / 2
                candidate_y = candidate_box[1] + candidate_box[3] / 2
                distances = []
                for _, anchor_box in anchor_entries:
                    anchor_x = float(anchor_box.get("x", 0)) + float(
                        anchor_box.get("width", 0)
                    ) / 2
                    anchor_y = float(anchor_box.get("y", 0)) + float(
                        anchor_box.get("height", 0)
                    ) / 2
                    distances.append(
                        (candidate_x - anchor_x) ** 2
                        + (candidate_y - anchor_y) ** 2
                    )
                distance = min(distances)
                if (
                    selector_index >= 4
                    and distance
                    > _VIDEO_MORE_OPTIONS_MAX_ANCHOR_DISTANCE_PX ** 2
                ):
                    continue

            # 明确的文本/a11y/class 语义最可信。相邻兄弟只证明位置，
            # 可能恰好是麦克风，因此排在明确三点 SVG 结构之后、任意
            # SVG 兜底之前。同一 DOM 若被多个 selector 命中，保留其
            # 最可信的 rank，而不是被第一个 selector 永久定级。
            semantic_priority = 2.5 if selector_index < 4 else 1.0
            remember_candidate(
                signature,
                (
                    semantic_priority,
                    distance,
                    0.0,
                    float(selector_index),
                ),
                candidate,
            )

    if not ranked_by_signature:
        return None
    ranked = list(ranked_by_signature.values())
    ranked.sort(key=lambda item: item[0])
    return ranked[0][1]


async def _diagnose_three_dot_candidates(page: Page) -> list[dict]:
    """失败时收集可见 SVG 按钮的结构和位置，不参与正常定位。"""
    try:
        result = await page.evaluate(
            """() => {
                const visibleBox = (el) => {
                    const box = el.getBoundingClientRect();
                    const style = window.getComputedStyle(el);
                    return box.width > 0 && box.height > 0 &&
                        style.visibility !== 'hidden' &&
                        style.display !== 'none'
                        ? box : null;
                };
                const modelPattern = /(?:模型|model|seedance)/i;
                const anchors = Array.from(document.querySelectorAll(
                    'button, [role="button"]'
                )).map(el => ({el, box: visibleBox(el)})).filter(item =>
                    item.box && modelPattern.test(item.el.innerText || '')
                );
                const candidates = Array.from(document.querySelectorAll(
                    'button, [role="button"], .semi-button'
                )).map(el => ({el, box: visibleBox(el)})).filter(item =>
                    item.box && item.el.querySelector('svg')
                );
                return candidates.map(({el, box}) => {
                    const svg = el.querySelector('svg');
                    const use = svg && svg.querySelector('use');
                    let nearest = null;
                    for (const anchor of anchors) {
                        const dx = Math.round(
                            box.x - (anchor.box.x + anchor.box.width)
                        );
                        const dy = Math.round(Math.abs(
                            box.y + box.height / 2 -
                            (anchor.box.y + anchor.box.height / 2)
                        ));
                        if (!nearest || Math.hypot(dx, dy) < nearest.distance) {
                            nearest = {dx, dy, distance: Math.hypot(dx, dy)};
                        }
                    }
                    return {
                        aria_label: el.getAttribute('aria-label'),
                        title: el.getAttribute('title'),
                        class_name: el.getAttribute('class'),
                        data_testid: el.getAttribute('data-testid'),
                        inner_text: (el.innerText || '').trim().slice(0, 40),
                        svg_class: svg && svg.getAttribute('class'),
                        svg_view_box: svg && svg.getAttribute('viewBox'),
                        circle_count: svg ? svg.querySelectorAll('circle').length : 0,
                        rect_count: svg ? svg.querySelectorAll('rect').length : 0,
                        path_count: svg ? svg.querySelectorAll('path').length : 0,
                        use_href: use && (
                            use.getAttribute('href') ||
                            use.getAttribute('xlink:href')
                        ),
                        bbox: {
                            x: Math.round(box.x),
                            y: Math.round(box.y),
                            width: Math.round(box.width),
                            height: Math.round(box.height),
                        },
                        inside_send_wrapper: Boolean(
                            el.closest('.send-btn-wrapper')
                        ),
                        inside_tab: Boolean(el.closest('[role="tab"]')),
                        nearest_model_dx: nearest && nearest.dx,
                        nearest_model_dy: nearest && nearest.dy,
                        nearest_model_distance: nearest && Math.round(
                            nearest.distance
                        ),
                    };
                }).sort((left, right) => {
                    const leftDistance = left.nearest_model_distance ?? Infinity;
                    const rightDistance = right.nearest_model_distance ?? Infinity;
                    if (leftDistance !== rightDistance) {
                        return leftDistance - rightDistance;
                    }
                    const leftExcluded = Number(
                        left.inside_send_wrapper || left.inside_tab
                    );
                    const rightExcluded = Number(
                        right.inside_send_wrapper || right.inside_tab
                    );
                    return leftExcluded - rightExcluded;
                }).slice(0, 20);
            }"""
        )
    except Exception as exc:
        return [{"error": str(exc)}]
    return result if isinstance(result, list) else []


async def _video_options_trigger_kind(trigger) -> str:
    try:
        text = await trigger.inner_text()
    except Exception:
        text = ""
    return "A" if _VIDEO_OPTIONS_TRIGGER_RE.search(text or "") else "B"


async def _find_video_options_trigger(
    page: Page,
    *,
    prefer_more: bool = False,
    excluded_more_signatures: set[tuple] | None = None,
    return_kind: bool = False,
):
    if prefer_more:
        trigger = await _find_video_more_options_trigger(
            page,
            excluded_signatures=excluded_more_signatures,
        )
        if trigger is not None and return_kind:
            return trigger, "B"
        return trigger

    trigger = await _find_video_options_summary_trigger(page)
    if trigger is not None:
        return (trigger, "A") if return_kind else trigger
    trigger = await _find_video_more_options_trigger(
        page,
        excluded_signatures=excluded_more_signatures,
    )
    if trigger is not None and return_kind:
        return trigger, "B"
    return trigger


async def _wait_for_video_options_trigger(
    page: Page,
    *,
    excluded_more_signatures: set[tuple] | None = None,
    return_kind: bool = False,
    prefer_more: bool = False,
):
    step_ms = 50
    attempts = max(
        1,
        (_VIDEO_OPTIONS_TRIGGER_WAIT_MS + step_ms - 1) // step_ms,
    )
    for attempt in range(attempts):
        trigger = await _find_video_options_trigger(
            page,
            prefer_more=prefer_more,
            excluded_more_signatures=excluded_more_signatures,
            return_kind=return_kind,
        )
        if trigger is not None:
            return trigger
        if attempt + 1 < attempts:
            await page.wait_for_timeout(step_ms)
    selector_matches: dict[str, int | str] = {}
    for selector in _VIDEO_MORE_OPTIONS_ALL_SELECTORS:
        try:
            selector_matches[selector] = len(
                await _visible_locators(page.locator(selector))
            )
        except Exception as exc:
            selector_matches[selector] = f"error:{exc}"
    three_dot_candidates = await _diagnose_three_dot_candidates(page)
    _LOGGER.warning(
        "event=video_options_trigger_not_found url=%s visible_buttons=%s "
        "trigger_pattern=%r more_selectors=%s selector_matches=%s "
        "three_dot_candidates=%s",
        page.url,
        await _visible_button_texts(page),
        _VIDEO_OPTIONS_TRIGGER_RE.pattern,
        _VIDEO_MORE_OPTIONS_ALL_SELECTORS,
        selector_matches,
        three_dot_candidates,
    )
    return None


async def _visible_texts_for_locator(locator) -> list[str]:
    """读取 locator 中可见元素的文本，失败时退回 all_text_contents。"""
    texts: list[str] = []
    try:
        for index in range(await locator.count()):
            candidate = locator.nth(index)
            if not await candidate.is_visible():
                continue
            text = " ".join((await candidate.inner_text()).split())
            if text and text not in texts:
                texts.append(text)
        return texts
    except Exception:
        pass
    try:
        for value in await locator.all_text_contents():
            text = " ".join(value.split())
            if text and text not in texts:
                texts.append(text)
    except Exception:
        pass
    return texts


async def _visible_video_tab_texts(page: Page) -> list[str]:
    """收集 tab 和视频相关按钮文本，供切换失败诊断使用。"""
    locators = []
    try:
        locators.append(page.locator("[role='tab']"))
    except Exception:
        pass
    try:
        locators.append(page.get_by_role("tab"))
    except Exception:
        pass
    try:
        locators.append(page.locator(".semi-tabs-tab"))
    except Exception:
        pass
    try:
        locators.append(
            page.locator("button").filter(
                has_text=re.compile(r"视频|video", re.IGNORECASE),
            )
        )
    except Exception:
        pass
    try:
        locators.append(
            page.locator("[role='button']").filter(
                has_text=re.compile(r"视频|video", re.IGNORECASE),
            )
        )
    except Exception:
        pass

    texts: list[str] = []
    for locator in locators:
        for text in await _visible_texts_for_locator(locator):
            if text not in texts:
                texts.append(text)
    # 即使页面没有任何 tab，也保留前几个可见按钮，便于识别仍停在
    # 侧边栏/登录页等错误状态。
    for text in await _visible_button_texts(page, limit=10):
        if text not in texts:
            texts.append(text)
    return texts


async def _wait_for_visible_video_tab_candidate(page: Page, selector: str):
    """在短窗口内寻找 selector 对应的第一个可见元素。"""
    step_ms = 50
    attempts = max(1, (3_000 + step_ms - 1) // step_ms)
    for attempt in range(attempts):
        try:
            candidate = await _first_visible_locator(page.locator(selector))
        except Exception:
            candidate = None
        if candidate is not None:
            return candidate
        if attempt + 1 < attempts:
            await page.wait_for_timeout(step_ms)
    return None


async def _best_effort_escape_video_options(page: Page) -> None:
    """关闭 TAB 校验临时打开的参数菜单，不让后续 apply 再次 toggle 错状态。"""
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(100)
    except Exception:
        pass


async def _validate_video_tab_content(page: Page) -> list[str]:
    """确认视频内容已经 mount，并返回至少四个可见比例选项。"""
    visible_options = await _wait_for_video_ratio_options(
        page,
        timeout_ms=_VIDEO_TAB_RATIO_WAIT_MS,
    )
    if len(visible_options) >= _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS:
        trigger_result = await _find_video_options_trigger(
            page,
            prefer_more=True,
            return_kind=True,
        )
        if trigger_result is None:
            trigger_result = await _find_video_options_trigger(
                page,
                return_kind=True,
            )
        if trigger_result is not None:
            trigger, trigger_kind = trigger_result
            try:
                await _close_video_options(
                    page,
                    trigger,
                    trigger_kind=trigger_kind,
                )
            except Exception as exc:
                await _best_effort_escape_video_options(page)
                raise RuntimeError("视频 TAB 校验后参数菜单未关闭") from exc
        else:
            await _best_effort_escape_video_options(page)
        return visible_options

    # 比例按钮通常在参数弹层内。复用正式打开流程，同时兼容「三点 →
    # 参数摘要 → 比例」两级菜单；校验完成后关闭，交给 apply 重开。
    trigger = None
    trigger_kind = None
    try:
        trigger, visible_options, trigger_kind = await _open_video_options(page)
        return visible_options
    except RuntimeError:
        return visible_options
    finally:
        if trigger is not None:
            try:
                await _close_video_options(
                    page,
                    trigger,
                    trigger_kind=trigger_kind,
                )
            except Exception as exc:
                await _best_effort_escape_video_options(page)
                raise RuntimeError("视频 TAB 校验后参数菜单未关闭") from exc


async def _click_video_tab_candidate(page: Page, candidate) -> None:
    """用与 try_click 相同的鼠标路径点击已选中的 TAB 元素。"""
    try:
        box = await candidate.bounding_box()
    except Exception:
        box = None
    if box and box.get("width", 0) > 0 and box.get("height", 0) > 0:
        try:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            await page.mouse.move(cx, cy, steps=3)
            await page.mouse.down()
            await page.wait_for_timeout(50 + random.randint(0, 80))
            await page.mouse.up()
            await page.wait_for_timeout(150)
            return
        except Exception:
            # 坐标点击失败时继续走 locator click，保留 selector 兜底。
            pass
    try:
        await candidate.click(timeout=3_000)
    except TypeError:
        await candidate.click()


async def _activate_legacy_video_tab(page: Page) -> None:
    """激活旧页面的视频 TAB，不打开或读取已停用的视频参数菜单。"""
    errors: list[str] = []
    seen_candidates: set[tuple] = set()
    for selector in _VIDEO_TAB_SELECTORS:
        candidate = await _wait_for_visible_video_tab_candidate(page, selector)
        if candidate is None:
            errors.append(f"{selector}:not_found")
            continue
        signature = await _video_tab_candidate_signature(candidate)
        if signature in seen_candidates:
            errors.append(f"{selector}:duplicate_candidate")
            continue
        seen_candidates.add(signature)

        selected = False
        try:
            for name, value in (("aria-selected", "true"), ("data-state", "active")):
                if (await candidate.get_attribute(name)) == value:
                    selected = True
                    break
        except Exception:
            selected = False
        if not selected:
            await _click_video_tab_candidate(page, candidate)
            await page.wait_for_timeout(_VIDEO_TAB_MOUNT_WAIT_MS)
        _LOGGER.info(
            "event=video_legacy_tab_active url=%s selector=%s already_selected=%s",
            page.url,
            selector,
            selected,
        )
        return

    visible_tabs = await _visible_video_tab_texts(page)
    raise RuntimeError(
        "旧视频 TAB 未找到: "
        f"当前可见 tabs={visible_tabs}, attempts={errors}, url={page.url}"
    )


async def _video_tab_candidate_signature(candidate) -> tuple:
    """生成签名，避免 role/button 兜底重复点击同一个 DOM 节点。"""
    try:
        text = " ".join((await candidate.inner_text()).split())
    except Exception:
        text = ""
    try:
        box = await candidate.bounding_box()
    except Exception:
        box = None
    if box:
        position = tuple(
            round(float(box.get(key, 0)), 1)
            for key in ("x", "y", "width", "height")
        )
    else:
        position = ()
    return text, position


async def _click_video_tab(page: Page) -> None:
    """点击视频 TAB，并确认视频参数内容已经真正挂载。"""
    initial_tabs = await _visible_video_tab_texts(page)
    _LOGGER.info(
        "event=video_tab_click_start url=%s tabs=%s",
        page.url,
        initial_tabs,
    )

    errors: list[str] = []
    seen_candidates: set[tuple] = set()
    for selector in _VIDEO_TAB_SELECTORS:
        candidate = await _wait_for_visible_video_tab_candidate(page, selector)
        if candidate is None:
            errors.append(f"{selector}:not_found")
            continue
        signature = await _video_tab_candidate_signature(candidate)
        if signature in seen_candidates:
            errors.append(f"{selector}:duplicate_candidate")
            continue
        seen_candidates.add(signature)
        candidate_clicked = False
        try:
            await _click_video_tab_candidate(page, candidate)
            candidate_clicked = True
            await page.wait_for_timeout(_VIDEO_TAB_MOUNT_WAIT_MS)
            visible_options = await _validate_video_tab_content(page)
            if len(visible_options) >= _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS:
                _LOGGER.info(
                    "event=video_tab_click_ok url=%s selector=%s "
                    "ratio_options=%s",
                    page.url,
                    selector,
                    visible_options,
                )
                return
            errors.append(f"{selector}:ratio_options={visible_options}")
            await _best_effort_escape_video_options(page)
        except Exception as exc:
            errors.append(f"{selector}:{exc}")
            if candidate_clicked:
                await _best_effort_escape_video_options(page)

    final_tabs = await _visible_video_tab_texts(page)
    _LOGGER.warning(
        "event=video_tab_click_failed url=%s tabs=%s attempts=%s",
        page.url,
        final_tabs,
        errors,
    )
    raise RuntimeError(
        f"视频 TAB 未切换: 当前可见 tabs={final_tabs}, url={page.url}"
    )


async def _open_video_options(page: Page):
    last_error: Exception | None = None
    visible_options: list[str] = []
    attempt_errors: list[str] = []
    more_attempts: dict[tuple, int] = {}
    excluded_more_signatures: set[tuple] = set()
    summary_attempts = 0
    prefer_more = False
    for attempt in range(10):
        _LOGGER.debug(
            "event=video_options_open_phase start_url=%s attempt=%s",
            page.url,
            attempt,
        )
        trigger_result = await _wait_for_video_options_trigger(
            page,
            excluded_more_signatures=excluded_more_signatures,
            return_kind=True,
            prefer_more=prefer_more,
        )
        if trigger_result is None:
            if attempt == 0:
                raise RuntimeError(
                    f"视频参数按钮未找到: {page.url}; "
                    f"visible_buttons={await _visible_button_texts(page)}; "
                    f"more_selectors={_VIDEO_MORE_OPTIONS_ALL_SELECTORS}"
                )
            break
        trigger, trigger_kind = trigger_result
        trigger_signature = (
            await _video_options_locator_signature(trigger)
            if trigger_kind == "B"
            else None
        )
        candidate_clicked = False
        try:
            await trigger.click()
            candidate_clicked = True
            if _LOGGER.isEnabledFor(logging.DEBUG):
                diagnostic_trigger_signature = (
                    trigger_signature
                    if trigger_signature is not None
                    else await _video_options_locator_signature_without_text(trigger)
                )
                duration_visible, ratio_visible = (
                    await _video_options_visibility_snapshot(page)
                )
                _LOGGER.debug(
                    "event=video_options_after_click kind=%s "
                    "trigger_signature=%r duration_visible=%s ratio_visible=%s",
                    trigger_kind,
                    diagnostic_trigger_signature,
                    duration_visible,
                    ratio_visible,
                )
            visible_options = await _wait_for_video_ratio_options(page)

            # 少数灰度 UI 是两级菜单：三点先展开外层，再点外层中的
            # 「自动 · 10s」摘要项才出现比例 chips。
            if (
                trigger_kind == "B"
                and len(visible_options) < _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS
            ):
                summary = await _find_video_options_summary_trigger(page)
                if summary is not None:
                    await summary.click()
                    visible_options = await _wait_for_video_ratio_options(page)

            if len(visible_options) >= _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS:
                try:
                    aria_label = await trigger.get_attribute("aria-label")
                except Exception:
                    aria_label = None
                _LOGGER.info(
                    "event=video_options_trigger_kind kind=%s "
                    "aria_label=%r signature=%r url=%s",
                    trigger_kind,
                    aria_label,
                    trigger_signature,
                    page.url,
                )
                return trigger, visible_options, trigger_kind
            attempt_errors.append(
                f"kind={trigger_kind} signature={trigger_signature!r} "
                f"visible_ratio_options={visible_options}"
            )
        except Exception as exc:
            last_error = exc
            attempt_errors.append(
                f"kind={trigger_kind} signature={trigger_signature!r} "
                f"error={exc}"
            )

        # 只有确认旧参数面板完成退场，才允许尝试下一个候选；否则旧
        # chips 会让错误的全局「更多」看起来像打开成功。
        if candidate_clicked:
            try:
                await _close_video_options(
                    page,
                    trigger,
                    trigger_kind=trigger_kind,
                )
                remaining = await _wait_for_video_options_closed(page)
                if len(remaining) >= _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS:
                    raise RuntimeError(f"visible_ratio_options={remaining}")
            except Exception as exc:
                raise RuntimeError(
                    "视频参数候选失败后菜单未关闭: "
                    f"kind={trigger_kind} url={page.url}"
                ) from exc
            await page.wait_for_timeout(300)
        else:
            await _best_effort_escape_video_options(page)

        if trigger_kind == "B" and trigger_signature is not None:
            more_attempts[trigger_signature] = (
                more_attempts.get(trigger_signature, 0) + 1
            )
            if more_attempts[trigger_signature] >= 2:
                excluded_more_signatures.add(trigger_signature)
        else:
            summary_attempts += 1
            if summary_attempts >= 2:
                prefer_more = True
    raise RuntimeError(
        f"视频参数菜单未展开: {page.url}; "
        f"visible_ratio_options={visible_options}; attempts={attempt_errors}; "
        f"last={last_error}"
    )


async def _find_video_duration_control(page: Page):
    range_inputs = await _visible_locators(page.locator("input[type='range']"))
    aria_sliders = await _visible_locators(page.locator("[role='slider']"))
    if len(range_inputs) + len(aria_sliders) > 1:
        raise RuntimeError(
            "视频时长滑块定位不唯一: "
            f"range={len(range_inputs)} aria={len(aria_sliders)}"
        )
    if range_inputs:
        return "range", range_inputs[0]

    if aria_sliders:
        return "aria", aria_sliders[0]
    return None, None


async def _video_options_duration_search_snapshot(
    page: Page,
) -> tuple[str, int]:
    """Read-only range/ARIA slider count for duration-search diagnostics."""
    try:
        range_count = len(
            await _visible_locators(page.locator("input[type='range']"))
        )
    except Exception:
        range_count = 0
    try:
        aria_count = len(
            await _visible_locators(page.locator("[role='slider']"))
        )
    except Exception:
        aria_count = 0
    if range_count:
        return "range", range_count + aria_count
    if aria_count:
        return "aria", aria_count
    return "none", 0


async def _wait_for_video_duration_control(page: Page, *, timeout_ms: int = 500):
    step_ms = 50
    attempts = max(1, (timeout_ms + step_ms - 1) // step_ms)
    started = time.monotonic()
    for attempt in range(attempts):
        try:
            kind, control = await _find_video_duration_control(page)
        except Exception:
            if _LOGGER.isEnabledFor(logging.DEBUG):
                visible_kind, visible_count = (
                    await _video_options_duration_search_snapshot(page)
                )
                _LOGGER.debug(
                    "event=video_options_duration_search step_ms=%s total_ms=%s "
                    "visible_kind=%s visible_count=%s",
                    step_ms,
                    round((time.monotonic() - started) * 1000),
                    visible_kind,
                    visible_count,
                )
            raise
        if _LOGGER.isEnabledFor(logging.DEBUG):
            visible_kind, visible_count = (
                await _video_options_duration_search_snapshot(page)
            )
            _LOGGER.debug(
                "event=video_options_duration_search step_ms=%s total_ms=%s "
                "visible_kind=%s visible_count=%s",
                step_ms,
                round((time.monotonic() - started) * 1000),
                visible_kind,
                visible_count,
            )
        if control is not None:
            return kind, control
        if attempt + 1 < attempts:
            await page.wait_for_timeout(step_ms)
    return None, None


async def _set_native_range_value(control, duration: int) -> None:
    try:
        await control.fill(str(duration))
    except Exception:
        await control.evaluate(
            """(el, value) => {
                const setter = Object.getOwnPropertyDescriptor(
                    HTMLInputElement.prototype, 'value'
                ).set;
                setter.call(el, String(value));
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            duration,
        )

    try:
        actual = int(float(await control.input_value()))
    except (TypeError, ValueError):
        actual = None
    if actual != duration:
        raise RuntimeError(
            f"视频时长滑块设置失败: expected={duration}s actual={actual!r}"
        )


async def _set_aria_slider_value(page: Page, control, duration: int) -> None:
    if not _VIDEO_DURATION_MIN_SECONDS <= duration <= _VIDEO_DURATION_MAX_SECONDS:
        raise ValueError(
            "视频时长必须在 "
            f"{_VIDEO_DURATION_MIN_SECONDS} 到 "
            f"{_VIDEO_DURATION_MAX_SECONDS} 秒之间: {duration}"
        )

    try:
        raw_min = int(float(
            await control.get_attribute("aria-valuemin")
            or _VIDEO_DURATION_MIN_SECONDS
        ))
        raw_max = int(float(
            await control.get_attribute("aria-valuemax")
            or _VIDEO_DURATION_MAX_SECONDS
        ))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("视频时长滑块值域不可解析") from exc

    supported_domains = {
        (_VIDEO_DURATION_MIN_SECONDS, _VIDEO_DURATION_MAX_SECONDS),
        (0, _VIDEO_DURATION_MAX_SECONDS - _VIDEO_DURATION_MIN_SECONDS),
    }
    if (raw_min, raw_max) not in supported_domains:
        raise RuntimeError(
            "视频时长滑块值域不匹配: "
            f"raw_min={raw_min} raw_max={raw_max} "
            f"supported_domains={sorted(supported_domains)}"
        )
    # 豆包当前有两种 ARIA 值域：旧 UI 直接用 4..15 秒，新 Radix UI
    # 用 0..11 作为 4..15 秒的索引。两者跨度都为 11，因此统一把秒数
    # 映射到 raw_min 起算的离散索引。
    target_raw = raw_min + (duration - _VIDEO_DURATION_MIN_SECONDS)

    async def _slider_probe(phase: str) -> None:
        """Emit a read-only snapshot of the selected slider in DEBUG logs."""
        if not _LOGGER.isEnabledFor(logging.DEBUG):
            return
        try:
            tag_name = await control.evaluate("el => el.tagName")
        except Exception:
            tag_name = None
        attrs: dict[str, str | None] = {}
        for name in (
            "aria-label",
            "role",
            "data-testid",
            "aria-valuemin",
            "aria-valuemax",
            "aria-valuenow",
            "aria-valuetext",
        ):
            try:
                attrs[name] = await control.get_attribute(name)
            except Exception:
                attrs[name] = None
        try:
            box = await control.bounding_box()
        except Exception:
            box = None
        try:
            data_attrs = await control.evaluate(
                """el => {
                    for (let node = el; node; node = node.parentElement) {
                        const attrs = Object.fromEntries(
                            [...node.attributes]
                                .filter(attr => attr.name.startsWith('data-'))
                                .map(attr => [attr.name, attr.value])
                        );
                        if (Object.keys(attrs).length) return attrs;
                    }
                    return {};
                }"""
            )
        except Exception:
            data_attrs = None
        try:
            ancestor = control.locator(
                "xpath=ancestor::*[contains(@class,'option') or "
                "contains(@class,'panel') or @role='dialog' or "
                "@data-slot='slider'][1]"
            )
            ancestor_html = (
                await ancestor.evaluate("el => el.outerHTML.slice(0, 400)")
                if await ancestor.count()
                else None
            )
        except Exception:
            ancestor_html = None
        _LOGGER.debug(
            "event=aria_slider_probe phase=%s tag=%r aria_label=%r role=%r "
            "data_testid=%r data_attrs=%r aria_valuemin=%r aria_valuemax=%r "
            "aria_valuenow=%r aria_valuetext=%r bbox=%r ancestor_html=%r",
            phase,
            tag_name,
            attrs["aria-label"],
            attrs["role"],
            attrs["data-testid"],
            data_attrs,
            attrs["aria-valuemin"],
            attrs["aria-valuemax"],
            attrs["aria-valuenow"],
            attrs["aria-valuetext"],
            box,
            ancestor_html,
        )

    await _slider_probe("before")
    keyboard_now: str | None = None
    try:
        await control.focus()
        await control.press("Home")
        for _ in range(target_raw - raw_min):
            await control.press("ArrowRight")
        await page.wait_for_timeout(100)
        keyboard_now = await control.get_attribute("aria-valuenow")
    except Exception:
        keyboard_now = None

    _LOGGER.debug(
        "event=aria_slider_set_path path=keyboard requested_seconds=%s "
        "target_raw=%s observed_raw=%r",
        duration,
        target_raw,
        keyboard_now,
    )

    # Dispatch the native value/change events as a second path.  This is
    # useful for React wrappers that ignore Playwright's synthetic key events;
    # the mouse path below remains the authoritative fallback for custom
    # div[role=slider] implementations.
    try:
        await control.evaluate(
            """(el, value) => {
                const desc = el.tagName === 'INPUT'
                    ? Object.getOwnPropertyDescriptor(
                        HTMLInputElement.prototype, 'value'
                    )
                    : null;
                if (desc && desc.set) {
                    desc.set.call(el, String(value));
                }
                el.dispatchEvent(new Event('input', {bubbles: true}));
                el.dispatchEvent(new Event('change', {bubbles: true}));
            }""",
            target_raw,
        )
    except Exception as exc:
        _LOGGER.debug("event=aria_slider_set_path path=native error=%r", exc)
    native_now: str | None = None
    try:
        native_now = await control.get_attribute("aria-valuenow")
    except Exception:
        pass
    _LOGGER.debug(
        "event=aria_slider_set_path path=native requested_seconds=%s "
        "target_raw=%s observed_raw=%r",
        duration,
        target_raw,
        native_now,
    )

    # 不把 ``aria-valuenow`` 变成目标值当作成功：对 React 受控的
    # ``div[role=slider]``，上面的 DOM 属性/事件路径可能只改了 DOM，
    # React state 仍保持旧值。始终走真实鼠标路径，让控件自己的事件链
    # 提交状态；最后再用 aria-valuenow 做严格校验。
    thumb_box = await control.bounding_box()
    track_box = None
    try:
        radix_track = control.locator(
            "xpath=ancestor::*[@data-slot='slider'][1]"
            "//*[@data-slot='slider-track']"
        )
        if await radix_track.count():
            track_box = await radix_track.bounding_box()
    except Exception:
        track_box = None
    if (
        not track_box
        or not thumb_box
        or track_box["width"] <= thumb_box["width"]
    ):
        try:
            parent_box = await control.locator("xpath=..").bounding_box()
        except Exception:
            parent_box = None
        if (
            parent_box
            and thumb_box
            and parent_box["width"] > thumb_box["width"]
        ):
            track_box = parent_box
    if (
        not thumb_box
        or not track_box
        or track_box["width"] <= thumb_box["width"]
    ):
        raise RuntimeError("视频时长滑块无法定位拖动轨道")
    start_x = thumb_box["x"] + thumb_box["width"] / 2
    start_y = thumb_box["y"] + thumb_box["height"] / 2
    target_x = track_box["x"] + track_box["width"] * (
        (target_raw - raw_min) / (raw_max - raw_min)
    )
    await page.mouse.move(start_x, start_y)
    await page.mouse.down()
    await page.mouse.move(target_x, start_y, steps=8)
    await page.mouse.up()
    await page.wait_for_timeout(100)

    now = await control.get_attribute("aria-valuenow")
    _LOGGER.debug(
        "event=aria_slider_set_path path=mouse requested_seconds=%s "
        "target_raw=%s observed_raw=%r thumb_box=%r track_box=%r",
        duration,
        target_raw,
        now,
        thumb_box,
        track_box,
    )
    await _slider_probe("after_mouse")
    try:
        actual = int(float(now)) if now is not None else None
    except (TypeError, ValueError):
        actual = None
    if actual != target_raw:
        raise RuntimeError(
            "视频时长滑块设置失败: "
            f"expected={duration}s expected_raw={target_raw} actual_raw={now!r}"
        )


async def _wait_for_video_options_closed(
    page: Page,
    *,
    timeout_ms: int = _VIDEO_OPTIONS_CLOSE_WAIT_MS,
) -> list[str]:
    step_ms = 50
    elapsed_ms = 0
    closed_stable_ms = 0
    visible: list[str] = []
    while True:
        visible = await _visible_video_ratio_options(page)
        if len(visible) < _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS:
            if closed_stable_ms >= _VIDEO_OPTIONS_CLOSED_STABLE_MS:
                return visible
        else:
            closed_stable_ms = 0
        if elapsed_ms >= timeout_ms:
            return visible
        wait_ms = min(step_ms, timeout_ms - elapsed_ms)
        await page.wait_for_timeout(wait_ms)
        elapsed_ms += wait_ms
        if len(visible) < _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS:
            closed_stable_ms += wait_ms


async def _wait_for_video_options_readback(
    page: Page,
    *,
    ratio: str,
    duration: int,
    trigger=None,
    trigger_kind: str = "A",
) -> str:
    expected = re.compile(
        rf"{re.escape(ratio)}\s*·\s*{duration}s"
    )
    ratio_expected = re.compile(rf"{re.escape(ratio)}\s*·")
    duration_pattern = re.compile(r"\b([4-9]|1[0-5])s\b")

    if trigger_kind == "B":
        # 三点入口本身永远不会变成「1:1 · 5s」。重新打开菜单，若灰度
        # UI 提供了组合摘要项，就对摘要做同样的严格 readback；直接展示
        # chips、没有摘要项的版本则依赖前面已完成的 ratio click 和 slider
        # value/aria-valuenow 校验，避免把恒定的「⋯」误判为失败。
        if trigger is None:
            raise RuntimeError("三点视频参数入口丢失，无法完成设置后校验")
        actual = ""
        summary_seen = False
        try:
            await trigger.click()
        except Exception:
            trigger = await _find_video_options_trigger(
                page,
                prefer_more=True,
            )
            if trigger is None:
                raise RuntimeError("三点视频参数入口丢失，无法完成设置后校验")
            await trigger.click()
        try:
            visible_options = await _wait_for_video_ratio_options(page)
            for readback_attempt in range(2):
                step_ms = 50
                attempts = max(
                    1,
                    (
                        _VIDEO_OPTIONS_READBACK_WAIT_MS + step_ms - 1
                    ) // step_ms,
                )
                for attempt in range(attempts):
                    summary = await _find_video_options_summary_trigger(page)
                    if summary is not None:
                        summary_seen = True
                        actual = await summary.inner_text()
                        if expected.search(actual):
                            return actual
                    if attempt + 1 < attempts:
                        await page.wait_for_timeout(step_ms)
                if readback_attempt == 0:
                    if not summary_seen:
                        visible_options = await _wait_for_video_ratio_options(
                            page,
                            timeout_ms=_VIDEO_OPTIONS_MENU_WAIT_MS,
                        )
                        if (
                            len(visible_options)
                            >= _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS
                        ):
                            actual_duration: int | None = None
                            duration_step_ms = 50
                            duration_attempts = max(
                                1,
                                (
                                    _VIDEO_OPTIONS_READBACK_WAIT_MS
                                    + duration_step_ms
                                    - 1
                                ) // duration_step_ms,
                            )
                            for duration_attempt in range(duration_attempts):
                                control_kind, control = (
                                    await _find_video_duration_control(page)
                                )
                                raw_duration = None
                                if control is not None:
                                    if control_kind == "range":
                                        raw_duration = await control.input_value()
                                    else:
                                        raw_duration = await control.get_attribute(
                                            "aria-valuenow"
                                        )
                                try:
                                    actual_raw = int(float(raw_duration))
                                except (TypeError, ValueError):
                                    actual_raw = None
                                if control_kind == "aria" and control is not None:
                                    try:
                                        raw_min = int(float(
                                            await control.get_attribute("aria-valuemin")
                                            or _VIDEO_DURATION_MIN_SECONDS
                                        ))
                                        raw_max = int(float(
                                            await control.get_attribute("aria-valuemax")
                                            or _VIDEO_DURATION_MAX_SECONDS
                                        ))
                                    except (TypeError, ValueError):
                                        raw_min = raw_max = None
                                    if (
                                        actual_raw is not None
                                        and raw_min is not None
                                        and raw_max is not None
                                        and (raw_min, raw_max)
                                        in {
                                            (
                                                _VIDEO_DURATION_MIN_SECONDS,
                                                _VIDEO_DURATION_MAX_SECONDS,
                                            ),
                                            (
                                                0,
                                                _VIDEO_DURATION_MAX_SECONDS
                                                - _VIDEO_DURATION_MIN_SECONDS,
                                            ),
                                        }
                                    ):
                                        actual_duration = (
                                            _VIDEO_DURATION_MIN_SECONDS
                                            + actual_raw
                                            - raw_min
                                        )
                                    else:
                                        actual_duration = None
                                else:
                                    actual_duration = actual_raw
                                if actual_duration == duration:
                                    break
                                if duration_attempt + 1 < duration_attempts:
                                    await page.wait_for_timeout(duration_step_ms)
                            if actual_duration != duration:
                                _LOGGER.warning(
                                    "event=video_options_readback_failed "
                                    "kind=B url=%s expected=%r "
                                    "actual_duration=%r",
                                    page.url,
                                    f"{ratio} · {duration}s",
                                    actual_duration,
                                )
                                raise RuntimeError(
                                    "视频参数设置后校验失败: "
                                    f"expected_duration={duration}s "
                                    f"actual_duration={actual_duration!r}s"
                                )
                            _LOGGER.info(
                                "event=video_options_readback_by_controls "
                                "kind=B url=%s expected=%r "
                                "actual_duration=%ss",
                                page.url,
                                f"{ratio} · {duration}s",
                                actual_duration,
                            )
                            return actual
                        raise RuntimeError(
                            "三点视频参数菜单重开失败，无法完成设置后校验: "
                            f"url={page.url}"
                        )
                    await page.wait_for_timeout(500)

            _LOGGER.warning(
                "event=video_options_readback_failed kind=B url=%s "
                "expected=%r actual=%r trigger_pattern=%r",
                page.url,
                f"{ratio} · {duration}s",
                actual,
                _VIDEO_OPTIONS_TRIGGER_RE.pattern,
            )
            raise RuntimeError(
                "视频参数设置后校验失败: "
                f"expected={ratio} · {duration}s actual={actual!r}"
            )
        finally:
            await _close_video_options(
                page,
                trigger,
                trigger_kind="B",
            )

    actual = ""
    for readback_attempt in range(3):
        step_ms = 50
        attempts = max(
            1,
            (_VIDEO_OPTIONS_READBACK_WAIT_MS + step_ms - 1) // step_ms,
        )
        for attempt in range(attempts):
            trigger = await _find_video_options_trigger(page)
            if trigger is not None:
                actual = await trigger.inner_text()
                if ratio_expected.search(actual):
                    duration_match = duration_pattern.search(actual)
                    if duration_match is not None:
                        actual_duration = int(duration_match.group(1))
                        if actual_duration != duration:
                            _LOGGER.warning(
                                "event=video_options_readback_failed "
                                "url=%s expected=%r actual=%r "
                                "actual_duration=%s",
                                page.url,
                                f"{ratio} · {duration}s",
                                actual,
                                actual_duration,
                            )
                            raise RuntimeError(
                                "视频参数设置后校验失败: "
                                f"expected={ratio} · {duration}s "
                                f"actual={actual!r}"
                            )
                        return actual
            if attempt + 1 < attempts:
                await page.wait_for_timeout(step_ms)
        if readback_attempt < 2:
            _LOGGER.warning(
                "event=video_options_readback_retry url=%s expected=%r actual=%r "
                "retry_after_ms=1000",
                page.url,
                f"{ratio} · {duration}s",
                actual,
            )
            await page.wait_for_timeout(1000)
    _LOGGER.warning(
        "event=video_options_readback_failed url=%s expected=%r actual=%r "
        "trigger_pattern=%r",
        page.url,
        f"{ratio} · {duration}s",
        actual,
        _VIDEO_OPTIONS_TRIGGER_RE.pattern,
    )
    raise RuntimeError(
        f"视频参数设置后校验失败: expected={ratio} · {duration}s actual={actual!r}"
    )


async def _close_video_options(
    page: Page,
    trigger,
    *,
    trigger_kind: str | None = None,
) -> None:
    trigger_kind = trigger_kind or await _video_options_trigger_kind(trigger)
    # Keep the normal A path observation-free here: inner_text() is also used
    # by readback and some page implementations expose it through a live
    # React render.  B's icon trigger has no useful text, but reading it is
    # useful for the requested diagnostic signature.
    trigger_text = ""
    if trigger_kind == "B":
        try:
            trigger_text = await trigger.inner_text()
        except Exception:
            trigger_text = ""
    else:
        try:
            trigger_text = (
                await trigger.get_attribute("aria-label")
                or await trigger.get_attribute("title")
                or ""
            )
        except Exception:
            trigger_text = ""
    if trigger_kind == "B":
        # 两级菜单下 Escape 可能只关闭比例内层而保留三点外层。B 型入口
        # 的根三点是确定的 toggle，优先点原 locator 一次关闭整个外层。
        _LOGGER.debug(
            "event=video_options_close_attempt kind=%s trigger_text=%r "
            "reason=%s",
            trigger_kind,
            trigger_text,
            "normal",
        )
        toggle_error: Exception | None = None
        try:
            await trigger.click()
        except Exception as first_error:
            try:
                _LOGGER.debug(
                    "event=video_options_close_attempt kind=%s "
                    "trigger_text=%r reason=%s",
                    trigger_kind,
                    trigger_text,
                    "fallback_reclick",
                )
                trigger = (
                    await _find_video_options_trigger(page, prefer_more=True)
                    or trigger
                )
                await trigger.click()
            except Exception as second_error:
                toggle_error = second_error
                _LOGGER.debug(
                    "video options B toggle failed twice: first=%r second=%r",
                    first_error,
                    second_error,
                )
        if toggle_error is None:
            await page.wait_for_timeout(100)
            visible_after_toggle = await _wait_for_video_options_closed(page)
            if len(visible_after_toggle) < _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS:
                return
        else:
            visible_after_toggle = await _visible_video_ratio_options(page)

        _LOGGER.debug(
            "event=video_options_close_attempt kind=%s trigger_text=%r "
            "reason=%s",
            trigger_kind,
            trigger_text,
            "fallback_escape",
        )
        await page.keyboard.press("Escape")
        visible_after_escape = await _wait_for_video_options_closed(page)
        if len(visible_after_escape) < _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS:
            return

        _LOGGER.warning(
            "event=video_options_close_failed url=%s trigger_text=%r "
            "trigger_kind=B toggle_error=%r visible_after_toggle=%s "
            "visible_after_escape=%s",
            page.url,
            trigger_text,
            toggle_error,
            visible_after_toggle,
            visible_after_escape,
        )
        raise RuntimeError(
            "视频参数菜单关闭失败: "
            f"trigger_text={trigger_text!r} visible={visible_after_escape}"
        )

    _LOGGER.debug(
        "event=video_options_close_attempt kind=%s trigger_text=%r reason=%s",
        trigger_kind,
        trigger_text,
        "normal",
    )
    await page.keyboard.press("Escape")
    visible_after_escape = await _wait_for_video_options_closed(page)
    if len(visible_after_escape) < _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS:
        return

    # 部分页面版本不处理 Escape,退回再次点击组合按钮关闭 toggle。
    trigger = await _find_video_options_trigger(page) or trigger
    try:
        trigger_text = await trigger.inner_text()
    except Exception:
        trigger_text = ""
    _LOGGER.debug(
        "event=video_options_close_attempt kind=%s trigger_text=%r "
        "reason=%s",
        trigger_kind,
        trigger_text,
        "fallback_reclick",
    )
    await trigger.click()
    visible_after_toggle = await _wait_for_video_options_closed(page)
    if len(visible_after_toggle) < _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS:
        return

    _LOGGER.warning(
        "event=video_options_close_failed url=%s trigger_text=%r "
        "trigger_pattern=%r visible_after_escape=%s visible_after_toggle=%s",
        page.url,
        trigger_text,
        _VIDEO_OPTIONS_TRIGGER_RE.pattern,
        visible_after_escape,
        visible_after_toggle,
    )
    raise RuntimeError(
        "视频参数菜单关闭失败: "
        f"trigger_text={trigger_text!r} visible={visible_after_toggle}"
    )


async def _apply_video_options(
    page: Page,
    *,
    ratio: str,
    duration: int,
) -> None:
    """点击视频参数组合按钮,在弹层中选择比例和整数秒时长。"""
    if ratio not in _VIDEO_RATIO_OPTIONS:
        raise RuntimeError(
            f"视频比例选项不存在: {ratio}; 可选项={list(_VIDEO_RATIO_OPTIONS)}"
        )
    if not _VIDEO_DURATION_MIN_SECONDS <= duration <= _VIDEO_DURATION_MAX_SECONDS:
        raise ValueError(
            "视频时长必须在 "
            f"{_VIDEO_DURATION_MIN_SECONDS} 到 "
            f"{_VIDEO_DURATION_MAX_SECONDS} 秒之间: {duration}"
        )

    trigger, visible_options, trigger_kind = await _open_video_options(page)
    root_trigger = trigger
    root_trigger_kind = trigger_kind
    if ratio not in visible_options:
        raise RuntimeError(
            f"视频比例选项不存在: {ratio}; 实际可见={visible_options}"
        )

    ratio_button = await _first_visible_locator(
        _exact_video_option_button(page, ratio)
    )
    if ratio_button is None:
        raise RuntimeError(
            f"视频比例选项不可点击: {ratio}; 实际可见={visible_options}"
        )
    await ratio_button.click()
    await page.wait_for_timeout(100)
    if _LOGGER.isEnabledFor(logging.DEBUG):
        duration_visible, ratio_visible = (
            await _video_options_visibility_snapshot(page)
        )
        try:
            root_signature = await _video_options_locator_signature_without_text(
                root_trigger
            )
        except Exception:
            root_signature = None
        _LOGGER.debug(
            "event=video_options_after_ratio_click ratio=%s "
            "duration_visible=%s ratio_visible=%s root_signature=%r",
            ratio,
            duration_visible,
            ratio_visible,
            root_signature,
        )

    kind, duration_control = await _wait_for_video_duration_control(page)
    if duration_control is None:
        # 比例按钮若会自动收起弹层,重新点开一次再找 slider。
        if not await _visible_video_ratio_options(page):
            trigger, _, trigger_kind = await _open_video_options(page)
            if root_trigger_kind != "B":
                root_trigger = trigger
                root_trigger_kind = trigger_kind
            kind, duration_control = await _wait_for_video_duration_control(page)
    if duration_control is None:
        raise RuntimeError(f"视频时长滑块未找到: {page.url}")

    if kind == "range":
        await _set_native_range_value(duration_control, duration)
    else:
        await _set_aria_slider_value(page, duration_control, duration)

    await _close_video_options(
        page,
        root_trigger,
        trigger_kind=root_trigger_kind,
    )
    await _wait_for_video_options_readback(
        page,
        ratio=ratio,
        duration=duration,
        trigger=root_trigger,
        trigger_kind=root_trigger_kind,
    )


def _exact_ui_text(value: str) -> re.Pattern[str]:
    return re.compile(rf"^\s*{re.escape(value)}\s*$")


async def _first_visible_exact_text(page: Page, selector: str, text: str):
    locator = page.locator(selector).filter(has_text=_exact_ui_text(text))
    return await _first_visible_locator(locator)


async def _wait_for_visible_exact_text(
    page: Page,
    selectors: tuple[str, ...],
    text: str,
    *,
    timeout_ms: int = _VIDEO_MODE_READY_WAIT_MS,
    require_enabled: bool = False,
):
    """等待 React 挂载 exact-text 控件，并按 selector 优先级返回首个命中。"""
    deadline = time.monotonic() + (timeout_ms / 1000)
    while time.monotonic() < deadline:
        for selector in selectors:
            candidate = await _first_visible_exact_text(page, selector, text)
            if candidate is None:
                continue
            if require_enabled:
                try:
                    if not await candidate.is_enabled():
                        continue
                except Exception:
                    continue
            return candidate
        await asyncio.sleep(0.1)
    return None


async def _wait_for_video_generation_mode_ready(page: Page):
    """等待视频模式的 placeholder、chip、model 三个标志同时挂载。"""
    deadline = time.monotonic() + (_VIDEO_MODE_READY_WAIT_MS / 1000)
    while time.monotonic() < deadline:
        placeholder = await _first_visible_locator(
            page.locator(_VIDEO_MODE_PLACEHOLDER_SEL)
        )
        chip = await _first_visible_locator(page.locator(_VIDEO_MODE_CHIP_SEL))
        model_button = await _first_visible_locator(
            page.locator(_VIDEO_MODEL_BUTTON_SEL)
        )
        if placeholder is not None and chip is not None and model_button is not None:
            try:
                if (await model_button.inner_text()).strip():
                    return model_button
            except Exception:
                pass
        await asyncio.sleep(0.1)

    visible_buttons = await _visible_button_texts(page)
    raise RuntimeError(
        "视频生成模式未完整挂载:需要 placeholder/chip/model 三项同时可见; "
        f"url={page.url}; visible_buttons={visible_buttons}"
    )


async def _click_video_mode_chip(page: Page) -> None:
    """Click the Layout B actionbar video chip without opening legacy parameter UI.

    The ``data-value=17`` node is sometimes a non-interactive presentation
    element inside the actual button. Prefer its nearest semantic clickable
    ancestor, then fall back to the chip itself for accounts whose actionbar
    uses a clickable ``div`` without an explicit role.
    """
    chip = None
    for selector in _VIDEO_MODE_CHIP_CANDIDATE_SELS:
        chip = await _first_visible_locator(page.locator(selector))
        if chip is not None:
            break
    if chip is None:
        raise RuntimeError("该账号未开通视频生成入口")

    candidates = []
    try:
        clickable_ancestor = chip.locator(
            "xpath=ancestor-or-self::*[self::button or @role='button' "
            "or @tabindex='0'][1]"
        )
        candidates.append(clickable_ancestor)
    except Exception:
        pass
    try:
        # Some actionbar builds attach the handler to an unannotated wrapper;
        # include the direct parent before falling back to the presentation
        # node itself (whose click still bubbles on the usual builds).
        candidates.append(chip.locator("xpath=parent::*[1]"))
    except Exception:
        pass
    candidates.append(chip)

    for candidate_locator in candidates:
        candidate = await _first_visible_locator(candidate_locator)
        if candidate is None:
            continue
        try:
            await candidate.click(timeout=3_000)
        except TypeError:
            # Minimal test doubles and older Playwright shims do not accept
            # the timeout keyword; the normal locator click remains identical.
            try:
                await candidate.click()
            except Exception:
                continue
        except Exception as exc:
            _LOGGER.debug(
                "event=video_generation_layout_b_chip_candidate_failed "
                "url=%s error=%s",
                page.url,
                exc.__class__.__name__,
            )
            continue
        _LOGGER.info(
            "event=video_generation_layout_b_chip_click url=%s",
            page.url,
        )
        return

    raise RuntimeError("该账号未开通视频生成入口")


async def _wait_for_legacy_video_generation_ready(page: Page):
    """旧 create-image 页面没有 actionbar chip，以 TAB、编辑器和模型作校验。"""
    deadline = time.monotonic() + (_VIDEO_MODE_READY_WAIT_MS / 1000)
    while time.monotonic() < deadline:
        editor = await _first_visible_locator(page.locator(EDITOR_SEL))
        model_button = await _first_visible_locator(
            page.locator(_VIDEO_MODEL_BUTTON_SEL)
        )
        if editor is not None and model_button is not None:
            try:
                model_text = (await model_button.inner_text()).strip()
                if model_text:
                    _LOGGER.info(
                        "event=video_generation_legacy_mode_ready url=%s model=%r",
                        page.url,
                        model_text,
                    )
                    return model_button
            except Exception:
                pass
        await asyncio.sleep(0.1)

    visible_buttons = await _visible_button_texts(page)
    raise RuntimeError(
        "旧视频生成模式未完整挂载:需要已校验的视频 TAB、编辑器和模型控件; "
        f"url={page.url}; visible_buttons={visible_buttons}"
    )


async def _enter_video_generation_mode(page: Page):
    """Enter video mode through the skill menu or actionbar chip.

    Some accounts do not expose the ``视频生成`` skill in the toolbar menu;
    those accounts expose the same mode as the bottom actionbar chip instead.
    Never navigate to ``create-image`` here: that legacy page opens the
    parameter UI which is intentionally retired from the submission path.
    """
    new_conversation = await _wait_for_visible_exact_text(
        page, (_NEW_CONVERSATION_SEL,), "新对话"
    )
    if new_conversation is None:
        raise RuntimeError("左侧「新对话」入口未找到")
    await new_conversation.click()

    more = await _wait_for_visible_exact_text(
        page,
        (_VIDEO_TOOLBAR_MORE_SEL,),
        "更多",
        require_enabled=True,
    )
    if more is None:
        raise RuntimeError("输入框工具条「更多」按钮未找到或不可用")

    video_generation = None
    skill_selectors = (
        _VIDEO_GENERATION_SKILL_SEL,
        _VIDEO_GENERATION_ACTIONBAR_ITEM_SEL,
        _VIDEO_GENERATION_SKILL_FALLBACK_SEL,
    )
    for menu_attempt in range(2):
        await more.click()
        video_generation = await _wait_for_visible_exact_text(
            page,
            skill_selectors,
            "视频生成",
            require_enabled=True,
        )
        if video_generation is not None:
            break
        if menu_attempt == 0:
            _LOGGER.warning(
                "event=video_generation_menu_reopen url=%s attempt=1",
                page.url,
            )
            await page.keyboard.press("Escape")

    if video_generation is None:
        try:
            skill_texts = await page.locator(
                _VIDEO_GENERATION_SKILL_FALLBACK_SEL
            ).all_text_contents()
        except Exception:
            skill_texts = []
        _LOGGER.warning(
            "event=video_generation_menu_missing url=%s skill_texts=%r",
            page.url,
            skill_texts,
        )
        await page.keyboard.press("Escape")
        await _click_video_mode_chip(page)
        return await _wait_for_video_generation_mode_ready(page)
    await video_generation.click()
    return await _wait_for_video_generation_mode_ready(page)


def _first_text_line(value: str) -> str:
    return next((line.strip() for line in value.splitlines() if line.strip()), "")


async def _find_exact_model_item(page: Page, label: str, *, selected: bool | None = None):
    items = await _visible_locators(page.locator(_VIDEO_MODEL_MENU_ITEM_SEL))
    for item in items:
        try:
            if _first_text_line(await item.inner_text()) != label:
                continue
            if selected is not None:
                is_selected = (await item.get_attribute("data-selected")) == "true"
                if is_selected != selected:
                    continue
            return item
        except Exception:
            continue
    return None


async def _select_video_model(page: Page, model_button, model: str) -> None:
    """显式选择 task 级模型，并做菜单状态和按钮文本双回读。"""
    label = VIDEO_MODEL_LABELS.get(model)
    if label is None:
        raise RuntimeError(f"不支持的视频模型: {model}")

    await model_button.click()
    await page.wait_for_timeout(150)
    item = await _find_exact_model_item(page, label)
    if item is None:
        raise RuntimeError(f"视频模型选项未找到: {label}")
    await item.click()
    await page.wait_for_timeout(200)

    # Radix 菜单通常在选择后关闭；若仍开着就直接回读，否则重新打开。
    if not await _visible_locators(page.locator(_VIDEO_MODEL_MENU_ITEM_SEL)):
        await model_button.click()
        await page.wait_for_timeout(150)
    selected_item = await _find_exact_model_item(page, label, selected=True)
    if selected_item is None:
        raise RuntimeError(f"视频模型选中状态回读失败: {label}")
    await page.keyboard.press("Escape")
    await page.wait_for_timeout(100)

    button_text = " ".join((await model_button.inner_text()).split())
    if not button_text.endswith(label):
        raise RuntimeError(
            f"视频模型按钮回读不一致: expected={label!r}, actual={button_text!r}"
        )


def _build_video_prompt(prompt: str, *, ratio: str, duration: int) -> str:
    base = prompt.rstrip()
    separator = "" if base.endswith(("。", "！", "？", ".", "!", "?")) else "。"
    return f"{base}{separator}时长{duration}秒，比例{ratio}"


async def submit_via_ui(
    page: Page,
    prompt: str,
    *,
    model: str,
    ratio: str,
    duration: int,
    profile_dir: Path,
    update: Callable[..., None],
) -> None:
    """真实 UI 提交:新对话进入视频生成模式，粘贴参数化 prompt 后发送。

    整段在浏览器内执行,绕过 shark_admin 服务端对 page.evaluate POST 的识别。
    外层 run gate 之外仍保留两次 aegis 兜底；任一命中即中止。
    """
    # 每次提交（含“确认请求未发出”的一次 ACK 重试）都从干净的新会话开始。
    model_button = await _enter_video_generation_mode(page)
    await _select_video_model(page, model_button, model)

    # 模式和 task 级模型确认后做第一轮只读探测。
    await _pre_submit_aegis_gate(page, profile_dir, update)

    # 清空输入框 —— 内容审核改写和 ACK 未发出重试路径均幂等。
    await clear_prose_mirror(page)

    # 输入 prompt —— 仍使用 clipboard paste，不退回 keyboard.type。
    # 原因:
    # - 用户把整段提示词一次给我,需要"一次性贴入",不是一字一字打
    # - keyboard.type(prompt, delay=20) 对 500 字 prompt 要 ~10s 跑完,
    #   加上均匀 delay 是 aegis 时序风控最爱的特征(真人 paste 间隔是 0ms 突刺)
    # - 用 navigator.clipboard.writeText + Ctrl+V = 真人从剪贴板贴长文,
    #   ProseMirror 也正确收到 paste event 同步 internal model state
    loc = page.locator(EDITOR_SEL).first
    await loc.wait_for(state="visible", timeout=10_000)
    await loc.click()
    # v0.3.5.2 DEBUG:把实际写到 clipboard 的 prompt 前 80 字符打到日志,
    # 排查"DB 写对但豆包收到错"的 prompt 错位 bug —— 用户报 4 段提示词
    # 一组提交,豆包 conversation 页只收到 3 条且 30-37 那段缺失,先确认
    # 究竟是 Python 侧 paste 就贴错了,还是 React app 抢 state 把对的
    # prompt 替成错的。复现一次后即可定位,定位完撤掉这行。
    submitted_prompt = _build_video_prompt(
        prompt,
        ratio=ratio,
        duration=duration,
    )
    _LOGGER.info(
        "[v0.3.5.2 DEBUG submit_via_ui] page.url=%s prompt[:80]=%r",
        page.url,
        submitted_prompt[:80],
    )
    # writeText 走 page.evaluate —— 必须 launch_persistent_context 已 grant
    # clipboard-read-write 权限(见 _build_launch_kwargs)
    await page.evaluate(
        "(text) => navigator.clipboard.writeText(text)",
        submitted_prompt,
    )
    # 给 clipboard 写入一点点时间(MS Edge 偶发 readback 竞速)
    await page.wait_for_timeout(50)
    await page.keyboard.press("Control+V")
    await page.wait_for_timeout(150)  # 等 ProseMirror 同步 internal state

    # 粘贴后、点击发送前再探一次，覆盖弹窗延迟挂载窗口。
    await _pre_submit_aegis_gate(page, profile_dir, update)
    await try_click(
        page,
        (SEND_BTN_SEL, SEND_BTN_FALLBACK_SEL, _SEND_BTN_LEGACY_SVG_SEL),
        timeout=5.0,
    )
    await page.wait_for_timeout(500)  # 等 POST 真的飞出去


class PlaywrightVideoRunner:
    """v0.2.19:async Playwright + per-profile_dir BrowserContext 共享。

    同一账号(profile_dir)的多个 task 复用同一个 BrowserContext,
    每个 task 自己 new_page → page.close()。BrowserContext 生命周期跟
    runner 实例走,close() 时统一关。task 之间不再 per-account asyncio.Lock
    串行化(同账号 5 个 mini 10s 视频可以并发跑,共享 50 点 quota)。

    pc_version 在第一次 context 启动时读,缓存到 _tokens。后续 task 复用
    缓存的 TokenBundle(避免每次重新抽 leveldb / 触 WebMSSDK)。
    """

    def __init__(self, timeout: float = 420, poll_interval: float = 10):
        self.timeout = timeout
        self.poll_interval = poll_interval
        self._pw = None  # async_playwright() instance
        self._pw_lock: asyncio.Lock | None = None  # lazy
        self._contexts: dict[str, BrowserContext] = {}
        self._tokens: dict[str, TokenBundle] = {}
        self._init_locks: dict[str, asyncio.Lock] = {}
        self._submit_ack_locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, profile_dir: Path) -> asyncio.Lock:
        """v0.2.19:per-profile 异步锁 —— lazy 创建,保证同一 profile
        的第一个 context 创建是串行的(避免并发 launch_persistent_context
        同 Lockfile 互撞)。后续 task 直接拿到已存在的 context。
        """
        key = str(profile_dir)
        if key not in self._init_locks:
            self._init_locks[key] = asyncio.Lock()
        return self._init_locks[key]

    def _submit_ack_lock_for(self, profile_dir: Path) -> asyncio.Lock:
        """同一 profile 仅串行 UI submit 到 ACK,后续 poll 继续并发。"""
        key = str(profile_dir)
        if key not in self._submit_ack_locks:
            self._submit_ack_locks[key] = asyncio.Lock()
        return self._submit_ack_locks[key]

    async def _ensure_playwright(self):
        if self._pw is None:
            self._pw = await async_playwright().start()
        return self._pw

    async def _get_shared_context(
        self,
        profile_dir: Path,
        pc_version: str | None,
        *,
        window_visible: bool = False,
    ):
        """拿到共享的 BrowserContext + 缓存的 TokenBundle。

        第一次:launch_persistent_context + load_browser_context + 缓存
        后续:直接返回缓存

        v0.2.20:anchor page 模式 —— 不关 init page,把它当 anchor 缓存。
        Playwright 的 BrowserContext 在所有 page 关闭后会自动 close context,
        一旦 context 进入 "0 page" 状态,后续 task 的 new_page() 就会
        TargetClosedError。保留 anchor page 同时确保 context 一直活着,
        也给后续 task 一个可复用的 doubao.com origin 页面,避免 about:blank
        上 history.replaceState 抛 SecurityError。

        v0.2.22:`window_visible` —— 决定 Chromium 窗口是否显示到桌面。
        仅在 context 首次创建时生效;cached context 复用前次位置。
        """
        key = str(profile_dir)
        async with self._lock_for(profile_dir):
            existing = self._contexts.get(key)
            if existing is not None:
                # context 可能被 Playwright 自动 close 了(context 全 page 关掉)
                # 或被外部 close 了(用户手关窗口 / 进程重启)。先探一下。
                try:
                    is_closed = existing.is_closed()
                except Exception:
                    is_closed = True
                if not is_closed:
                    return existing, self._tokens[key]
                # 已 close → 清掉缓存走完整重建流程
                self._contexts.pop(key, None)
                self._tokens.pop(key, None)

            pw = await self._ensure_playwright()
            context = await pw.chromium.launch_persistent_context(
                str(profile_dir),
                **_build_launch_kwargs(window_visible=window_visible),
            )
            # 必须先打开 doubao.com 拿到 aegis 风控指纹,否则
            # COMPLETION_SCRIPT 会被字节拒为 1011(用户未登录)。
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(
                "https://www.doubao.com/chat/",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await page.wait_for_timeout(2_000)
            # 抽完整 TokenBundle 缓存起来
            bundle = await load_browser_context(page, context, pc_version=pc_version)
            # v0.2.20:不关 init page —— 保留作 anchor。context 全 page 关闭
            # 会触发 Playwright 自动 close context,后续 task 就废了。
            self._contexts[key] = context
            self._tokens[key] = bundle
            return context, bundle

    async def _release_profile_context(self, profile_dir: Path) -> None:
        """关闭并清除共享 profile context，使人工登录窗口可以重新打开。"""
        key = str(profile_dir)
        async with self._lock_for(profile_dir):
            context = self._contexts.pop(key, None)
            self._tokens.pop(key, None)
            if context is None:
                return
            try:
                # Persistent contexts can retain the profile SingletonLock while
                # an anchor/task page is still alive. Close every page first so
                # the user's account-management browser can reopen this profile.
                for page in list(context.pages):
                    try:
                        if not page.is_closed():
                            await page.close()
                    except Exception:
                        pass
                await context.close()
            except Exception:
                # 释放是 fail-fast 的清理动作；底层 context 已经失效时不应
                # 覆盖 AegisBlocked，也不能阻止缓存清理。
                pass

    async def close(self) -> None:
        """v0.2.19:关闭所有共享 BrowserContext + Playwright 实例。
        service.shutdown() 调用一次即可。

        v0.2.20:context 关闭前先关掉所有 anchor pages,Playwright 才会
        真正释放 context。否则 context 会因 "还有 page 存活" 保持开启,
        Chromium 进程不退出,占用 profile_dir 的 Lockfile 直到下次 GC。
        """
        for key, context in list(self._contexts.items()):
            try:
                # 先关 page,再关 context;不要被个别失败阻塞其余账号
                for page in list(context.pages):
                    try:
                        if not page.is_closed():
                            await page.close()
                    except Exception:
                        pass
                await context.close()
            except Exception:
                pass
        self._contexts.clear()
        self._tokens.clear()
        self._init_locks.clear()
        if self._pw is not None:
            try:
                await self._pw.stop()
            except Exception:
                pass
            self._pw = None
        self._pw_lock = None

    async def recheck_result(
        self,
        profile_dir: Path,
        conversation_id: str,
        update: Callable[..., None],
        cancel_event: threading.Event,
        *,
        deadline_seconds: float = 90,
        expected_local_message_ids: set[str] | None = None,
        expected_remote_task_ids: set[str] | None = None,
    ) -> dict[str, str] | None:
        """v0.2.9:不重提交,只重解析 —— 复用已存的 conversation_id,
        重新打开 /chat/<id> 拉一次 CHAIN_SCRIPT,parse 出最新 result。

        用于 retry-result 端点:用户报告"succeeded 但 result_url 失效"
        或 "卡在 generating 很久不动了",想再查一次远端而不消耗豆包额度
        (不调 COMPLETION_SCRIPT,只查 chain)。

        v0.2.19:async Playwright,共用 shared context。

        返回 None = 还在生成中 / 远端还没出 result;
        返回 dict = parse_creation_result 出的 result 字段(result_url /
        backup_result_url / fallback_result_url / vid / cover_url 等),
        调用方负责写回 VideoTask。

        v0.3.3:`expected_local_message_ids` —— recheck 不重提交,不知道
        本任务 submit 时用过的 id,通常传 None 走 fall through(原有行为)。
        service.py 调过来时透传 task.db 里存的 id 集合(若以前 v0.3.3+
        写入过的话)。这是 best-effort,不传一样能工作。
        """
        context, _bundle = await self._get_shared_context(profile_dir, pc_version=None)
        # v0.2.26:不复用 anchor page(同 run() 一样的修复 —— 详见 run() 注释)。
        # 必须 new_page() 才不会让 finally page.close() 把 anchor 关掉 → context
        # 0 page → Playwright 自动 close context → 同账号并发任务全炸。
        if not _is_context_alive(context):
            self._contexts.pop(str(profile_dir), None)
            self._tokens.pop(str(profile_dir), None)
            raise RuntimeError("视频浏览器上下文已关闭,请重试")
        try:
            page = await context.new_page()
        except Exception as exc:
            self._contexts.pop(str(profile_dir), None)
            self._tokens.pop(str(profile_dir), None)
            raise RuntimeError(
                f"视频浏览器窗口已关闭,请重新打开后重试:{exc}"
            ) from exc
        try:
            # 必须先打开 doubao.com 拿到 aegis 风控指纹,否则 CHAIN_SCRIPT
            # 会被字节拒为 1011(用户未登录)——和首次提交一样的硬约束。
            await page.goto(
                "https://www.doubao.com/chat/",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            await page.wait_for_timeout(2_000)
            # 切到指定 conversation(只读,不发新请求)
            await page.evaluate(
                "id => history.replaceState({}, '', '/chat/' + id)",
                conversation_id,
            )
            update(status="rechecking")
            deadline = time.monotonic() + deadline_seconds
            poll_interval_s = max(1, int(self.poll_interval))  # v0.3.4:同主 poll
            while time.monotonic() < deadline:
                if cancel_event.is_set():
                    raise RuntimeError("任务已取消")
                # recheck 同样执行只读探测，命中即停止并释放 profile。
                await _handle_aegis_in_poll(page, profile_dir, update)
                chain = await page.evaluate(
                    CHAIN_SCRIPT, {"conversationId": conversation_id}
                )
                if chain["status"] != 200:
                    raise RuntimeError(
                        f"豆包结果接口返回 HTTP {chain['status']}"
                    )
                # v0.3.3 race 防御:recheck 不重提交,调用方通常传
                # expected_local_message_ids=None 走 fall through(原行为);
                # 若 task.db 之前 v0.3.3+ 写入过 id 集合就透传,做 best-effort
                # 串话过滤。
                # v0.3.5:同样透传 expected_remote_task_ids(若调用方有)。
                result = parse_creation_result(
                    chain["data"],
                    expected_local_message_ids=expected_local_message_ids,
                    expected_remote_task_ids=expected_remote_task_ids,
                )
                if result:
                    update(status="resolving", **result)
                    return await self._resolve_original_download(page, result, cancel_event)
                # v0.3.4:wait 拆 1s 段 + 每段先 probe(同主 poll)
                for _ in range(poll_interval_s):
                    if time.monotonic() >= deadline:
                        break
                    try:
                        if await aegis_popup_present(page):
                            break
                    except Exception:
                        break
                    await asyncio.sleep(1)
            return None
        except AegisBlocked:
            await self._release_profile_context(profile_dir)
            raise
        finally:
            with contextlib.suppress(Exception):
                await page.close()

    async def run(
        self,
        profile_dir: Path,
        prompt: str,
        model: str,
        ratio: str,
        duration: int,
        update: Callable[..., None],
        cancel_event: threading.Event,
        *,
        mode: str = "t2v",
        image_paths: list[str] | None = None,
        pc_version: str | None = None,
        max_reject_retries: int = 0,
        window_visible: bool = False,
        owner_task_id: str | None = None,
        prompt_retry_count: int = 0,
    ) -> dict[str, str]:
        """单账号一次性视频生成。

        v0.2.22 Q1:加 `max_reject_retries`(opt-in,默认 0 沿用 v0.2.21)。
        收到豆包内容审核拒绝时,用 prompt_reviser 改写 prompt 后,在同一
        page 上 history.replaceState + COMPLETION_SCRIPT 重提交。共享
        page / 上传好的图片 / 缓存的 TokenBundle —— retry 不重做这些。

        v0.2.22 Q2:`window_visible` 决定 Chromium 窗口是否显示;只在
        BrowserContext 首次创建时生效,见 _get_shared_context 注释。
        """
        context, token_bundle = await self._get_shared_context(
            profile_dir, pc_version=pc_version, window_visible=window_visible,
        )
        # v0.2.26:每个 task 必须 new_page() —— 不复用 anchor。
        # 旧逻辑「遍历 context.pages 选未关闭的」会让 task 拿到 anchor page
        # (context.pages[0],由 _get_shared_context 保留防止 context 进入
        # "0 page" 状态被 Playwright 自动 close)。task 完成后 finally
        # page.close() 会把 anchor 关掉 → 同账号并发任务全部抛
        # TargetClosedError。改为始终 new_page(),anchor 仍由 _get_shared_context
        # 持有,生命周期跟 context 走(runner.close() 时统一关)。
        if not _is_context_alive(context):
            self._contexts.pop(str(profile_dir), None)
            self._tokens.pop(str(profile_dir), None)
            raise RuntimeError("视频浏览器上下文已关闭,请重试")
        try:
            page = await context.new_page()
        except Exception as exc:
            # context 已 close(用户手关窗 / Playwright 因外部原因 close /
            # 进程重启后缓存指向旧 context 等)。清掉缓存,下次重试会走完整
            # 重建流程;不要把底层 Playwright 异常原文直接透到 UI。
            self._contexts.pop(str(profile_dir), None)
            self._tokens.pop(str(profile_dir), None)
            raise RuntimeError(
                f"视频浏览器窗口已关闭,请重新打开后重试:{exc}"
            ) from exc
        try:
            # v0.2.20:保险 —— 如果 anchor 是 about:blank(用户从 login profile
            # 拉过一次然后关窗,我们用另一个 instance 重启了 context 的场景),
            # 显式重定向到 doubao.com 让 history.replaceState 有合法 origin。
            # recheck_result 早就这么做了,run() 漏了,v0.2.19 直接撞 SecurityError。
            current_url = page.url
            if current_url == "about:blank" or not current_url.startswith("https://www.doubao.com/"):
                await page.goto(
                    "https://www.doubao.com/chat/",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                await page.wait_for_timeout(1_500)

            # 上传或提交前先完成一次完整探测窗口；命中即停止该 profile。
            await _pre_submit_aegis_gate(page, profile_dir, update)

            fingerprint = token_bundle.device_id or token_bundle.web_id

            # i2v 图片上传一次性完成,retry 不重复上传(豆包图片上传有独立
            # 签名逻辑,重传会拿到不同 OSS key,反而被风控)。
            # TODO(v0.3.5.6):同 profile 的 image upload 仍在 submit→ACK 锁外,
            # page.evaluate 上传的并发边界留待后续单独处理。
            uploaded_images: list[dict] = []
            if mode == "i2v":
                paths = [Path(item) for item in (image_paths or [])]
                if not paths:
                    raise RuntimeError("图生视频缺少本地图片")
                if len(paths) > 9:
                    raise RuntimeError("图生视频最多支持 9 张图片")
                update(status="starting")
                for index, image_path in enumerate(paths, start=1):
                    if cancel_event.is_set():
                        raise RuntimeError("任务已取消")
                    if not image_path.is_file():
                        raise RuntimeError(f"图片不存在：{image_path}")
                    name, mime, b64 = load_image_base64(image_path)
                    update(status="starting", error_message=f"正在上传图片 {index}/{len(paths)}")
                    uploaded = await page.evaluate(
                        UPLOAD_IMAGE_SCRIPT,
                        {"name": name, "mime": mime, "base64Data": b64},
                    )
                    uploaded_images.append(uploaded)

            # Q1:retry loop —— 共享 page、TokenBundle、uploaded_images。
            # quota 扣款发生在 service._run_inner.update("generating") 时,
            # 且有 quota_recorded 闸门只扣 1 次 —— 重试不重复扣。
            prompt_to_send = prompt
            # 保留 task 已有的改词次数；同一 runner 内的生成失败重试也
            # 要写回 DB，避免 UI 一直显示 0 次、下一轮任务又从旧 prompt 开始。
            base_prompt_retry_count = max(0, int(prompt_retry_count or 0))
            attempt = 0
            while True:
                try:
                    return await self._submit_and_poll(
                        page,
                        prompt_to_send,
                        model,
                        ratio,
                        duration,
                        fingerprint,
                        token_bundle,
                        mode,
                        uploaded_images,
                        update,
                        cancel_event,
                        profile_dir,
                        use_real_browser=True,  # v0.3.2:UI click 路径
                        owner_task_id=owner_task_id,
                    )
                except DoubaoContentRejected as exc:
                    attempt += 1
                    if max_reject_retries <= 0 or attempt > max_reject_retries:
                        raise
                    failure = classify_failure(str(exc))
                    new_prompt = revise_prompt(prompt_to_send, failure, attempt=attempt)
                    # revise 拿回原 prompt 或空字符串 → 改写器认为无药可救,
                    # 别浪费豆包次数,直接报失败让上层退款。
                    if not new_prompt or new_prompt == prompt_to_send:
                        raise
                    _LOGGER.warning(
                        "event=video_content_reject_revise attempt=%d max=%d reason=%s",
                        attempt, max_reject_retries, exc.error_message,
                    )
                    update(
                        prompt=new_prompt,
                        prompt_retry_count=base_prompt_retry_count + attempt,
                        error_message=(
                            f"豆包拒绝(第 {attempt}/{max_reject_retries} 次改写重试中)"
                        ),
                    )
                    prompt_to_send = new_prompt
                    continue
        except AegisBlocked:
            await self._release_profile_context(profile_dir)
            raise
        finally:
            with contextlib.suppress(Exception):
                await page.close()

    async def _submit_and_poll(
        self,
        page: Page,
        prompt: str,
        model: str,
        ratio: str,
        duration: int,
        fingerprint: str,
        token_bundle,
        mode: str,
        uploaded_images: list[dict],
        update: Callable[..., None],
        cancel_event: threading.Event,
        profile_dir: Path,
        *,
        use_real_browser: bool = True,
        expected_local_message_ids: set[str] | None = None,
        expected_remote_task_ids: set[str] | None = None,
        owner_task_id: str | None = None,
    ) -> dict[str, str]:
        """v0.3.2:run() 的 submit + poll 切片,被 retry loop 复用。

        use_real_browser 开关:
        - True(默认)= 真实 UI click 路径(navigate → 点 视频 tab → type → 点 send)
          用 page.on('response') 拦截 /chat/completion 响应,服务端口气不可见
          「非 page.evaluate 触发」,绕过 shark_admin 风控。
        - False = 原 page.evaluate fetch /chat/completion 路径完整保留
          (selector 失效 / 测试 fixture / 老账号验证 fetch 行为)。

        ack 解析契约不变(parse_sse_ack 不动,3 字段全在):只是 trigger
        方式换了,响应体一致 → service.py **ack 解包不破坏。

        v0.3.3:`expected_local_message_ids` —— 本任务 submit 时用过的
        local_message_id 集合(t2v 1 个,i2v 2 个)。不传 → 退回到旧行为
        (envelope 上有 id 也走 fall through,即 race 防御关闭)。

        v0.3.5:`expected_remote_task_ids` —— submit 时抽到的服务端
        `creation.id`(=remote_task_id)。在 chain response 里 `creation.id`
        命中这一集合是**最强证据**(`parse_creation_result` 优先级 1)。
        """
        if use_real_browser:
            # v0.3.2:UI click → /chat/completion 响应 → parse_sse_ack。
            # v0.3.5.5:同 profile 仅串行 submit→ACK,避免并发页面拿到同一组
            # conversation/section/question。锁在 poll 前释放,生成仍可并发。
            async with self._submit_ack_lock_for(profile_dir):
                # 只有明确观察到 completion request **没有发出**时才允许完整
                # 重走一次新会话提交流程。request 已发出但 ACK 丢失属于歧义
                # 状态，绝不重发，避免重复生成和重复计费。
                ack_state: dict[str, object] = {}
                text = ""
                for submit_attempt in range(2):
                    async with _ack_interceptor(page) as ack_state:
                        await submit_via_ui(
                            page,
                            prompt,
                            model=model,
                            ratio=ratio,
                            duration=duration,
                            profile_dir=profile_dir,
                            update=update,
                        )
                        try:
                            text = await _wait_for_ack(
                                ack_state, timeout=_UI_ACK_WAIT_SECONDS,
                            )
                        except _AckWaitTimeout:
                            # 发送动作本身可能触发延迟挂载的 aegis。重试前先
                            # 做一次只读探测，命中就沿用 fail-fast 释放 profile。
                            await _handle_aegis_in_poll(
                                page, profile_dir, update
                            )
                            if bool(ack_state.get("request_seen", False)):
                                raise
                            if submit_attempt == 0:
                                _LOGGER.warning(
                                    "event=video_submit_retry_no_request "
                                    "profile=%s attempt=1",
                                    profile_dir,
                                )
                                continue
                            raise
                    break
            ack = parse_sse_ack(text)
            # v0.3.3 race 防御:浏览器侧 crypto.randomUUID 生成的 id 我们
            # Python 侧拿不到,只能从 server echo 的 SSE_ACK payload 里抽。
            # 调用方没传 expected → 用 ack 里抓到的 id(若 ack 里也抓不到
            # 就退化为 None = race 防御关闭,不阻塞正常任务)。
            ack_payload = ack_state.get("ack_payload")
            if isinstance(ack_payload, dict):
                if expected_local_message_ids is None:
                    extracted = _extract_local_message_ids_from_ack_payload(ack_payload)
                    if extracted:
                        expected_local_message_ids = extracted
                # v0.3.5:同理从 ack 抽服务端 creation.id(=remote_task_id)
                if expected_remote_task_ids is None:
                    extracted_remote = _extract_remote_task_ids_from_ack_payload(ack_payload)
                    if extracted_remote:
                        expected_remote_task_ids = extracted_remote
            update(status="generating", **ack)
        else:
            # 原 fetch 路径完整保留 —— 测试 + 回滚兜底
            payload = build_completion_payload(
                prompt,
                model,
                ratio,
                duration,
                fingerprint,
                mode=mode,
                images=uploaded_images or None,
                **token_bundle.to_client_meta(),
            )
            local_id = payload["client_meta"]["local_conversation_id"]
            await page.evaluate(
                "id => history.replaceState({}, '', '/chat/' + id)", local_id,
            )
            response = await page.evaluate(
                COMPLETION_SCRIPT, {"payload": payload},
            )
            if response["status"] != 200:
                raise RuntimeError(
                    f"豆包提交接口返回 HTTP {response['status']}"
                )
            ack = parse_sse_ack(response["text"])
            # v0.3.3 race 防御:fetch 路径是 Python 构造的 payload,直接
            # 从 messages 数组里抽我们自己生成的两个 id 即可。
            if expected_local_message_ids is None:
                ids: set[str] = set()
                for message in payload.get("messages") or []:
                    if not isinstance(message, dict):
                        continue
                    lm = message.get("local_message_id")
                    if isinstance(lm, str) and lm:
                        ids.add(lm)
                    mm = message.get("message_meta") or {}
                    mm_lm = mm.get("local_message_id")
                    if isinstance(mm_lm, str) and mm_lm:
                        ids.add(mm_lm)
                if ids:
                    expected_local_message_ids = ids
            # v0.3.5:fetch 路径同样尝试从 ack 抽服务端 creation.id,优先
            # 用 ack_payload,字段缺失就保持 None(走 expected_local + cooldown
            # 兜底)。python-side 的 SSE 解析跟浏览器 UI 路径共用同一个
            # ack_payload 来源(_parse_sse_ack_payload_from_text)。
            if isinstance(response.get("text"), str):
                ack_payload = _parse_sse_ack_payload_from_text(response["text"])
                if isinstance(ack_payload, dict) and expected_remote_task_ids is None:
                    extracted_remote = _extract_remote_task_ids_from_ack_payload(ack_payload)
                    if extracted_remote:
                        expected_remote_task_ids = extracted_remote
            update(status="generating", **ack)

        deadline = time.monotonic() + self.timeout
        poll_log_every = max(5, int(self.timeout / 6))  # 默认 20min → 每 3min 一条;最短 5
        poll_count = 0
        poll_interval_s = max(1, int(self.poll_interval))  # v0.3.4:wait 拆 1s 段
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                raise RuntimeError("任务已取消")
            # 每次 poll 都先做只读探测，避免弹窗把 chain 轮询拖到超时。
            await _handle_aegis_in_poll(page, profile_dir, update)
            chain = await page.evaluate(CHAIN_SCRIPT, {"conversationId": ack["conversation_id"]})
            if chain["status"] != 200:
                raise RuntimeError(f"豆包结果接口返回 HTTP {chain['status']}")
            # v0.3.3 race 防御:同账号并发 A1/A2 → 字节偶尔把 A1 的 creation
            # 塞给 A2 的 chain response。expected_local_message_ids 把本任务
            # submit 时用过的 id 喂给 parse_creation_result,串话的 creation
            # 没有匹配的 id 会被跳过 → 继续 poll 直到拿到自己的。
            # v0.3.5:新增 expected_remote_task_ids,服务端 creation.id 命中是
            # 优先级 1 强证据(envelope 命中是优先级 2)。两个都为 None 时退化为
            # 完全向后兼容(无 race 防御)。
            result = parse_creation_result(
                chain["data"],
                expected_local_message_ids=expected_local_message_ids,
                expected_remote_task_ids=expected_remote_task_ids,
                owner_task_id=owner_task_id,
            )
            if result:
                update(status="resolving", **result)
                return await self._resolve_original_download(page, result, cancel_event)
            poll_count += 1
            # v0.2.31:polling 期间节流打日志,避免之前「卡生成中」无任何输出,
            # 让用户能看到 chain 还在跑、还要等多久。DEBUG 级别,默认不打印;
            # 出问题用 `LOGURU_LEVEL=DEBUG` 起程序即可看到。
            if poll_count % poll_log_every == 0:
                remaining = max(0, int(deadline - time.monotonic()))
                _LOGGER.debug(
                    "video poll still waiting conv=%s polls=%d remaining=%ds",
                    ack.get("conversation_id", "?"), poll_count, remaining,
                )
            # v0.3.4:wait_for_timeout 拆成 1s 段 + 每段先 probe —— 避免 aegis
            # 弹窗刚弹出后到下一次 wait 之间被关掉 page 时客户端毫无反应。
            # 一次 wait 最多 1s(避免 TargetClosedError 堆积),命中 popup
            # 立即 break 进入下一轮 chain 探测。
            for _ in range(poll_interval_s):
                if time.monotonic() >= deadline:
                    break
                try:
                    if await aegis_popup_present(page):
                        break
                except Exception:
                    # page 在 wait 期间被外部关掉 → 跳出,上层 catch 后处理
                    break
                await asyncio.sleep(1)
        raise RuntimeError("视频生成超时")

    async def _resolve_original_download(self, page, result: dict[str, str], cancel_event: threading.Event) -> dict[str, str]:
        fallback = {**result, "result_url": result["fallback_result_url"]}
        homepage = await page.evaluate(
            AISPACE_SCRIPT,
            {"endpoint": "/samantha/aispace/homepage", "body": {}},
        )
        if homepage["status"] != 200:
            return fallback
        folder_id = find_creation_directory(homepage["data"])
        if not folder_id or not result.get("vid"):
            return fallback

        deadline = time.monotonic() + 120
        node_id = None
        while time.monotonic() < deadline and not cancel_event.is_set():
            nodes = await page.evaluate(
                AISPACE_SCRIPT,
                {
                    "endpoint": "/samantha/aispace/node_info",
                    "body": {
                        "node_id": folder_id,
                        "need_full_path": True,
                        "size": 50,
                        "sort_param": {"need_sort_config": True, "sort_order": 1, "sort_type": 0},
                    },
                },
            )
            if nodes["status"] == 200:
                node_id = find_video_node(nodes["data"], result["vid"])
            if node_id:
                break
            await page.wait_for_timeout(5_000)
        if not node_id:
            return fallback

        download = await page.evaluate(
            AISPACE_SCRIPT,
            {
                "endpoint": "/samantha/aispace/get_download_info",
                "body": {"requests": [{"node_id": node_id}]},
            },
        )
        original = parse_download_info(download["data"]) if download["status"] == 200 else None
        return {**result, **(original or {"result_url": result["fallback_result_url"]})}
