<script setup lang="ts">
import { UsersRound, Plus, RefreshCw, Trash2 } from '@lucide/vue';
import { DpBadge, DpButton, DpEmpty, DpPanel, DpSwitch, DpTable } from '@/ui';

type Account = {
  id: string;
  display_name: string;
  nickname?: string;
  status: string;
  enabled: boolean;
  last_verified_at?: string;
  // v0.2.9:按 seedance 模型拆三桶。老 video_quota_used / video_quota_total
  // 仍保留(API alias 到 mini 桶),用于老缓存 / 单桶视图兜底。
  video_quota_used?: number;
  video_quota_total?: number;
  video_quota_used_mini?: number;
  video_quota_total_mini?: number;
  video_quota_used_v2?: number;
  video_quota_total_v2?: number;
  video_quota_used_std?: number;
  video_quota_total_std?: number;
  video_limited_until?: string;
};

type Bucket = 'mini' | 'v2' | 'std';
const BUCKETS: { key: Bucket; label: string }[] = [
  { key: 'mini', label: 'mini' },
  { key: 'v2', label: 'v2.0' },
  { key: 'std', label: 'std' },
];

defineProps<{ accounts: Account[]; loading?: boolean; busy?: boolean }>();
const emit = defineEmits<{
  add: [];
  toggle: [value: { id: string; enabled: boolean }];
  delete: [id: string];
  relogin: [id: string];
}>();

function remove(account: Account) {
  if (confirm(`确定删除账号“${account.display_name}”及其本地会话吗？`)) {
    emit('delete', account.id);
  }
}

function initial(name: string) {
  return (name || '?').trim().charAt(0).toUpperCase();
}

function bucketUsed(account: Account, bucket: Bucket): number {
  const field = `video_quota_used_${bucket}` as const;
  const value = account[field];
  if (typeof value === 'number') return value;
  // 兜底:老 API / 旧数据只用 video_quota_used。统一展示在 mini 桶。
  return bucket === 'mini' ? (account.video_quota_used ?? 0) : 0;
}

function bucketTotal(account: Account, bucket: Bucket): number {
  const field = `video_quota_total_${bucket}` as const;
  const value = account[field];
  if (typeof value === 'number') return value;
  return bucket === 'mini' ? (account.video_quota_total ?? 5) : 5;
}

function bucketRatio(account: Account, bucket: Bucket): number {
  const used = bucketUsed(account, bucket);
  const total = bucketTotal(account, bucket);
  return total > 0 ? Math.min(1, used / total) : 0;
}

function bucketWidth(account: Account, bucket: Bucket): number {
  return Math.round(bucketRatio(account, bucket) * 100);
}

function anyBucketExhausted(account: Account): boolean {
  return BUCKETS.some((b) => bucketRatio(account, b.key) >= 1);
}

function formatRecovery(value?: string) {
  if (!value) return '—';
  return new Date(value).toLocaleString('zh-CN');
}
</script>

<template>
  <DpPanel title="账号列表" :subtitle="`${accounts.length} 个账号`">
    <template #actions>
      <DpButton variant="solid" :disabled="busy" @click="$emit('add')">
        <Plus :size="15" :stroke-width="2.25" />
        {{ busy ? '等待登录…' : '＋ 添加账号' }}
      </DpButton>
    </template>

    <DpTable min-width="980px">
      <thead>
        <tr>
          <th style="width: 24%">账号</th>
          <th style="width: 12%">状态</th>
          <th style="width: 26%">今日额度(mini / v2 / std)</th>
          <th style="width: 16%">限额恢复</th>
          <th style="width: 10%">参与调度</th>
          <th style="width: 12%">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="loading">
          <td colspan="6">
            <DpEmpty title="正在加载…">
              <template #icon><UsersRound :size="18" /></template>
            </DpEmpty>
          </td>
        </tr>
        <tr v-else-if="!accounts.length">
          <td colspan="6">
            <DpEmpty title="还没有账号" description="添加账号后即可进入自动调度，扫码登录信息仅保存在本机。">
              <template #icon><UsersRound :size="18" /></template>
            </DpEmpty>
          </td>
        </tr>
        <tr v-for="account in accounts" :key="account.id">
          <td>
            <div class="acct-cell">
              <span class="avatar" :class="{ dim: !account.enabled }">{{ initial(account.display_name) }}</span>
              <div class="acct-meta">
                <b>{{ account.display_name }}</b>
                <small>{{ account.nickname || '豆包账号' }}</small>
              </div>
            </div>
          </td>
          <td>
            <DpBadge :tone="account.status === 'active' ? 'active' : 'expired'" dot>
              {{ account.status === 'active' ? '已登录' : '需登录' }}
            </DpBadge>
          </td>
          <td>
            <div class="quota-stack">
              <div
                v-for="bucket in BUCKETS"
                :key="bucket.key"
                class="quota-row"
                :class="{ limited: account.video_limited_until }"
              >
                <span class="quota-label">{{ bucket.label }}</span>
                <span class="quota-text">
                  {{ bucketUsed(account, bucket.key) }}/{{ bucketTotal(account, bucket.key) }}
                </span>
                <div
                  class="quota-bar"
                  :class="{
                    exhausted: bucketRatio(account, bucket.key) >= 1,
                    limited: account.video_limited_until,
                  }"
                >
                  <div
                    class="quota-bar-fill"
                    :style="{ width: bucketWidth(account, bucket.key) + '%' }"
                  />
                </div>
              </div>
              <small v-if="anyBucketExhausted(account) && !account.video_limited_until" class="quota-hint">
                至少一个模型额度已用完
              </small>
            </div>
          </td>
          <td class="muted">{{ formatRecovery(account.video_limited_until) }}</td>
          <td>
            <DpSwitch
              :on="account.enabled"
              :aria-label="`${account.enabled ? '停用' : '启用'} ${account.display_name}`"
              @click="$emit('toggle', { id: account.id, enabled: !account.enabled })"
            />
          </td>
          <td>
            <div class="row-actions">
              <DpButton
                v-if="account.status !== 'active'"
                size="sm"
                @click="$emit('relogin', account.id)"
              >
                <RefreshCw :size="12" />
                重新登录
              </DpButton>
              <DpButton
                size="sm"
                variant="danger"
                :aria-label="`删除 ${account.display_name}`"
                @click="remove(account)"
              >
                <Trash2 :size="12" />
                删除
              </DpButton>
            </div>
          </td>
        </tr>
      </tbody>
    </DpTable>
  </DpPanel>
</template>

<style scoped>
.acct-cell {
  display: flex;
  align-items: center;
  gap: 11px;
  min-width: 0;
}

.avatar {
  display: grid;
  place-items: center;
  width: 32px;
  height: 32px;
  flex-shrink: 0;
  border-radius: 8px;
  background: linear-gradient(135deg, var(--accent-soft), rgba(124, 106, 245, 0.28));
  border: 1px solid rgba(124, 106, 245, 0.25);
  color: var(--accent-text);
  font-size: 12px;
  font-weight: 700;
}

.avatar.dim {
  opacity: 0.45;
  filter: grayscale(0.4);
}

.acct-meta {
  min-width: 0;
}

.acct-meta b {
  display: block;
  color: var(--text);
  font-weight: 600;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.acct-meta small {
  display: block;
  margin-top: 2px;
  color: var(--text-faint);
  font-size: 11.5px;
}

.quota-stack {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 170px;
}

.quota-row {
  display: grid;
  grid-template-columns: 38px 1fr 70px;
  align-items: center;
  gap: 6px;
  font-size: 11.5px;
}

.quota-label {
  color: var(--text-faint);
  font-weight: 600;
  letter-spacing: 0.02em;
}

.quota-text {
  font-variant-numeric: tabular-nums;
  font-weight: 550;
  color: var(--text-secondary);
  text-align: right;
}

.quota-row.limited .quota-text {
  color: var(--danger-text);
}

.quota-bar {
  grid-column: 1 / 4;
  height: 5px;
  background: var(--bg-elev);
  border-radius: 3px;
  overflow: hidden;
}

.quota-bar-fill {
  height: 100%;
  background: var(--accent);
  transition: width 0.3s ease;
}

.quota-bar.exhausted .quota-bar-fill,
.quota-bar.limited .quota-bar-fill {
  background: var(--danger-text);
}

.quota-hint {
  color: var(--danger-text);
  font-size: 11px;
  margin-top: 2px;
}

.muted {
  color: var(--text-muted);
  font-size: 12px;
}

.row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
}
</style>
