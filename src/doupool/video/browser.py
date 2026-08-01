from __future__ import annotations

import base64
import mimetypes
import threading
import time
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import sync_playwright

from .protocol import (
    build_completion_payload,
    find_creation_directory,
    find_video_node,
    parse_creation_result,
    parse_download_info,
    parse_sse_ack,
)


PC_VERSION = "3.27.4"

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


def load_image_base64(path: Path) -> tuple[str, str, str]:
    data = path.read_bytes()
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return path.name, mime, base64.b64encode(data).decode("ascii")


class PlaywrightVideoRunner:
    def __init__(self, timeout: float = 420, poll_interval: float = 10):
        self.timeout = timeout
        self.poll_interval = poll_interval

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
    ) -> dict[str, str]:
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir),
                headless=False,
                viewport={"width": 940, "height": 650},
                args=["--window-size=1000,720", "--window-position=-2000,-2000"],
            )
            try:
                page = context.pages[0] if context.pages else context.new_page()
                page.goto("https://www.doubao.com/chat/", wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_timeout(2_000)
                fingerprint = read_browser_fingerprint(page, context)

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
