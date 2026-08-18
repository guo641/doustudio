import { fireEvent, render, screen, within } from '@testing-library/vue';
import { expect, it } from 'vitest';
import VideoTaskTable from '../components/VideoTaskTable.vue';

const tasks = [
  {
    id: 'failed-1', account_name: '莲韵', prompt: '一段很长的小狗跳舞画面描述',
    model: 'seedance_v2.0_mini', ratio: '9:16', duration: 5, status: 'failed',
    error: 'rate limited', created_at: '2026-07-13T12:06:23',
  },
  {
    id: 'done-1', account_name: '账号 02', prompt: '橘猫窗边看雨',
    model: 'seedance_v2.0_std', ratio: '16:9', duration: 5, status: 'succeeded',
    result_url: 'https://example.test/video.mp4', quota_used: 2, quota_total: 5,
    created_at: '2026-07-13T11:12:26',
  },
  {
    id: 'failed-i2v', account_name: '旧账号', prompt: '历史图生任务',
    model: 'seedance_v2.0_mini', ratio: '1:1', duration: 5, mode: 'i2v',
    image_count: 1, status: 'failed', error: '历史任务失败',
    created_at: '2026-07-13T10:00:00',
  },
];

it('renders and filters the video task table', async () => {
  const view = render(VideoTaskTable, { props: { tasks } });
  for (const name of ['状态', '任务', '账号', '时间', '操作']) {
    expect(screen.getByRole('columnheader', { name })).toBeTruthy();
  }
  expect(screen.queryByRole('columnheader', { name: '额度' })).toBeNull();
  expect(screen.queryByRole('columnheader', { name: '模型参数' })).toBeNull();
  // v0.2.11:std 改名为 Seedance Fast
  expect(screen.getByText('Seedance Fast · 5s · 16:9')).toBeTruthy();
  // v0.2.28:DownloadButton 渲染为 <button>,不是 <a>;「预览」链接保持 <a> 不变。
  expect(screen.getByRole('button', { name: /下载/ })).toBeTruthy();
  expect(screen.getByRole('link', { name: /预览/ })).toBeTruthy();

  await fireEvent.update(screen.getByLabelText('搜索任务'), '橘猫');
  expect(screen.queryByRole('cell', { name: '莲韵' })).toBeNull();
  await fireEvent.update(screen.getByLabelText('状态筛选'), 'succeeded');
  await fireEvent.update(screen.getByLabelText('账号筛选'), '账号 02');
  expect(screen.getByText('橘猫窗边看雨')).toBeTruthy();

  await fireEvent.update(screen.getByLabelText('搜索任务'), '');
  await fireEvent.update(screen.getByLabelText('状态筛选'), '');
  await fireEvent.update(screen.getByLabelText('账号筛选'), '');
  await fireEvent.click(screen.getByRole('button', { name: '展开任务 failed-1' }));
  expect(screen.getByText('rate limited')).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name: '重试任务 failed-1' }));
  expect(view.emitted('retry')[0]).toEqual([tasks[0]]);
  expect(screen.queryByRole('button', { name: '重试任务 failed-i2v' })).toBeNull();
  expect(screen.getByText('历史图生任务')).toBeTruthy();
});

it('partitions visible tasks by group and keeps group order after filtering', async () => {
  const groupedTasks = [
    {
      id: 'b-2', account_name: '账号 B', prompt: 'B-第二段',
      model: 'seedance_v2.0_mini', ratio: '16:9', duration: 10, status: 'queued',
      group_id: 'group-b', group_index: 2, created_at: '2026-07-13T11:01:00',
    },
    {
      id: 'a-2', account_name: '账号 A', prompt: 'A-第二段',
      model: 'seedance_v2.0_mini', ratio: '1:1', duration: 10, status: 'succeeded',
      group_id: 'group-a', group_index: 2, created_at: '2026-07-13T10:01:00',
    },
    {
      id: 'b-1', account_name: '账号 B', prompt: 'B-第一段',
      model: 'seedance_v2.0_mini', ratio: '16:9', duration: 10, status: 'queued',
      group_id: 'group-b', group_index: 1, created_at: '2026-07-13T11:00:00',
    },
    {
      id: 'a-1', account_name: '账号 A', prompt: 'A-第一段',
      model: 'seedance_v2.0_mini', ratio: '1:1', duration: 10, status: 'succeeded',
      group_id: 'group-a', group_index: 1, created_at: '2026-07-13T10:00:00',
    },
    {
      id: 'legacy-1', account_name: '旧账号', prompt: '未分组任务',
      model: 'seedance_v2.0_mini', ratio: '9:16', duration: 10, status: 'failed',
      created_at: '2026-07-13T09:00:00',
    },
  ];

  const view = render(VideoTaskTable, { props: { tasks: groupedTasks } });
  const scoped = within(view.container as HTMLElement);
  const headers = [...view.container.querySelectorAll('tr.group-header .group-title > span:first-of-type')].map(
    (node) => node.textContent?.replace(/\s+/g, ' ').trim(),
  );
  expect(headers).toEqual([
    '组 #1 · 2 个任务',
    '组 #2 · 2 个任务',
    '未分组 · 1 个任务',
  ]);

  const prompts = [...view.container.querySelectorAll('tbody tr:not(.group-header):not(.detail-row) .prompt-btn')]
    .map((node) => node.textContent?.trim());
  expect(prompts).toEqual(['A-第一段', 'A-第二段', 'B-第一段', 'B-第二段', '未分组任务']);

  const searchInput = view.container.querySelector('input[aria-label="搜索任务"]') as HTMLInputElement;
  await fireEvent.update(searchInput, 'B-');
  const filteredHeaders = [...view.container.querySelectorAll('tr.group-header .group-title > span:first-of-type')].map(
    (node) => node.textContent?.replace(/\s+/g, ' ').trim(),
  );
  expect(filteredHeaders).toEqual(['组 #1 · 2 个任务']);
  expect(scoped.queryByText('A-第一段')).toBeNull();
  expect(scoped.queryByText('未分组任务')).toBeNull();
  expect(scoped.getByText('B-第一段')).toBeTruthy();
  expect(scoped.getByText('B-第二段')).toBeTruthy();
});

it('uses the group name as the header and toggles only that group', async () => {
  const groupedTasks = [
    {
      id: 'snake-1', account_name: '账号 A', prompt: '蛇的第一段',
      model: 'seedance_v2.0_mini', ratio: '9:16', duration: 10, status: 'queued',
      group_id: 'snake-group', group_index: 1, group_name: '美女蛇', created_at: '2026-07-13T10:00:00',
    },
    {
      id: 'snake-2', account_name: '账号 A', prompt: '蛇的第二段',
      model: 'seedance_v2.0_mini', ratio: '9:16', duration: 10, status: 'queued',
      group_id: 'snake-group', group_index: 2, group_name: '美女蛇', created_at: '2026-07-13T10:01:00',
    },
  ];

  const view = render(VideoTaskTable, { props: { tasks: groupedTasks } });
  const groupHeader = view.container.querySelector('tr.group-header') as HTMLTableRowElement;
  expect(groupHeader.textContent).toContain('美女蛇 · 2 个任务');
  expect(groupHeader.getAttribute('aria-expanded')).toBe('true');
  expect(screen.getByText('蛇的第一段')).toBeTruthy();

  await fireEvent.click(groupHeader);
  expect(groupHeader.getAttribute('aria-expanded')).toBe('false');
  expect(screen.queryByText('蛇的第一段')).toBeNull();
  expect(screen.queryByText('蛇的第二段')).toBeNull();

  await fireEvent.click(groupHeader);
  expect(groupHeader.getAttribute('aria-expanded')).toBe('true');
  expect(screen.getByText('蛇的第一段')).toBeTruthy();
});
