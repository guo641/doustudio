// v0.3.0:激活闸门测试 —— 后端 /api/license/status 返 missing/expired 时主 UI 不可见,
// 只有「激活码」按钮存在。
//
// cleanup() 是关键:多个 render() 不清理 DOM 会让前一个 App 实例的 sidebar / workspace
// 残留,导致后续测试 queryByRole('button', {name: '账号池'}) 命中前一个组件的节点。
// @testing-library/vue v8 不再自动注入 cleanup —— 显式调。
import { cleanup, render, screen, waitFor } from '@testing-library/vue';
import { afterEach, expect, it, vi } from 'vitest';
import App from '../App.vue';

function mockJsonFetch(payload: unknown) {
  return vi.fn().mockResolvedValue({
    ok: true,
    status: 200,
    text: async () => JSON.stringify(payload),
    json: async () => payload,
  });
}

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

it('未激活时侧栏隐藏,只显激活窗', async () => {
  // /api/license/status 返 missing → 主 UI 不渲染,激活窗渲染
  vi.stubGlobal(
    'fetch',
    mockJsonFetch({
      status: 'missing',
      fingerprint: 'abcd1234abcd1234abcd1234abcd1234',
      customer: '',
      expires_at: null,
    }),
  );

  render(App);

  // 等待 onMounted → refreshLicense → licenseState 切到 needs-activation
  await waitFor(() => {
    expect(screen.queryByRole('button', { name: '激活' })).toBeTruthy();
  });

  // 侧栏按钮不再渲染
  expect(screen.queryByRole('button', { name: '账号池' })).toBeNull();
  expect(screen.queryByRole('button', { name: '视频任务' })).toBeNull();
  expect(screen.queryByRole('button', { name: '生成结果' })).toBeNull();
  expect(screen.queryByRole('button', { name: '运行日志' })).toBeNull();
  expect(screen.queryByRole('button', { name: '设置' })).toBeNull();
});

it('激活有效时正常渲染主 UI,无激活窗', async () => {
  // status=valid → licenseState='valid' → 渲染主 UI,不渲染激活窗
  // 后续 /api/accounts 等请求也会被发,我们只 mock 第一个,然后其他全返 []
  const accountsPayload: unknown[] = [];
  const fetchMock = vi.fn().mockImplementation((url: string) => {
    if (url.includes('/api/license/status')) {
      return Promise.resolve({
        ok: true,
        status: 200,
        text: async () => JSON.stringify({
          status: 'valid',
          fingerprint: 'abcd1234abcd1234abcd1234abcd1234',
          customer: 'test',
          expires_at: Math.floor(Date.now() / 1000) + 86400,
        }),
        json: async () => ({
          status: 'valid',
          fingerprint: 'abcd1234abcd1234abcd1234abcd1234',
          customer: 'test',
          expires_at: Math.floor(Date.now() / 1000) + 86400,
        }),
      });
    }
    return Promise.resolve({
      ok: true,
      status: 200,
      text: async () => JSON.stringify(accountsPayload),
      json: async () => accountsPayload,
    });
  });
  vi.stubGlobal('fetch', fetchMock);

  render(App);

  await waitFor(() => {
    expect(screen.getByRole('button', { name: '账号池' })).toBeTruthy();
  });

  // 激活窗的「激活」按钮不应该出现
  expect(screen.queryByRole('button', { name: '激活' })).toBeNull();
});

it('过期时输入框禁用 + 显退出按钮', async () => {
  vi.stubGlobal(
    'fetch',
    mockJsonFetch({
      status: 'expired',
      fingerprint: 'abcd1234abcd1234abcd1234abcd1234',
      customer: '测试用户',
      expires_at: Math.floor(Date.now() / 1000) - 60,
    }),
  );

  render(App);

  await waitFor(() => {
    // 过期态显示「退出软件」+「复制机器码」按钮
    expect(screen.getByRole('button', { name: '退出软件' })).toBeTruthy();
  });

  expect(screen.queryByRole('button', { name: '账号池' })).toBeNull();
});