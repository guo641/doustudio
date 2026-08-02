import { render, screen } from '@testing-library/vue';
import { expect, it, vi } from 'vitest';
import App from '../App.vue';

it('renders compact icon navigation without title cards', () => {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok:true, json:async()=>[] }));
  render(App);
  for (const name of ['账号池', '视频任务', '生成结果', '运行日志', '设置']) {
    expect(screen.getByRole('button', { name })).toBeTruthy();
  }
  expect(screen.queryByRole('heading', { name:'豆包账号' })).toBeNull();
  expect(screen.queryByText('账号总数')).toBeNull();
  expect(screen.getByRole('button', { name:'＋ 添加账号' })).toBeTruthy();
});
