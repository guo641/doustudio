import { fireEvent, render, screen } from '@testing-library/vue';
import { expect, it, vi } from 'vitest';

vi.mock('../api', () => ({
  listAccounts: vi.fn().mockResolvedValue([]),
  listVideoTasks: vi.fn().mockResolvedValue([]),
  createVideoTask: vi.fn(),
  startLogin: vi.fn().mockResolvedValue({ id: 'attempt-1' }),
  loginEvents: vi.fn().mockReturnValue({ close: vi.fn() }),
}));

import App from '../App.vue';


it('opens Chromium without showing a duplicate login modal', async () => {
  render(App);
  await fireEvent.click(screen.getByRole('button', { name: '＋ 添加账号' }));

  expect(screen.queryByText('独立 Chromium 会话 · 登录信息仅保存在本机')).toBeNull();
  expect(screen.getByText('正在启动豆包登录窗口…')).toBeTruthy();
})
