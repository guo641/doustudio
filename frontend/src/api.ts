// token 由后端 FastAPI 在渲染 index.html 时通过 <script> 注入到 window.__DOUPOOL_TOKEN__
// 不再放在 <meta> 里(那会写入磁盘 HTML,被分享/截图泄漏)
const token: string = (window as unknown as { __DOUPOOL_TOKEN__?: string }).__DOUPOOL_TOKEN__ || '';
const headers = { 'X-DouPool-Token': token, 'Content-Type': 'application/json' };

// v0.2.21:listVideoTasks 返回类型 —— 用 inline structural 避免和 App.vue
// 同名 VideoTask 类型冲突(tsc 视为不相关类型)。
// 完整 VideoTask 类型在 App.vue 定义,本层只关心 status + id 用于差集比较。

async function json(response: Response, message: string) {
  if (!response.ok) {
    // v0.2.36: 错误信息不再只有「刷新 token 失败」这种干瘪文案,
    // 把 HTTP 状态码 + 请求 URL + 服务端响应体一起塞进去,这样用户能直接看到
    // 「刷新 token 失败(POST /api/.../refresh-tokens → 500): DatabaseError: ...」,
    // 不用再翻后端日志也能定位到根因(尤其是 Chromium Cookies 损坏、权限锁、
    // sqlite3 报错这种之前被笼统吞掉的情况)。
    let bodySnippet = '';
    try {
      const raw = await response.text();
      if (raw) {
        // 截前 240 字避免前端 toast 被几 MB 的 Python traceback 撑爆
        bodySnippet = raw.length > 240 ? raw.slice(0, 240) + '…' : raw;
      }
    } catch {
      /* 响应体读不到(网络中断 / 流被关闭)→ 留空,后面 'no body' 兜底 */
    }
    // FastAPI 标准错误格式 { "detail": "..." } 优先取 detail;非 JSON 时直接展示原文。
    let detail = '';
    if (bodySnippet) {
      try {
        const parsed = JSON.parse(bodySnippet);
        if (parsed && typeof parsed.detail === 'string') detail = parsed.detail;
      } catch {
        /* 不是 JSON,继续用 bodySnippet 兜底 */
      }
    }
    const tail = detail || bodySnippet || 'no body';
    throw new Error(`${message} (${response.status} ${response.url}): ${tail}`);
  }
  if (response.status === 204) return undefined;
  return response.json();
}

export async function listAccounts() {
  return json(await fetch('/api/accounts', { headers }), '账号加载失败');
}
export async function updateAccount(id: string, body: { enabled: boolean }) {
  return json(
    await fetch(`/api/accounts/${id}`, { method: 'PATCH', headers, body: JSON.stringify(body) }),
    '账号更新失败',
  );
}
export async function deleteAccount(id: string) {
  return json(await fetch(`/api/accounts/${id}`, { method: 'DELETE', headers }), '账号删除失败');
}
// v0.2.29:单账号 / 一键全部重置额度 —— 兜底用,防止跨日 cron 卡住。
export async function resetAccountQuota(id: string) {
  return json(
    await fetch(`/api/accounts/${id}/reset-quota`, { method: 'POST', headers }),
    '账号额度重置失败',
  );
}
export async function resetAllQuotas() {
  return json(
    await fetch('/api/accounts/reset-all-quota', { method: 'POST', headers }),
    '一键重置额度失败',
  );
}
export async function startLogin() {
  return json(await fetch('/api/accounts/login-attempts', { method: 'POST', headers }), '无法启动登录');
}
export function loginEvents(id: string, onEvent: (event: any) => void) {
  const source = new EventSource(
    `/api/login-attempts/${id}/events?access_token=${encodeURIComponent(token)}`,
  );
  // payload 损坏时不要让整条 SSE 流断掉 —— JSON.parse 抛异常会被 EventSource
  // 当成 onerror 处理,导致用户永远看不到后续的 succeeded / failed 事件。
  source.addEventListener('login_state', (e) => {
    try {
      const data = JSON.parse((e as MessageEvent).data);
      onEvent(data);
    } catch (err) {
      // eslint-disable-next-line no-console
      console.warn('[loginEvents] payload parse failed', err);
    }
  });
  // 断线 / 服务端关闭 / 网络中断:
  // EventSource readyState 0=connecting, 1=open, 2=closed。
  // 浏览器对 SSE 失败会无限重连,UI 会一直显示 busy —— 我们手动
  // 给调用方一个 failed 事件,让前端能退出 busy 态并允许重试。
  source.addEventListener('error', () => {
    if (source.readyState === EventSource.CLOSED) {
      onEvent({ state: 'failed', message: '登录事件流已断开,请重试' });
    } else if (!window.navigator.onLine) {
      onEvent({ state: 'failed', message: '网络已断开,请检查后重试' });
      source.close();
    }
    // readyState === CONNECTING 表示浏览器正在自动重连,先不动;
    // 下次 error 再触发时如果仍是 CONNECTING,通常意味着服务端长连接已死,
    // 此时 readyState 也会快速变 CLOSED,上面那条会接住。
  });
  return source;
}
export async function listVideoTasks(): Promise<Array<{
  id: string;
  account_name?: string;
  prompt: string;
  model: string;
  ratio: string;
  duration: number;
  mode?: string;
  image_count?: number;
  status: string;
  result_url?: string;
  backup_result_url?: string;
  fallback_result_url?: string;
  // v0.2.28:zhuceka 处理后的无水印视频;存在时优先给用户下载。
  clean_video_url?: string;
  clean_error?: string;
  cover_url?: string;
  error?: string;
  quota_used?: number;
  quota_total?: number;
  // v0.2.28:批量任务被后端打成同一 group_id,结果页按组折叠展示。
  group_id?: string;
  group_index?: number;
  group_name?: string;
  created_at: string;
}>> {
  return json(await fetch('/api/video-tasks', { headers }), '任务加载失败');
}
export async function createVideoTask(body: {
  prompt?: string;
  prompts?: string[];
  model: string;
  ratio: string;
  duration: number;
  account_id: string | null;
  mode?: 't2v' | 'i2v';
  images?: { name: string; data_base64: string }[];
  // v0.2.32:手动重试路径透传原 task.group_id,新任务仍归属同组。
  group_id?: string;
  group_name?: string;
}) {
  // v0.2.35:跨账号凑余额 —— 200 OK + {task, partial_rejected} 包装。
  // 解析返 {task, partial_rejected},App.vue 据此 Toast 提示哪几条 prompt
  // 当前无可用账号、稍后会被自动重试。
  return (await json(
    await fetch('/api/video-tasks', { method: 'POST', headers, body: JSON.stringify(body) }),
    '任务创建失败',
  )) as { task: Record<string, unknown>; partial_rejected: Array<{ index: number; prompt: string; reason: string }> };
}

// v0.2.11:删除一条视频任务(running 状态服务端会 409)。
export async function deleteVideoTask(taskId: string) {
  return json(
    await fetch(`/api/requests/${taskId}`, { method: 'DELETE', headers }),
    '任务删除失败',
  );
}

export async function listVideoTaskGroups(limit = 50) {
  return json(
    await fetch(`/api/video-task-groups?limit=${limit}`, { headers }),
    '任务组加载失败',
  );
}

export async function listVideoTaskGroupDetail(groupId: string) {
  return json(
    await fetch(`/api/video-task-groups/${encodeURIComponent(groupId)}`, { headers }),
    '任务组详情加载失败',
  );
}

export async function checkUpdate() {
  return json(await fetch('/api/update-check', { headers }), '更新检查失败');
}
export async function listLogs() {
  return json(await fetch('/api/logs', { headers }), '日志加载失败');
}
export async function clearLogs() {
  return json(await fetch('/api/logs', { method: 'DELETE', headers }), '日志清空失败');
}
export async function getSettings() {
  return json(await fetch('/api/settings', { headers }), '设置加载失败');
}
export async function saveSettings(body: Record<string, unknown>) {
  return json(
    await fetch('/api/settings', { method: 'PUT', headers, body: JSON.stringify(body) }),
    '设置保存失败',
  );
}

// v0.3.5.13-B:让桌面端通过后端弹出系统原生目录选择器。
// 这里只返回用户选择的路径,不直接保存 download_dir;用户仍需点击「保存设置」。
export type PickDownloadDirResponse = { path: string | null };

export async function pickDownloadDir(startDir = ''): Promise<PickDownloadDirResponse> {
  return json(
    await fetch('/api/settings/pick-download-dir', {
      method: 'POST',
      headers,
      body: JSON.stringify({ start_dir: startDir }),
    }),
    '选择下载目录失败',
  );
}

// v0.3.5.13-B:在系统文件管理器中打开当前下载目录(体验增强,不修改设置)。
export type OpenDownloadDirResponse = { ok: boolean; message?: string };

export async function openDownloadDir(path: string): Promise<OpenDownloadDirResponse> {
  return json(
    await fetch('/api/settings/open-dir', {
      method: 'POST',
      headers,
      body: JSON.stringify({ path }),
    }),
    '打开下载目录失败',
  );
}

export async function backupDatabase() {
  return json(await fetch('/api/settings/backup', { method: 'POST', headers }), '数据库备份失败');
}

// v0.2.17:WebMSSDK / TeaSDK token 状态 + 手动刷新。
// 返回的 bundle 字段详情见 api/app.py:_token_bundle_dict。
export type WebMSSDKTokensResponse = {
  available: boolean;
  hint: string;
  ms_token_preview: string;
  web_id: string;
  web_id_signature: string;
  device_id: string;
  tea_uuid: string;
  pc_version: string;
  fetched_at: number;
  age_seconds: number | null;
};

export async function getWebMSSDKTokens(accountId: string): Promise<WebMSSDKTokensResponse> {
  return json(
    await fetch(`/api/accounts/${accountId}/webmssdk-tokens`, { headers }),
    'token 状态加载失败',
  );
}

export async function refreshWebMSSDKTokens(accountId: string): Promise<WebMSSDKTokensResponse> {
  return json(
    await fetch(`/api/accounts/${accountId}/refresh-tokens`, { method: 'POST', headers }),
    '刷新 token 失败',
  );
}

// v0.2.37.2:「重新导出 cookies」按钮 —— 让 Playwright 用账号 profile 重新打开
// 浏览器,8 秒后把当前 doubao.com cookie 明文写到 profile_dir/cookies.json。
// 跟 refresh-tokens(也写 cookies.json)区别:
// - refresh-tokens 强调「让 WebMSSDK 跑一遍拿到最新 msToken」,返回完整 bundle。
// - re-export-cookies 强调「让 cookies.json 重新可读」,返回更简单 ok/hint。
export async function reExportCookies(
  accountId: string,
): Promise<{ ok: boolean; saved: boolean; elapsed: number; hint: string }> {
  return json(
    await fetch(`/api/accounts/${accountId}/re-export-cookies`, {
      method: 'POST',
      headers,
    }),
    '重新导出 cookies 失败',
  );
}

// v0.2.20:「📂 打开浏览器」按钮 —— 复用账号已有 login profile
// 拉起 Chromium 窗口。同 profile_dir 已有窗口时服务端返回 409。
export async function openAccountBrowser(accountId: string): Promise<{ ok: boolean; message: string }> {
  return json(
    await fetch(`/api/accounts/${accountId}/open-browser`, { method: 'POST', headers }),
    '打开浏览器失败',
  );
}

export async function closeAccountBrowser(
  accountId: string,
): Promise<{ ok: boolean; cancel_sent: boolean; message: string }> {
  return json(
    await fetch(`/api/accounts/${accountId}/close-browser`, { method: 'POST', headers }),
    '关闭浏览器失败',
  );
}

export async function getAccountBrowserStatus(accountId: string): Promise<{ open: boolean }> {
  return json(
    await fetch(`/api/accounts/${accountId}/browser-status`, { headers }),
    '浏览器状态加载失败',
  );
}

// v0.2.22 Q4:DownloadButton 下载失败时调用,同步拿新签名 URL。
// 后端调 runner.recheck_result(deadline=60s),不消耗 quota,只刷
// task.result_url / backup_result_url / fallback_result_url 三个字段。
export async function refreshResultUrl(taskId: string): Promise<{
  id: string;
  status?: string;
  result_url?: string;
  backup_result_url?: string;
  fallback_result_url?: string;
  cover_url?: string;
  error?: string;
}> {
  return json(
    await fetch(`/api/results/${taskId}/refresh-url`, { method: 'POST', headers }),
    '刷新下载链接失败',
  );
}

// v0.2.28 Q2:结果页点「保存到下载目录」时调用,后端把 group_id 下所有
// succeeded 任务视频流式写到 settings.download_dir/<batch_folder>/,
// 返回落盘路径 + 文件数。同步等待(N 通常 3-5,每个 5-30s),后端设了
// 30s/视频 超时,整组最坏 ~150s;前端按钮要 disable + 显示进度。
export async function groupDownload(groupId: string): Promise<{
  saved_dir: string;
  file_count: number;
}> {
  return json(
    await fetch('/api/results/group-download', {
      method: 'POST',
      headers,
      body: JSON.stringify({ group_id: groupId }),
    }),
    '保存批量视频失败',
  );
}

// v0.2.35:一键清除任务 + 一键清除结果。
// target 端点:
//   - "completed"  清 succeeded / failed / cancelled(预扣过额度的会在删前退)
//   - "queued"     只清 queued(running 状态绝不动)
// downloaded_only 在「清除结果」端点区分已下载 vs 全部。
export async function clearVideoTasks(target: 'completed' | 'queued'): Promise<{ deleted_count: number }> {
  const path = target === 'completed'
    ? '/api/video-tasks/clear-completed'
    : '/api/video-tasks/clear-queued';
  return json(
    await fetch(path, { method: 'POST', headers }),
    '清除任务失败',
  );
}
export async function clearResults(downloaded_only: boolean): Promise<{ deleted_count: number }> {
  const path = downloaded_only
    ? '/api/results/clear-downloaded'
    : '/api/results/clear-all';
  return json(
    await fetch(path, { method: 'POST', headers }),
    '清除结果失败',
  );
}

export function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onerror = () => reject(new Error('读取图片失败'));
    reader.onload = () => {
      const result = String(reader.result || '');
      const comma = result.indexOf(',');
      resolve(comma >= 0 ? result.slice(comma + 1) : result);
    };
    reader.readAsDataURL(file);
  });
}

// v0.3.0:离线激活闸门 —— 三个端点都不带 X-DouPool-Token(未激活时也要能调)。
//
// /api/license/status 和 /api/license/activate 后端刻意放在
// authorize_with_license 之外;前端在未激活态也能正常调用。
//
// /api/license/quit 后端走 os._exit(0) 强杀进程 —— webview 随之关。
// 前端 fire-and-forget:fetch 不一定能拿到响应。
export type LicenseStatus = {
  status: 'valid' | 'expired' | 'missing' | 'uncompiled';
  fingerprint: string;
  customer: string;
  expires_at: number | null;
};

// v0.3.0:前端激活闸门状态 —— App.vue 用它决定渲染 ActivationDialog 还是主 UI。
// 'loading':刚启动,等 /api/license/status 返回
// 'valid':已激活,渲染主 UI
// 'needs-activation':未激活,渲染激活窗 (state=needs-activation 输入框可用)
// 'expired':已过期,渲染激活窗 (state=expired 输入框禁用)
export type LicenseState = 'loading' | 'valid' | 'needs-activation' | 'expired';

const licenseHeaders = { 'Content-Type': 'application/json' };

export async function getLicenseStatus(): Promise<LicenseStatus> {
  return json(
    await fetch('/api/license/status', { headers: licenseHeaders }),
    '激活状态加载失败',
  );
}

export async function activateLicense(code: string): Promise<{ ok: true }> {
  return json(
    await fetch('/api/license/activate', {
      method: 'POST',
      headers: licenseHeaders,
      body: JSON.stringify({ code }),
    }),
    '激活失败',
  );
}

export async function quitApp(): Promise<void> {
  try {
    await fetch('/api/license/quit', { method: 'POST', headers: licenseHeaders });
  } catch {
    // 后端立刻 os._exit,fetch 不一定拿得到响应 —— 静默吞掉
  }
}
