import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/vue';
import { afterEach, expect, it, vi } from 'vitest';

const api = vi.hoisted(() => ({
  listLogs: vi.fn().mockResolvedValue([{ id:1, level:'ERROR', module:'doupool.video', event:'failed', message:'rate limited', created_at:'2026-07-13T12:00:00' }]),
  clearLogs: vi.fn().mockResolvedValue(undefined),
  getSettings: vi.fn().mockResolvedValue({ max_concurrency:1, daily_quota:5, quota_reset_time:'00:00', scheduler_strategy:'least_used', default_model:'seedance_v2.0_mini', default_duration:5, default_ratio:'1:1', download_dir:'/tmp/downloads', log_level:'INFO', log_retention_days:30, data_dir:'/tmp/data' }),
  saveSettings: vi.fn().mockImplementation(async value=>value),
  backupDatabase: vi.fn().mockResolvedValue({ path:'/tmp/backup.sqlite3' }),
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

it('shows successful results and download actions', () => {
  render(ResultsTable, { props:{ tasks:[{ id:'t1', prompt:'橘猫看雨', model:'seedance_v2.0_mini', ratio:'16:9', duration:5, account_name:'莲韵', status:'succeeded', result_url:'https://example.test/video.mp4', created_at:'2026-07-13T12:00:00' }] } });
  expect(screen.getByText('橘猫看雨')).toBeTruthy();
  expect(screen.getByRole('link', { name:'无水印下载' }).getAttribute('href')).toContain('video.mp4');
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
