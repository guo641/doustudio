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
  startLogin: vi.fn(),
  loginEvents: vi.fn(),
}));

import App from '../App.vue';

beforeEach(() => {
  createVideoTask.mockClear();
  listVideoTasks.mockReset().mockResolvedValue([]);
});
afterEach(cleanup);

it('creates a text-to-video task from the video task page', async () => {
  render(App);
  await fireEvent.click(screen.getByText(/视频任务/));
  await fireEvent.click(screen.getByRole('button', { name: '＋ 添加任务' }));
  expect(screen.getByRole('dialog', { name: '添加视频任务' })).toBeTruthy();
  await fireEvent.update(screen.getByLabelText('画面描述'), '一只猫在草地上行走');
  await fireEvent.click(screen.getByRole('button', { name: '添加文生任务' }));

  await waitFor(() => expect(createVideoTask).toHaveBeenCalledWith(expect.objectContaining({
    prompt: '一只猫在草地上行走',
    model: 'seedance_v2.0_mini',
    duration: 5,
    account_id: null,
    mode: 't2v',
  })));
});

it('retries a failed task with its original parameters', async () => {
  listVideoTasks.mockResolvedValue([{
    id: 'failed-1', account_name: '莲韵', prompt: '小狗跳舞',
    model: 'seedance_v2.0_mini', ratio: '9:16', duration: 5,
    status: 'failed', error: 'rate limited', created_at: '2026-07-13T12:06:23',
  }]);

  render(App);
  await fireEvent.click(screen.getByText(/视频任务/));
  await screen.findByText('小狗跳舞');
  await fireEvent.click(screen.getByRole('button', { name: '重试任务 failed-1' }));

  await waitFor(() => expect(createVideoTask).toHaveBeenCalledWith({
    prompt: '小狗跳舞', model: 'seedance_v2.0_mini', ratio: '9:16',
    duration: 5, account_id: null, mode: 't2v', images: [],
  }));
});
