import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/vue';
import { afterEach, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  listLogs: vi.fn().mockResolvedValue([{ id:1, level:'ERROR', module:'doupool.video', event:'failed', message:'rate limited', created_at:'2026-07-13T12:00:00' }]),
  clearLogs: vi.fn().mockResolvedValue(undefined),
  getSettings: vi.fn().mockResolvedValue({ max_concurrency:1, daily_quota:5, quota_reset_time:'00:00', scheduler_strategy:'least_used', default_model:'seedance_v2.0_mini', default_duration:5, default_ratio:'1:1', download_dir:'/tmp/downloads', log_level:'INFO', log_retention_days:30, data_dir:'/tmp/data' }),
  saveSettings: vi.fn().mockImplementation(async value=>value),
  backupDatabase: vi.fn().mockResolvedValue({ path:'/tmp/backup.sqlite3' }),
  // v0.2.17:AccountTable 在 onMounted/watch 时调这两个,默认给 "available=true" 兜底
  // —— 老测试不关心 token 字段,但 mock 缺失会让组件初始 render 失败。
  getWebMSSDKTokens: vi.fn().mockResolvedValue({
    available: true, hint: '', ms_token_preview: '', web_id: 'wb', web_id_signature: '',
    device_id: 'dev', tea_uuid: 'tu', pc_version: '3.27.4', fetched_at: 0, age_seconds: 0,
  }),
  refreshWebMSSDKTokens: vi.fn().mockResolvedValue({
    available: true, hint: 'mock', ms_token_preview: '', web_id: 'wb', web_id_signature: '',
    device_id: 'dev', tea_uuid: 'tu', pc_version: '3.27.4', fetched_at: 0, age_seconds: 0,
  }),
  // v0.2.28:Q2 批量下载 —— ResultsTable 点「保存到下载目录」时调。
  groupDownload: vi.fn().mockResolvedValue({ saved_dir: '/tmp/downloads/abcdef12_143022', file_count: 3 }),
}));
vi.mock('../api', () => api);

import AccountTable from '../components/AccountTable.vue';
import LogsPage from '../components/LogsPage.vue';
import ResultsTable from '../components/ResultsTable.vue';
import SettingsPage from '../components/SettingsPage.vue';

afterEach(cleanup);

it('emits account toggle and confirmed delete', async () => {
  vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
  const view = render(AccountTable, { props:{ accounts:[{ id:'a1', display_name:'莲韵', status:'active', enabled:true, video_quota_used:3, video_quota_total:5 }] } });
  expect(screen.getByText('3/5')).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name:'停用 莲韵' }));
  await fireEvent.click(screen.getByRole('button', { name:'删除 莲韵' }));
  expect(view.emitted('toggle')[0]).toEqual([{ id:'a1', enabled:false }]);
  expect(view.emitted('delete')[0]).toEqual(['a1']);
});

it('shows successful results and download actions', async () => {
  render(ResultsTable, { props:{ tasks:[{ id:'t1', prompt:'橘猫看雨', model:'seedance_v2.0_mini', ratio:'16:9', duration:5, account_name:'莲韵', status:'succeeded', result_url:'https://example.test/video.mp4', created_at:'2026-07-13T12:00:00' }] } });
  expect(screen.getByText('橘猫看雨')).toBeTruthy();
  // 跨域下载改走 DownloadButton(fetch + Blob),不再用 <a download href>。
  // 这里只验证 button 存在;实际下载路径由 DownloadButton 自身保证。
  const downloadBtn = await screen.findByRole('button', { name: /下载视频|下载无水印/ });
  expect(downloadBtn).toBeTruthy();
});

// v0.2.28 Q2:有 group_id 的批量任务在结果页折叠为组,提供「下载全部」
// 和「保存到下载目录」两个按钮;无 group_id 的老任务保持扁平展示。
it('groups batched tasks with collapsible sections and per-group download buttons', async () => {
  vi.stubGlobal('alert', vi.fn());
  const tasks = [
    // 同一组(3 段 prompt 一次提交),按 group_index 升序排
    { id:'t1', prompt:'第一段', model:'seedance_v2.0_mini', ratio:'1:1', duration:5, status:'succeeded',
      result_url:'https://example.test/v1.mp4', group_id:'abcdef12-group', group_index:1, created_at:'2026-07-13T12:00:00' },
    { id:'t2', prompt:'第二段', model:'seedance_v2.0_mini', ratio:'1:1', duration:5, status:'succeeded',
      result_url:'https://example.test/v2.mp4', group_id:'abcdef12-group', group_index:2, created_at:'2026-07-13T12:01:00' },
    { id:'t3', prompt:'第三段', model:'seedance_v2.0_mini', ratio:'1:1', duration:5, status:'succeeded',
      result_url:'https://example.test/v3.mp4', group_id:'abcdef12-group', group_index:3, created_at:'2026-07-13T12:02:00' },
    // 老任务,无 group_id,保持扁平
    { id:'t0', prompt:'老任务', model:'seedance_v2.0_mini', ratio:'1:1', duration:5, status:'succeeded',
      result_url:'https://example.test/v0.mp4', created_at:'2026-07-13T11:00:00' },
  ];
  render(ResultsTable, { props: { tasks } });
  // 组头存在(group_id 前 8 位 + 任务数)
  expect(screen.getByText(/组 #abcdef12 · 3 个视频/)).toBeTruthy();
  // 两个组级按钮都在
  expect(screen.getByRole('button', { name: '下载全部' })).toBeTruthy();
  expect(screen.getByRole('button', { name: '保存到下载目录' })).toBeTruthy();
  // 4 个任务描述都渲染(组内 3 + 扁平 1)
  expect(screen.getByText('第一段')).toBeTruthy();
  expect(screen.getByText('第二段')).toBeTruthy();
  expect(screen.getByText('第三段')).toBeTruthy();
  expect(screen.getByText('老任务')).toBeTruthy();
  // 点「保存到下载目录」调 groupDownload,并 alert 出 saved_dir
  await fireEvent.click(screen.getByRole('button', { name: '保存到下载目录' }));
  await waitFor(() => expect(api.groupDownload).toHaveBeenCalledWith('abcdef12-group'));
  expect(window.alert).toHaveBeenCalledWith(expect.stringContaining('/tmp/downloads/abcdef12_143022'));
});

it('filters and clears logs', async () => {
  vi.stubGlobal('confirm', vi.fn().mockReturnValue(true));
  render(LogsPage);
  await screen.findByText('rate limited');
  await fireEvent.update(screen.getByLabelText('日志级别'), 'ERROR');
  await fireEvent.click(screen.getByRole('button', { name:'清空日志' }));
  expect(api.clearLogs).toHaveBeenCalled();
});

it('saves settings and creates a backup', async () => {
  render(SettingsPage);
  await screen.findByDisplayValue('5');
  await fireEvent.update(screen.getByLabelText('每日额度'), '8');
  await fireEvent.click(screen.getByRole('button', { name:'保存设置' }));
  await waitFor(()=>expect(api.saveSettings).toHaveBeenCalledWith(expect.objectContaining({ daily_quota:8 })));
  await fireEvent.click(screen.getByRole('button', { name:'备份数据库' }));
  expect(api.backupDatabase).toHaveBeenCalled();
});
