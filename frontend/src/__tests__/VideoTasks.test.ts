import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/vue';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';

const { createVideoTask, listVideoTasks } = vi.hoisted(() => ({
  createVideoTask: vi.fn().mockResolvedValue({ id: 'task-1' }),
  listVideoTasks: vi.fn().mockResolvedValue([]),
}));
vi.mock('../api', () => ({
  listAccounts: vi.fn().mockResolvedValue([
    { id: 'account-1', display_name: '测试账号', status: 'active', enabled: true },
  ]),
  listVideoTasks,
  createVideoTask,
  // 模拟老设置库仍返回 5 秒；App 必须忽略并固定提交 10 秒。
  getSettings: vi.fn().mockResolvedValue({
    default_model: 'seedance_v2.0_mini',
    default_ratio: '1:1',
    default_duration: 5,
  }),
  startLogin: vi.fn(),
  loginEvents: vi.fn(),
  // v0.3.0:激活闸门 mock —— 不写的话 App.onMounted 卡在 loading,sidebar 永不出现。
  getLicenseStatus: vi.fn().mockResolvedValue({
    status: 'valid',
    fingerprint: 'a'.repeat(64),
    customer: '测试',
    expires_at: Math.floor(Date.now() / 1000) + 86400,
  }),
}));

import App from '../App.vue';

beforeEach(() => {
  createVideoTask.mockClear();
  listVideoTasks.mockReset().mockResolvedValue([]);
});
afterEach(cleanup);

it('creates a text-to-video task from the video task page', async () => {
  render(App);
  // v0.3.0:激活闸门先于 sidebar 渲染 → 用 findByText 等闸门切到 'valid' 后再点。
  await fireEvent.click(await screen.findByText(/视频任务/));
  await fireEvent.click(screen.getByRole('button', { name: '＋ 添加任务' }));
  expect(screen.getByRole('dialog', { name: '添加视频任务' })).toBeTruthy();
  expect(screen.queryByLabelText('图片')).toBeNull();
  const durationInput = screen.getByLabelText('时长') as HTMLInputElement;
  expect(durationInput.value).toBe('10');
  expect(durationInput.disabled).toBe(true);
  await fireEvent.update(screen.getByLabelText('画面描述'), '一只猫在草地上行走');
  await fireEvent.click(screen.getByRole('button', { name: '添加文生任务' }));

  await waitFor(() => expect(createVideoTask).toHaveBeenCalledWith(expect.objectContaining({
    prompt: '一只猫在草地上行走',
    model: 'seedance_v2.0_mini',
    duration: 10,
    account_id: null,
    mode: 't2v',
    images: [],
  })));
});

it('retries a historical text task with its model and ratio but fixed 10 second duration', async () => {
  listVideoTasks.mockResolvedValue([{
    id: 'failed-1', account_name: '莲韵', prompt: '小狗跳舞',
    model: 'seedance_v2.0_mini', ratio: '9:16', duration: 5,
    status: 'failed', error: 'rate limited', created_at: '2026-07-13T12:06:23',
  }]);

  render(App);
  // 同上:findByText 等闸门切到 'valid' + sidebar 渲染完。
  await fireEvent.click(await screen.findByText(/视频任务/));
  await screen.findByText('小狗跳舞');
  await fireEvent.click(screen.getByRole('button', { name: '重试任务 failed-1' }));

  await waitFor(() => expect(createVideoTask).toHaveBeenCalledWith({
    prompt: '小狗跳舞', model: 'seedance_v2.0_mini', ratio: '9:16',
    duration: 10, account_id: null, mode: 't2v', images: [],
  }));
});
