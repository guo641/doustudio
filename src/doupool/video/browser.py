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
from ..captcha.solver import (
    AegisCaptchaDisabled as _AegisCaptchaDisabled,
    AegisCaptchaFailed as _AegisCaptchaFailed,
    detect_aegis_captcha as _detect_aegis_captcha,
    is_in_cooldown as _captcha_is_in_cooldown,
    make_client as _make_captcha_client,
    mark_cooldown as _captcha_mark_cooldown,
    probe_aegis_quickly as _probe_aegis_quickly,
    solve_aegis_captcha as _solve_aegis_captcha,
)
from ..captcha.config import load_credentials as _load_captcha_credentials
from .protocol import (
    EXTRA_CLIENT_META_KEYS,
    DoubaoContentRejected,
    DoubaoRateLimited,
    build_completion_payload,
    find_creation_directory,
    find_video_node,
    parse_creation_result,
    parse_sse_ack,
    parse_download_info,
)

# v0.3.1.2:video runner 在 page.goto 之后、提交之前等弹窗出现的最长时间。
# aegis 弹窗通常在 navigation 后 1-3s 渲染;4s 缓冲。
_CAPTCHA_DETECT_WAIT_BEFORE_SUBMIT_SECONDS = 4.0
# v0.3.1.3(去掉):用户实测 aegis 实际在「开始生成视频时」弹,不在 page.goto 后。
# pre-submit hook 等 4s 经常空等,真正起作用的是 poll 期间的 hook;把后者挪到
# chain 请求之前 + 去掉每 3 轮节流,改成每次 poll 都探。captcha 探本身是纯 DOM
# 查询(几十 ms),不会拖慢 timeout。_CAPTCHA_DETECT_INTERVAL_POLLS 常量删除。

# v0.3.2:UI click 路径 selector 常量。类名稳定优先,SVG path 兜底。
# 视频 tab 用 a11y role + 文本(豆包前端 aria 标签就是「视频」)。
VIDEO_TAB_SEL = "[role='tab']:has-text('视频')"
EDITOR_SEL = "div[contenteditable='true']"
SEND_BTN_SEL = ".send-btn-wrapper button"
SEND_BTN_FALLBACK_SEL = "button:has(svg path[d^='M4.93934 10.2598'])"
CREATE_IMAGE_URL = "https://www.doubao.com/chat/create-image"
_VIDEO_RATIO_OPTIONS = ("3:4", "4:3", "9:16", "16:9", "1:1", "21:9")
_VIDEO_OPTIONS_TRIGGER_RE = re.compile(
    r"^\s*(?:自动|3:4|4:3|9:16|16:9|1:1|21:9)\s*·\s*(?:[4-9]|1[0-5])s\s*$"
)
_VIDEO_OPTIONS_MENU_WAIT_MS = 1_000
_VIDEO_OPTIONS_READBACK_WAIT_MS = 1_500
_VIDEO_OPTIONS_CLOSE_WAIT_MS = 1_500
_VIDEO_OPTIONS_TRIGGER_WAIT_MS = 1_500
_VIDEO_OPTIONS_MIN_VISIBLE_RATIOS = 4
# v0.3.2.3:UI click 路径下的弹窗缓冲。**经验值**(用户在 v0.3.2.2 反馈):
# 实测 aegis 弹窗在 navigate 后 3-5s 才会出现,v0.3.2.2 用的 2s 经常空等,
# 然后 POST 立即飞出 + 弹窗刚好弹 → shark_admin 拒绝。所以 submit 前必须
# 给弹窗足够的"出现窗口",而且必须确认弹窗消失后才能点 send。
_UI_CAPTCHA_WAIT_SECONDS = 6.0  # 弹窗出现最长的等待(用户实测 3-5s)
_UI_CAPTCHA_VERIFY_GONE_SECONDS = 4.0  # 解完后等弹窗彻底消失的轮询窗口
_UI_CAPTCHA_DETECT_POLL_INTERVAL = 0.5  # 弹窗轮询间隔
# 拦截 /chat/completion 响应的最长时间,跟 click → POST → SSE 飞出去对齐
_UI_ACK_WAIT_SECONDS = 30.0

# v0.3.2.5:shark_admin 拒绝 → 不关浏览器 → 等滑块 → 图鉴拖 → 重提 的循环上限。
# 用户实测触发后通常 1 次 solve + retry 就能过,但同一账号连续 2 次都过不了时
# 应直接失败,免得无限循环浪费 token / 额度。2 次足够覆盖偶发网络抖动。
_MAX_RISK_RETRY = 2

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
    """v0.3.4:poll 循环里碰到 aegis 弹窗时的统一处理入口。

    设计目标:
      - 不被 cooldown 短路(廉价探测必须每次跑)
      - 凭证可用 + 未在 cooldown → 调完整 solver 求解
      - 凭证不可用 / 已 cooldown → **fail-fast**,不浪费 30 分钟盲飞
        (用户原话:"我在后台看到视频已经生成成功了,你特么还卡在生成中")

    返回:
      True  = 探测到弹窗并**已解决**(solver 跑通),调用方继续 poll
      False = 未探测到弹窗,调用方继续 poll
      raise  AegisCaptchaUnresolvable = 探测到但**无法自动解**
              (凭证关 / 已 cooldown),调用方应终止任务

    与 `_try_solve_captcha_in_video` 的关系:
      本函数是 poll 专用入口,内部仍可能调 `_try_solve_captcha_in_video`
      做求解(它会走 cooldown),但探测 + fail-fast 决策由本函数负责。
    """
    if not await _probe_aegis_quickly(page):
        return False

    # 探测到了 → 先看凭证能不能用
    creds = _load_captcha_credentials()
    client = None
    try:
        client = _make_captcha_client(creds)
    except _AegisCaptchaDisabled as exc:
        # 凭证不可用 —— 整个 ~35 分钟视频生成都要被这个 popup 挡住,
        # 早 fail 早让用户知道,别让任务挂"生成中"直到超时。
        _LOGGER.warning(
            "aegis popup detected during poll but credentials unusable: %s; "
            "failing task fast instead of polling blind",
            exc,
        )
        _captcha_mark_cooldown(str(profile_dir))
        try:
            update(error_message=(
                f"aegis 拖拽验证已弹出,但图鉴打码凭证未配置或已停用({exc})。"
                "请在 Settings → 图鉴打码平台填写凭证并启用,或手动在浏览器中拖动验证后重新提交任务。"
            ))
        except Exception:
            pass
        raise _AegisUnresolvableInPoll(
            "aegis drag captcha blocking poll loop; captcha credentials not configured"
        ) from exc

    # 凭证可用 → 立即关掉 client(只需要 make_client 用来检查可不可用,
    # 真正的 solver 会自己构造 client)。然后调老 solver。
    if client is not None:
        try:
            client.close()
        except Exception:
            pass
    return await _try_solve_captcha_in_video(
        page, profile_dir, update, wait_for_popup_seconds=0.0,
    )


class _AegisUnresolvableInPoll(RuntimeError):
    """v0.3.4:poll 循环里 aegis 弹窗持续挂着但客户端无法自动解。
    抛这个让上层 service 把任务标 failed + 退额度。"""


async def _try_solve_captcha_in_video(
    page: Page,
    profile_dir: Path,
    update: Callable[..., None],
    *,
    wait_for_popup_seconds: float = 0.0,
) -> bool:
    """v0.3.1.2:video runner 路径的 captcha hook —— 与 login 的
    `_try_solve_captcha_if_needed` 不同,这里是 async,跑在 video runner
    的事件循环里。**不能**套 `asyncio.run`(login 那样),否则会
    "asyncio.run() cannot be called from a running event loop"。

    `wait_for_popup_seconds`:
      - 0 = poll 路径,弹窗已在(刚 detect 到)就解
      - > 0 = 提交前,先等 N 秒让弹窗出现再 detect

    返回 True = 本次调用尝试解过(无论成败),False = cooldown/无弹窗。
    **失败/异常一律吞掉不 raise** —— 让 task 继续走原有失败路径。
    aegis 弹窗还在 / 图鉴坏了 / 凭证关掉 / 网络挂了,都不会挂掉 task,
    只是把 cooldown 标上,防止短时间内反复调图鉴 API 浪费钱。

    account_key = `str(profile_dir)`,与 login 路径用同一份,共享 30 分钟
    cooldown —— login keepalive 刚解完 / video 刚解完,两边都自动跳过。
    """
    account_key = str(profile_dir)
    if _captcha_is_in_cooldown(account_key):
        return False

    if wait_for_popup_seconds > 0:
        try:
            await asyncio.sleep(wait_for_popup_seconds)
        except Exception:
            return False

    try:
        kind = await _detect_aegis_captcha(page)
    except Exception as exc:
        _LOGGER.debug("aegis detect failed: %s", exc)
        return False
    if kind.value == "unknown":
        return False

    creds = _load_captcha_credentials()
    try:
        client = _make_captcha_client(creds)
    except _AegisCaptchaDisabled as exc:
        _LOGGER.info("aegis detected but captcha disabled: %s", exc)
        _captcha_mark_cooldown(account_key)
        try:
            update(error_message=f"检测到拖拽验证,但图鉴打码未启用或凭证缺失,任务将按豆包原流程处理({exc})")
        except Exception:
            pass
        return True

    def _on_state(s: str) -> None:
        msg = {
            "uploading": "正在通过图鉴打码平台识别拖拽验证",
            "dragging": "正在拟人拖拽通过验证",
            "verifying": "等待 aegis 校验结果",
            "ok": "拖拽验证已通过,继续提交任务",
            "failed": "拖拽验证未通过,任务将按豆包原流程处理",
        }.get(s, f"图鉴解算:{s}")
        _LOGGER.info("video captcha state: %s", s)
        try:
            update(error_message=msg)
        except Exception:
            pass

    try:
        try:
            await _solve_aegis_captcha(page, client, on_state=_on_state)
        finally:
            client.close()
    except _AegisCaptchaFailed as exc:
        _LOGGER.warning("aegis solver failed in video path: %s", exc)
        _captcha_mark_cooldown(account_key)
        try:
            update(error_message=f"图鉴自动解算失败({exc}),任务将按豆包原流程处理")
        except Exception:
            pass
        return True
    except _AegisCaptchaDisabled as exc:
        _LOGGER.warning("aegis solver disabled mid-run: %s", exc)
        _captcha_mark_cooldown(account_key)
        try:
            update(error_message=f"图鉴打码平台凭证失效({exc})")
        except Exception:
            pass
        return True
    except Exception:  # noqa: BLE001
        _LOGGER.exception("aegis solver unexpected error in video path")
        _captcha_mark_cooldown(account_key)
        return True
    else:
        _LOGGER.info("aegis solved for video account=%s, marking cooldown", account_key)
        _captcha_mark_cooldown(account_key)
        try:
            update(error_message="拖拽验证已通过,继续提交任务")
        except Exception:
            pass
        return True


async def _pre_submit_aegis_gate(
    page: Page,
    profile_dir: Path,
    update: Callable[..., None],
) -> bool:
    """v0.3.2.4:提交前的**阻塞式** aegis 网关。

    用户反馈(v0.3.2.3,v0.3.2.4):
      「提示词粘贴进去,刚提交,滑块弹窗刚出现,窗口就被自动关闭了。
       应该是软件把滑块认定为问题,直接关闭了窗口。」

    根因:`submit_via_ui` step 2 用 `_UI_CAPTCHA_WAIT_SECONDS = 2.0s` 等弹窗。
    实测 aegis 弹窗在 navigation 后 3-5s 才会出现,所以:
      - 2s 等待期内没有弹窗 → step 6 直接点 send
      - POST 飞出去 → 弹窗刚好在 POST 中出现
      - 服务端 shark_admin 看到「非真人触发」(因为 aegis 弹窗挂在前面挡 submit)→ 拒绝
      - 弹窗还在挂着 → 用户看到「弹窗刚出现就关」(其实是 aegis 超时自动收起)
      - 用户感觉「软件主动关了窗口」

    本 helper 改阻塞式轮询:
      1. cooldown 检查:在冷却期直接放行(已解过别浪费)
      2. 轮询 detect aegis 弹窗(每 0.5s 一次,最多 6s —— 用户实测窗口 3-5s)
      3. 探测到 → 调 `solve_aegis_captcha` 拖
      4. 解完后,**轮询确认弹窗彻底消失**(每 0.5s,最多 4s)
      5. 弹窗仍在 / 解失败 / 异常 → 返 False,**阻止** click send
         (防 POST + 弹窗撞车 → shark_admin 拒 → 浪费视频额度)

    返回 True = 放行(可以点 send)/ False = 网关拒绝(上层 raise 阻止 submit)。

    **v0.3.2.4 重要修正**:solver 失败 / 凭证关 / 异常统一返 False,**不**
    再「降级放行让 submit 撞弹窗」。原因:
      - 降级放行 = POST 撞弹窗 = shark_admin 拒 = 视频额度被扣 + 任务失败
      - 阻止 submit = 30 分钟 cooldown 保护 + 任务标失败但**不退额度**
        (用户没提交成功就不会进 chain poll,链上不会有扣费记录)
      - 阻断告诉用户「拖拽未通过,稍后重试」比 shark_admin 莫大扣费更友好

    **v0.3.2.4 第 2 处修正**:本 helper 现在**也用于 paste 之后** (submit_via_ui
    step 6 替换为再次调本 helper,等 6s + 解 + 验证消失,而不是 fire-and-forget)。
    实测中 aegis 弹窗有时在 submit 前没起,在 paste / click send 之间才挂上,
    这种 case v0.3.2.3 的「step 2 单点拦截」覆盖不到,必须 step 6 再拦一次。

    cooldown 复用:`str(profile_dir)` 与 login 路径共享同一份 30min dict,
    login keepalive 刚解完 → video 路径立即放行。
    """
    account_key = str(profile_dir)
    if _captcha_is_in_cooldown(account_key):
        # login / 上次 video 路径 30min 内已解过 — 直接放行
        _LOGGER.debug("aegis gate: account=%s in cooldown, allow submit", account_key)
        return True

    # Step 1:轮询 detect aegis 弹窗(最多 6s,实测窗口 3-5s)
    popup_present = False
    deadline = time.monotonic() + _UI_CAPTCHA_WAIT_SECONDS
    last_kind = None
    while time.monotonic() < deadline:
        try:
            kind = await _detect_aegis_captcha(page)
        except Exception as exc:
            _LOGGER.debug("aegis gate detect failed: %s", exc)
            kind = None
        if kind is not None and kind.value != "unknown":
            popup_present = True
            last_kind = kind
            break
        await asyncio.sleep(_UI_CAPTCHA_DETECT_POLL_INTERVAL)

    if not popup_present:
        # 探测期内无弹窗 — 好事,放行
        _LOGGER.debug("aegis gate: no popup within %.1fs, allow submit", _UI_CAPTCHA_WAIT_SECONDS)
        return True

    # Step 2:弹窗已挂上,调 solver 拖
    _LOGGER.info("aegis gate: popup=%s detected, solving", last_kind)
    try:
        update(error_message="提交前检测到拖拽验证,正在通过图鉴打码平台识别")
    except Exception:
        pass

    creds = _load_captcha_credentials()
    try:
        client = _make_captcha_client(creds)
    except _AegisCaptchaDisabled as exc:
        _LOGGER.warning("aegis gate: solver disabled: %s", exc)
        # v0.3.2.4:凭证关也返 False — 别让 POST 撞弹窗浪费额度
        _captcha_mark_cooldown(account_key)
        try:
            update(error_message=f"图鉴凭证未配置或已停用({exc}),暂不提交以免扣额度")
        except Exception:
            pass
        return False

    def _on_state(s: str) -> None:
        msg = {
            "uploading": "图鉴正在识别拖拽图...",
            "dragging": "图鉴正在拖动滑块...",
            "verifying": "等待 aegis 校验拖拽结果...",
            "ok": "拖拽已通过,等待弹窗消失",
            "failed": "图鉴解算失败",
        }.get(s, f"图鉴:{s}")
        try:
            update(error_message=msg)
        except Exception:
            pass

    try:
        try:
            await _solve_aegis_captcha(page, client, on_state=_on_state)
        finally:
            client.close()
    except (_AegisCaptchaFailed, _AegisCaptchaDisabled) as exc:
        _LOGGER.warning("aegis gate solver failed: %s", exc)
        _captcha_mark_cooldown(account_key)
        # v0.3.2.4:解失败也返 False — 不再放行让 POST 撞弹窗
        try:
            update(error_message=f"图鉴解算失败({exc}),暂不提交以免扣额度")
        except Exception:
            pass
        return False
    except Exception:  # noqa: BLE001
        _LOGGER.exception("aegis gate solver unexpected error")
        _captcha_mark_cooldown(account_key)
        try:
            update(error_message="图鉴解算异常,暂不提交以免扣额度")
        except Exception:
            pass
        return False

    # Step 3:解完后轮询确认弹窗彻底消失(最多 4s)
    gone_deadline = time.monotonic() + _UI_CAPTCHA_VERIFY_GONE_SECONDS
    while time.monotonic() < gone_deadline:
        try:
            kind = await _detect_aegis_captcha(page)
        except Exception:
            kind = None
        if kind is None or kind.value == "unknown":
            # 弹窗消失 — 放行
            _LOGGER.info(
                "aegis gate: popup gone after %.2fs, allow submit",
                _UI_CAPTCHA_VERIFY_GONE_SECONDS - (gone_deadline - time.monotonic()),
            )
            try:
                update(error_message="拖拽验证已通过,正在提交任务")
            except Exception:
                pass
            return True
        await asyncio.sleep(_UI_CAPTCHA_DETECT_POLL_INTERVAL)

    # Step 4:解完后 4s 弹窗仍在 — **不放行**(防止 POST + 弹窗撞车触发 shark_admin)
    _LOGGER.error(
        "aegis gate: popup still present after solve within %.1fs, blocking submit",
        _UI_CAPTCHA_VERIFY_GONE_SECONDS,
    )
    try:
        update(error_message="拖拽验证后弹窗未消失,暂不提交,稍后重试")
    except Exception:
        pass
    return False


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

    行为参考 captcha/solver.py 的 `_find_element_box` 模式:
    locator().first.wait_for(state='visible') → bounding_box → mouse.move/down/up。
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
    state: dict[str, object] = {}

    async def _on_response(response):
        url = response.url
        if "/chat/completion" not in url:
            return
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

    page.on("response", _on_response)
    try:
        yield state
    finally:
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
    raise RuntimeError(f"等待 /chat/completion 响应超时 ({timeout}s)")


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


async def _find_video_options_trigger(page: Page):
    candidates = page.locator("button").filter(has_text=_VIDEO_OPTIONS_TRIGGER_RE)
    return await _first_visible_locator(candidates)


async def _wait_for_video_options_trigger(page: Page):
    step_ms = 50
    attempts = max(
        1,
        (_VIDEO_OPTIONS_TRIGGER_WAIT_MS + step_ms - 1) // step_ms,
    )
    for attempt in range(attempts):
        trigger = await _find_video_options_trigger(page)
        if trigger is not None:
            return trigger
        if attempt + 1 < attempts:
            await page.wait_for_timeout(step_ms)
    return None


async def _open_video_options(page: Page):
    trigger = await _wait_for_video_options_trigger(page)
    if trigger is None:
        raise RuntimeError(f"视频参数按钮未找到: {page.url}")

    last_error: Exception | None = None
    visible_options: list[str] = []
    for attempt in range(2):
        try:
            if attempt:
                # 触发按钮是 toggle。第一次若已经打开但菜单渲染超时,
                # 直接再点会把它关掉;先用 Escape 归一化到关闭态。
                await page.keyboard.press("Escape")
                await page.wait_for_timeout(100)
                trigger = await _wait_for_video_options_trigger(page)
                if trigger is None:
                    raise RuntimeError("重试前视频参数按钮消失")
            await trigger.click()
            visible_options = await _wait_for_video_ratio_options(page)
            if len(visible_options) >= _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS:
                return trigger, visible_options
        except Exception as exc:
            last_error = exc
    raise RuntimeError(
        f"视频参数菜单未展开: {page.url}; "
        f"visible_ratio_options={visible_options}; last={last_error}"
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


async def _wait_for_video_duration_control(page: Page, *, timeout_ms: int = 500):
    step_ms = 50
    attempts = max(1, (timeout_ms + step_ms - 1) // step_ms)
    for attempt in range(attempts):
        kind, control = await _find_video_duration_control(page)
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
    min_value = int(await control.get_attribute("aria-valuemin") or 4)
    max_value = int(await control.get_attribute("aria-valuemax") or 15)
    if not min_value <= duration <= max_value:
        raise ValueError(
            f"视频时长必须在 {min_value} 到 {max_value} 秒之间: {duration}"
        )

    try:
        await control.focus()
        await control.press("Home")
        for _ in range(duration - min_value):
            await control.press("ArrowRight")
        await page.wait_for_timeout(100)
        now = await control.get_attribute("aria-valuenow")
        if now is not None and int(float(now)) == duration:
            return
    except Exception:
        pass

    # 某些自定义 slider 不响应键盘,退回到真实鼠标拖动。
    thumb_box = await control.bounding_box()
    track_box = await control.locator("xpath=..").bounding_box()
    if not thumb_box or not track_box or track_box["width"] <= 0:
        raise RuntimeError("视频时长滑块无法定位拖动轨道")
    start_x = thumb_box["x"] + thumb_box["width"] / 2
    start_y = thumb_box["y"] + thumb_box["height"] / 2
    target_x = track_box["x"] + track_box["width"] * (
        (duration - min_value) / (max_value - min_value)
    )
    await page.mouse.move(start_x, start_y)
    await page.mouse.down()
    await page.mouse.move(target_x, start_y, steps=8)
    await page.mouse.up()
    await page.wait_for_timeout(100)

    now = await control.get_attribute("aria-valuenow")
    if now is not None and int(float(now)) != duration:
        raise RuntimeError(
            f"视频时长滑块设置失败: expected={duration}s actual={now}s"
        )


async def _wait_for_video_options_closed(
    page: Page,
    *,
    timeout_ms: int = _VIDEO_OPTIONS_CLOSE_WAIT_MS,
) -> list[str]:
    step_ms = 50
    elapsed_ms = 0
    visible: list[str] = []
    while True:
        visible = await _visible_video_ratio_options(page)
        if len(visible) < _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS:
            return visible
        if elapsed_ms >= timeout_ms:
            return visible
        wait_ms = min(step_ms, timeout_ms - elapsed_ms)
        await page.wait_for_timeout(wait_ms)
        elapsed_ms += wait_ms


async def _wait_for_video_options_readback(
    page: Page,
    *,
    ratio: str,
    duration: int,
) -> str:
    expected = re.compile(
        rf"^\s*{re.escape(ratio)}\s*·\s*{duration}s\s*$"
    )
    step_ms = 50
    attempts = max(
        1,
        (_VIDEO_OPTIONS_READBACK_WAIT_MS + step_ms - 1) // step_ms,
    )
    actual = ""
    for attempt in range(attempts):
        trigger = await _find_video_options_trigger(page)
        if trigger is not None:
            actual = await trigger.inner_text()
            if expected.fullmatch(actual):
                return actual
        if attempt + 1 < attempts:
            await page.wait_for_timeout(step_ms)
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


async def _close_video_options(page: Page, trigger) -> None:
    await page.keyboard.press("Escape")
    visible_after_escape = await _wait_for_video_options_closed(page)
    if len(visible_after_escape) < _VIDEO_OPTIONS_MIN_VISIBLE_RATIOS:
        return

    # 部分页面版本不处理 Escape,退回再次点击组合按钮关闭 toggle。
    trigger = await _find_video_options_trigger(page) or trigger
    trigger_text = await trigger.inner_text()
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
    if not 4 <= duration <= 15:
        raise ValueError(f"视频时长必须在 4 到 15 秒之间: {duration}")

    trigger, visible_options = await _open_video_options(page)
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

    kind, duration_control = await _wait_for_video_duration_control(page)
    if duration_control is None:
        # 比例按钮若会自动收起弹层,重新点开一次再找 slider。
        if not await _visible_video_ratio_options(page):
            await trigger.click()
            await _wait_for_video_ratio_options(page)
            kind, duration_control = await _wait_for_video_duration_control(page)
    if duration_control is None:
        raise RuntimeError(f"视频时长滑块未找到: {page.url}")

    if kind == "range":
        await _set_native_range_value(duration_control, duration)
    else:
        await _set_aria_slider_value(page, duration_control, duration)

    await _close_video_options(page, trigger)
    await _wait_for_video_options_readback(
        page,
        ratio=ratio,
        duration=duration,
    )


async def submit_via_ui(
    page: Page,
    prompt: str,
    *,
    ratio: str,
    duration: int,
    profile_dir: Path,
    update: Callable[..., None],
) -> None:
    """真实 UI 提交:进入视频 tab,应用参数,粘贴 prompt,再点发送。

    整段在浏览器内执行,绕过 shark_admin 服务端对 page.evaluate POST 的识别。
    前后两次 aegis 兜底(进入 tab 前 + click send 前),wait 2s / 0s;
    cooldown 自动跳过(30min 复用 login 路径的同一份 _captcha_cooldown dict)。
    """
    # 1. 导航到 create-image 页(已在则 skip,避免重复跳转)
    if not page.url.startswith(CREATE_IMAGE_URL):
        await page.goto(CREATE_IMAGE_URL, wait_until="domcontentloaded", timeout=30_000)
        await page.wait_for_timeout(1_000)  # 等 React mount 完成

    # 2. v0.3.2.3:**阻塞式** aegis 网关 —— 取代 v0.3.2.2 的 fire-and-forget 探测
    # 旧逻辑:`_UI_CAPTCHA_WAIT_SECONDS = 2.0s` 等弹窗,实测 aegis 3-5s 才出现,
    # 经常空等 → step 6 点 send → POST 撞弹窗 → shark_admin 拒绝。
    # 新逻辑:轮询 6s 探弹窗 → 解 → 验证消失(4s)→ 还在就**不点 send**。
    if not await _pre_submit_aegis_gate(page, profile_dir, update):
        # 弹窗 4s 内没消失 — 阻止 POST,告诉上层重试或换号
        raise RuntimeError(
            "aegis 拖拽验证未通过或弹窗未消失,暂不提交任务以免触发服务端风控。"
            "请稍候 30s 重试,或确认图鉴打码平台账号配额。"
        )

    # 3. 点视频 tab(create-image 默认是图像 tab)
    await try_click(page, (VIDEO_TAB_SEL,), timeout=5.0)
    await page.wait_for_timeout(300)

    # 4. 应用任务比例 + 时长,不继承豆包页面默认或 profile 上次状态。
    await _apply_video_options(page, ratio=ratio, duration=duration)

    # 5. 清空输入框 —— retry 路径 revise 后调用,这里幂等
    await clear_prose_mirror(page)

    # 6. 输入 prompt —— v0.3.2.1:改 clipboard paste(不是 keyboard.type)
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
    _LOGGER.info(
        "[v0.3.5.2 DEBUG submit_via_ui] page.url=%s prompt[:80]=%r",
        page.url,
        prompt[:80],
    )
    # writeText 走 page.evaluate —— 必须 launch_persistent_context 已 grant
    # clipboard-read-write 权限(见 _build_launch_kwargs)
    await page.evaluate(
        "(text) => navigator.clipboard.writeText(text)",
        prompt,
    )
    # 给 clipboard 写入一点点时间(MS Edge 偶发 readback 竞速)
    await page.wait_for_timeout(50)
    await page.keyboard.press("Control+V")
    await page.wait_for_timeout(150)  # 等 ProseMirror 同步 internal state

    # 7. v0.3.2.4:**再跑一次阻塞式 aegis 网关**(paste 后、click send 前)
    # v0.3.2.3 这步用 _try_solve_captcha_in_video(wait=0) 仅 fire-and-forget,
    # 用户实测中 aegis 弹窗常在 paste / click send 之间才挂上,前一次(step 2)
    # 网关捕捉不到;step 6 用 wait=0 探测也来不及。修法:step 6 直接复用
    # _pre_submit_aegis_gate(cooldown 自动跳过 30min 内已解过场景,
    # 第 2 次跑跟第 1 次跑共享 cooldown)。
    if not await _pre_submit_aegis_gate(page, profile_dir, update):
        raise RuntimeError(
            "粘贴后再次检测到拖拽验证且未通过,暂不提交任务以免触发服务端风控。"
            "请稍候 30s 重试,或确认图鉴打码平台账号配额。"
        )
    await try_click(page, (SEND_BTN_SEL, SEND_BTN_FALLBACK_SEL), timeout=5.0)
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
                # v0.3.4:recheck 路径同样会撞 aegis(用户查视频时正好赶上弹窗),
                # 走相同的 fail-fast / solve 逻辑,不让 aegis 把 recheck 阻塞到超时。
                try:
                    await _handle_aegis_in_poll(page, profile_dir, update)
                except _AegisUnresolvableInPoll:
                    raise
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
                        if await _probe_aegis_quickly(page):
                            break
                    except Exception:
                        break
                    await asyncio.sleep(1)
            return None
        finally:
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

            # v0.3.1.2:提交前探 aegis 弹窗 —— 弹窗通常在 navigation 后 1-3s
            # 渲染,等 4s 后再 detect + solve。失败/凭证关一律吞 + mark cooldown,
            # 不挂 task。helper 内部 update(error_message=...) 上报进度,前端
            # 看到「正在通过图鉴打码平台识别拖拽验证」之类提示。
            await _try_solve_captcha_in_video(
                page,
                profile_dir,
                update,
                wait_for_popup_seconds=_CAPTCHA_DETECT_WAIT_BEFORE_SUBMIT_SECONDS,
            )

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
            attempt = 0
            risk_attempt = 0  # v0.3.2.5:shark_admin 重试次数,独立于 reject 重试
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
                except DoubaoRateLimited as exc:
                    # v0.3.2.5:shark_admin 风控拦截 —— **不能**关浏览器。
                    # 用户反馈(2026-08-13):「还是滑块刚出现就被关掉了,我现在要求
                    # 你修改一个审核逻辑,只要是识别到账号被风控拦截这个报错,就
                    # 不能关闭浏览器,必须等滑块出来让图鉴识别再模拟拖动提交。」
                    #
                    # 之前 v0.3.2.3/4 的实现:任何 submit 异常 → bubbles up 到
                    # service.py → run() finally 块 page.close() → 滑块随页面
                    # 一起被关。本分支**不让**它冒泡,保持 page 活着,在原页
                    # 等 aegis 弹窗出现,然后调图鉴 solver 解,再用同一 prompt
                    # 调一次 _submit_and_poll(即 submit_via_ui 再走一遍
                    # navigate/click/type/submit 的完整流程)。
                    #
                    # 关键不变量:
                    # - page 在整个 retry 期间不关(finally 块不会跑到,因为
                    #   本分支既不 raise 也不 return)
                    # - quota 已经在 service._run_inner.update("generating")
                    #   时扣过,失败路径 service.py 会 refund,所以 retry 不
                    #   重复扣
                    # - prompt 不变(同账号同风控场景,改写 prompt 也救不了,
                    #   shark_admin 是按账号 + IP + 请求指纹特征拦的)
                    # - 最多 _MAX_RISK_RETRY 次,避免无限循环浪费 token
                    if not exc.is_risk_control:
                        # 非风控的 quota 限流:沿用原行为(交给 service.py 走
                        # mark_account_limited + assign None + 任务回 queued)
                        raise
                    risk_attempt += 1
                    if risk_attempt > _MAX_RISK_RETRY:
                        _LOGGER.error(
                            "event=video_risk_control_retry_exhausted attempts=%d",
                            risk_attempt - 1,
                        )
                        # 用 reject 风格的 RuntimeError 抛出,让 service.py
                        # 走「task failed + 退额度 + 不阻塞账号」路径
                        raise RuntimeError(
                            f"账号被风控拦截(shark_admin),连续 {risk_attempt - 1} 次"
                            f"自动解滑块均失败,请稍后重试或换号"
                        )
                    _LOGGER.warning(
                        "event=video_risk_control_keepalive attempt=%d/%d | %s",
                        risk_attempt, _MAX_RISK_RETRY, exc,
                    )
                    try:
                        update(
                            error_message=(
                                f"检测到风控拦截,正在保留浏览器等待滑块并自动"
                                f"解算(第 {risk_attempt}/{_MAX_RISK_RETRY} 次)"
                            )
                        )
                    except Exception:
                        pass
                    # 在原 page 上等 aegis 弹窗出现 → 图鉴解 → 验证消失。
                    # _pre_submit_aegis_gate 返回 True = 放行(可继续 submit),
                    # False = 解失败/超时(阻止 submit,本分支 raise 出去走失败路径)。
                    solved = await _pre_submit_aegis_gate(
                        page, profile_dir, update,
                    )
                    if not solved:
                        _LOGGER.error(
                            "event=video_risk_control_captcha_solve_failed attempt=%d",
                            risk_attempt,
                        )
                        raise RuntimeError(
                            f"账号被风控拦截(shark_admin),第 {risk_attempt} 次"
                            f"自动解滑块失败,请稍后重试或确认图鉴凭证"
                        )
                    try:
                        update(
                            error_message=(
                                f"滑块已通过,正在以原 prompt 重新提交"
                                f"(第 {risk_attempt}/{_MAX_RISK_RETRY} 次)"
                            )
                        )
                    except Exception:
                        pass
                    # continue → 重新跑 _submit_and_poll,submit_via_ui 会再
                    # 走一次 create-image → 视频 tab → 清空 → type → send。
                    # page 状态由 _pre_submit_aegis_gate 接管后是「弹窗消失」,
                    # 再走 submit 不会立即撞弹窗。
                    continue
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
                    update(error_message=f"豆包拒绝(第 {attempt}/{max_reject_retries} 次改写重试中)")
                    prompt_to_send = new_prompt
                    continue
        finally:
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
                # 拦截器在 context 内部拿 state,exit 时自动 remove_listener。
                async with _ack_interceptor(page) as ack_state:
                    await submit_via_ui(
                        page,
                        prompt,
                        ratio=ratio,
                        duration=duration,
                        profile_dir=profile_dir,
                        update=update,
                    )
                    text = await _wait_for_ack(
                        ack_state, timeout=_UI_ACK_WAIT_SECONDS,
                    )
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
            # v0.3.1.3 + v0.3.4:每次 poll 都探 aegis,挪到 chain 请求之前。
            # v0.3.1.3 的实现有 bug:`_try_solve_captcha_in_video` 内部被
            # 30 分钟 cooldown 短路,导致探测只在第 1 次生效,后续 30 分钟
            # 完全失明 → 用户看到「卡生成中」,实际 aegis 一直挂着挡 chain。
            # v0.3.4 改用 `_handle_aegis_in_poll` —— 探测不走 cooldown,
            # 凭证不可用时直接抛 `_AegisUnresolvableInPoll` 让任务 fail-fast,
            # 不浪费 35 分钟盲飞。
            try:
                await _handle_aegis_in_poll(page, profile_dir, update)
            except _AegisUnresolvableInPoll:
                raise
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
                    if await _probe_aegis_quickly(page):
                        # 探测到 popup → 跳出 wait 段,下一轮开头由
                        # `_handle_aegis_in_poll` 做凭证检查 / fail-fast / 求解
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
