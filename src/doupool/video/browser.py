from __future__ import annotations

import base64
import json
import mimetypes
import random
import re
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from playwright.sync_api import sync_playwright

from .protocol import (
    EXTRA_CLIENT_META_KEYS,
    build_completion_payload,
    find_creation_directory,
    find_video_node,
    parse_creation_result,
    parse_download_info,
    parse_sse_ack,
)


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


def _read_chromium_cookies(profile_dir: Path) -> dict[str, str]:
    """v0.2.17:读 Chromium Cookies SQLite(Default/Cookies),key 是 cookie name。

    返回 {cookie_name: value},失败抛 TokenBundleUnavailable。Chromium 在 Windows
    下用 SQLite 存 cookie,内部 hosts 表里 doubao.com 一行一行都展开。lock 文件
    临时库 profile 锁住读不到 → 复制到 tmp 再读。
    """
    candidates = [
        profile_dir / "Default" / "Cookies",
        profile_dir / "Default" / "Network" / "Cookies",
    ]
    db_path = next((p for p in candidates if p.exists()), None)
    if db_path is None:
        return {}

    tmp = db_path.with_suffix(".doupool.read.tmp")
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
    finally:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def _read_chromium_local_storage(profile_dir: Path) -> dict[str, str]:
    """v0.2.17:读 Local Storage leveldb,挑出 web_id / device_id / tea_uuid。

    leveldb 是二进制格式(每条记录 varint 头 + key + value),直接读 .log 文件
    用正则扫 `__tea_cache_tokens_497858` / `samantha_web_web_id` 出现的 JSON。
    实测这两个 key 在 Chromium 重启后会被压到 .log 文件(冷启动),足够用。
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
    # __tea_cache_tokens_497858 是一个 JSON 串:{user_unique_id, web_id, ...}
    tea_match = re.search(rb'__tea_cache_tokens_497858(.+?)</script>', raw, re.DOTALL)
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
    sam_match = re.search(rb'samantha_web_web_id(.+?)</script>', raw, re.DOTALL)
    if sam_match:
        try:
            obj = json.loads(sam_match.group(1).decode("utf-8", errors="replace"))
            if isinstance(obj, dict) and obj.get("web_id"):
                out["device_id"] = str(obj["web_id"])
        except (ValueError, TypeError):
            pass
    # 备用:直接 regex 抓 web_id 的 string
    if "web_id" not in out:
        m = re.search(r'"web_id"\s*:\s*"([A-Za-z0-9_\-]{8,80})"', text)
        if m:
            out["web_id"] = m.group(1)
    if "tea_uuid" not in out:
        m = re.search(r'"user_unique_id"\s*:\s*"([A-Za-z0-9_\-]{8,80})"', text)
        if m:
            out["tea_uuid"] = m.group(1)
    return out


def extract_webmssdk_tokens(profile_dir: Path) -> TokenBundle:
    """v0.2.17:从登录后持久化的 Chromium profile 抽 WebMSSDK / TeaSDK 真实指纹。

    读 Default/Cookies(挑 doubao.com 域名的)+ Local Storage/leveldb/000003.log
    拼出 TokenBundle。任意一个关键字段缺失 → 抛 TokenBundleUnavailable,
    UI 引导用户「在浏览器里访问 doubao.com/chat/ 主页 5 秒后点刷新 token」。
    """
    profile_dir = Path(profile_dir)
    cookies = _read_chromium_cookies(profile_dir)
    storage = _read_chromium_local_storage(profile_dir)

    ms_token = cookies.get("msToken", "") or cookies.get("ms_token", "")
    web_id_signature = cookies.get("_signature", "") or cookies.get("samantha_web_id_signature", "")
    web_id = storage.get("web_id", "") or cookies.get("samantha_web_web_id", "")
    device_id = storage.get("device_id", "") or cookies.get("s_v_web_id", "")
    tea_uuid = storage.get("tea_uuid", "") or cookies.get("user_unique_id", "")

    # web_id 是字节风控核心,缺失 = 抽不出来
    missing = []
    if not web_id:
        missing.append("web_id")
    if not device_id:
        missing.append("device_id")
    if missing and not ms_token:
        # msToken 缺失常见(用户刚 login 没让主页跑过 WebMSSDK),只有 web_id 缺失才是硬错
        raise TokenBundleUnavailable(
            f"profile 中缺少 web_id/device_id,字段: {missing}; "
            "请在浏览器里访问 https://www.doubao.com/chat/ 主页 5-10 秒后点「刷新 token」"
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


def read_browser_fingerprint(page, context) -> str:
    """旧版 fp(只取 s_v_web_id cookie)— v0.2.17 之前 main path,现在保留
    是为了 login 模块和外部脚本不破。视频提交已切到 load_browser_context。"""
    page.wait_for_function(
        "JSON.parse(localStorage.getItem('__tea_cache_tokens_497858') || '{}').user_unique_id",
        timeout=15_000,
    )
    cookies = context.cookies(["https://www.doubao.com"])
    fingerprint = next(
        (cookie["value"] for cookie in cookies if cookie["name"] == "s_v_web_id"),
        "",
    )
    if not fingerprint:
        raise RuntimeError("豆包浏览器指纹不可用，请重新登录")
    return fingerprint


def load_browser_context(page, context, *, pc_version: str | None = None) -> TokenBundle:
    """v0.2.17:从已登录 page + context 抽完整 TokenBundle。

    跟 read_browser_fingerprint 行为差:除了 fp cookie 还读 localStorage 的
    web_id / tea_uuid / device_id,组成 TokenBundle 给 payload.client_meta 透传。
    pc_version 优先用 settings 传进来的(从 SettingsService.get("pc_version")
    读),fallback 到模块级 PC_VERSION 常量。
    """
    effective_pc_version = pc_version or PC_VERSION
    page.wait_for_function(
        "JSON.parse(localStorage.getItem('__tea_cache_tokens_497858') || '{}').user_unique_id",
        timeout=15_000,
    )
    cookies = context.cookies(["https://www.doubao.com"])
    cookie_map = {cookie["name"]: cookie["value"] for cookie in cookies if cookie.get("name")}

    storage = page.evaluate(
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


def _build_launch_kwargs() -> dict:
    """v0.2.17:launch_persistent_context 的「拟人化」参数。"""
    return {
        "headless": False,
        "viewport": {
            "width": 940 + random.randint(-3, 3),
            "height": 650 + random.randint(-3, 3),
        },
        "args": [
            "--window-size=1000,720",
            "--window-position=-2000,-2000",
            *_STEALTH_LAUNCH_ARGS,
        ],
        "locale": _BROWSER_LOCALE,
        "timezone_id": _BROWSER_TIMEZONE,
        "extra_http_headers": {
            "Referer": "https://www.doubao.com/chat/",
            "Accept-Language": "zh-CN,zh;q=0.9",
        },
    }


def load_image_base64(path: Path) -> tuple[str, str, str]:
    data = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return path.name, mime, base64.b64encode(data).decode("ascii")


class PlaywrightVideoRunner:
    def __init__(self, timeout: float = 420, poll_interval: float = 10):
        self.timeout = timeout
        self.poll_interval = poll_interval

    def recheck_result(
        self,
        profile_dir: Path,
        conversation_id: str,
        update: Callable[..., None],
        cancel_event: threading.Event,
        *,
        deadline_seconds: float = 90,
    ) -> dict[str, str] | None:
        """v0.2.9:不重提交,只重解析 —— 复用已存的 conversation_id,
        重新打开 /chat/<id> 拉一次 CHAIN_SCRIPT,parse 出最新 result。

        用于 retry-result 端点:用户报告"succeeded 但 result_url 失效"
        或 "卡在 generating 很久不动了",想再查一次远端而不消耗豆包额度
        (不调 COMPLETION_SCRIPT,只查 chain)。

        返回 None = 还在生成中 / 远端还没出 result;
        返回 dict = parse_creation_result 出的 result 字段(result_url /
        backup_result_url / fallback_result_url / vid / cover_url 等),
        调用方负责写回 VideoTask。
        """
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                **_build_launch_kwargs(),
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                # 必须先打开 doubao.com 拿到 aegis 风控指纹,否则 CHAIN_SCRIPT
                # 会被字节拒为 1011(用户未登录)——和首次提交一样的硬约束。
                page.goto(
                    "https://www.doubao.com/chat/",
                    wait_until="domcontentloaded",
                    timeout=30_000,
                )
                page.wait_for_timeout(2_000)
                # 切到指定 conversation(只读,不发新请求)
                page.evaluate(
                    "id => history.replaceState({}, '', '/chat/' + id)",
                    conversation_id,
                )
                update(status="rechecking")
                deadline = time.monotonic() + deadline_seconds
                while time.monotonic() < deadline:
                    if cancel_event.is_set():
                        raise RuntimeError("任务已取消")
                    chain = page.evaluate(
                        CHAIN_SCRIPT, {"conversationId": conversation_id}
                    )
                    if chain["status"] != 200:
                        raise RuntimeError(
                            f"豆包结果接口返回 HTTP {chain['status']}"
                        )
                    result = parse_creation_result(chain["data"])
                    if result:
                        update(status="resolving", **result)
                        return self._resolve_original_download(page, result, cancel_event)
                    page.wait_for_timeout(self.poll_interval * 1000)
                return None
            finally:
                context.close()

    def run(
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
    ) -> dict[str, str]:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                **_build_launch_kwargs(),
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(2_000)
                # v0.2.17:抽完整 TokenBundle,透传给 payload.client_meta
                token_bundle = load_browser_context(page, context, pc_version=pc_version)
                fingerprint = token_bundle.device_id or token_bundle.web_id

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
                        uploaded = page.evaluate(
                            UPLOAD_IMAGE_SCRIPT,
                            {"name": name, "mime": mime, "base64Data": b64},
                        )
                        uploaded_images.append(uploaded)

                payload = build_completion_payload(
                    prompt,
                    model,
                    ratio,
                    duration,
                    fingerprint,
                    mode=mode,
                    images=uploaded_images or None,
                    # v0.2.17:WebMSSDK / TeaSDK 真实指纹(从登录 profile 抽)透传给
                    # payload.client_meta — 走 EXTRA_CLIENT_META_KEYS 白名单。
                    **token_bundle.to_client_meta(),
                )
                local_id = payload["client_meta"]["local_conversation_id"]
                page.evaluate("id => history.replaceState({}, '', '/chat/' + id)", local_id)
                response = page.evaluate(COMPLETION_SCRIPT, {"payload": payload})
                if response["status"] != 200:
                    raise RuntimeError(f"豆包提交接口返回 HTTP {response['status']}")
                ack = parse_sse_ack(response["text"])
                update(status="generating", **ack)

                deadline = time.monotonic() + self.timeout
                while time.monotonic() < deadline:
                    if cancel_event.is_set():
                        raise RuntimeError("任务已取消")
                    chain = page.evaluate(CHAIN_SCRIPT, {"conversationId": ack["conversation_id"]})
                    if chain["status"] != 200:
                        raise RuntimeError(f"豆包结果接口返回 HTTP {chain['status']}")
                    result = parse_creation_result(chain["data"])
                    if result:
                        update(status="resolving", **result)
                        return self._resolve_original_download(page, result, cancel_event)
                    page.wait_for_timeout(self.poll_interval * 1000)
                raise RuntimeError("视频生成超时")
            finally:
                context.close()

    def _resolve_original_download(self, page, result: dict[str, str], cancel_event: threading.Event) -> dict[str, str]:
        fallback = {**result, "result_url": result["fallback_result_url"]}
        homepage = page.evaluate(
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
            nodes = page.evaluate(
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
            page.wait_for_timeout(5_000)
        if not node_id:
            return fallback

        download = page.evaluate(
            AISPACE_SCRIPT,
            {
                "endpoint": "/samantha/aispace/get_download_info",
                "body": {"requests": [{"node_id": node_id}]},
            },
        )
        original = parse_download_info(download["data"]) if download["status"] == 200 else None
        return {**result, **(original or {"result_url": result["fallback_result_url"]})}
