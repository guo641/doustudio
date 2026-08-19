import { fireEvent, render, screen } from '@testing-library/vue';
import { expect, it, vi } from 'vitest';

vi.mock('../api', () => ({
  listAccounts: vi.fn().mockResolvedValue([]),
  listVideoTasks: vi.fn().mockResolvedValue([]),
  createVideoTask: vi.fn(),
  startLogin: vi.fn().mockResolvedValue({ id: 'attempt-1' }),
  loginEvents: vi.fn().mockReturnValue({ close: vi.fn() }),
  // v0.3.0:激活闸门 —— App.onMounted 第一件事就是查 status,不 mock 会卡 loading,
  // 然后整页一直停在全屏激活窗,看不到 sidebar 也点不到按钮。
  getLicenseStatus: vi.fn().mockResolvedValue({
    status: 'valid',
    fingerprint: 'a'.repeat(64),
    customer: '测试',
    expires_at: Math.floor(Date.now() / 1000) + 86400,
  }),
  getSettings: vi.fn().mockResolvedValue({}),
}));

import App from '../App.vue';


it('opens Chromium without showing a duplicate login modal', async () => {
  render(App);
  // v0.3.0:激活闸门先于 sidebar 渲染,findByRole 等闸门切到 'valid' + sidebar 出现。
  await fireEvent.click(await screen.findByRole('button', { name: '＋ 添加账号' }));

  expect(screen.queryByText('独立 Chromium 会话 · 登录信息仅保存在本机')).toBeNull();
  expect(screen.getByText('正在启动豆包登录窗口…')).toBeTruthy();
})
