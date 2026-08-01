const token = document.querySelector<HTMLMetaElement>('meta[name="doupool-token"]')?.content || '';
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
  source.addEventListener('login_state', (e) => onEvent(JSON.parse((e as MessageEvent).data)));
  return source;
}
export async function listVideoTasks() {
  return json(await fetch('/api/video-tasks', { headers }), '任务加载失败');
}
export async function createVideoTask(body: {
  prompt: string;
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
