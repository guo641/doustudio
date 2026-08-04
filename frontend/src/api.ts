// token 由后端 FastAPI 在渲染 index.html 时通过 <script> 注入到 window.__DOUPOOL_TOKEN__
// 不再放在 <meta> 里(那会写入磁盘 HTML,被分享/截图泄漏)
const token: string = (window as unknown as { __DOUPOOL_TOKEN__?: string }).__DOUPOOL_TOKEN__ || '';
const headers = { 'X-DouPool-Token': token, 'Content-Type': 'application/json' };

async function json(response: Response, message: string) {
  if (!response.ok) {
    let detail = message;
    try {
      detail = (await response.json()).detail || message;
    } catch {
      /* keep default */
    }
    throw new Error(detail);
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
export async function listVideoTasks() {
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
}) {
  return json(
    await fetch('/api/video-tasks', { method: 'POST', headers, body: JSON.stringify(body) }),
    '任务创建失败',
  );
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
