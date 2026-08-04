<script setup lang="ts">
import { onMounted, ref, watch } from 'vue';
import { UsersRound, Plus, RefreshCw, Trash2 } from '@lucide/vue';
import { DpBadge, DpButton, DpEmpty, DpPanel, DpSwitch, DpTable } from '@/ui';
import {
  getWebMSSDKTokens,
  refreshWebMSSDKTokens,
  type WebMSSDKTokensResponse,
} from '@/api';

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
  video_quota_used_std?: number;
  video_quota_total_std?: number;
  video_limited_until?: string;
};

type Bucket = 'mini' | 'std';
const BUCKETS: { key: Bucket; label: string }[] = [
  { key: 'mini', label: 'mini' },
  { key: 'std', label: 'fast' },
];

const props = defineProps<{ accounts: Account[]; loading?: boolean; busy?: boolean }>();
const emit = defineEmits<{
  add: [];
  toggle: [value: { id: string; enabled: boolean }];
  delete: [id: string];
  relogin: [id: string];
}>();

// v0.2.17:token 状态。Record<accountId, bundle>,无 token 时 hint 引导用户去刷。
const tokenStatus = ref<Record<string, WebMSSDKTokensResponse | null>>({});
const refreshing = ref<Record<string, boolean>>({});
const refreshError = ref<Record<string, string>>({});

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

// v0.2.17:把 msToken age 格式化成"12 分钟前"等人类可读。
function formatTokenAge(bundle: WebMSSDKTokensResponse | null | undefined): string {
  if (!bundle || bundle.age_seconds == null) return '从未';
  const sec = bundle.age_seconds;
  if (sec < 60) return `${Math.round(sec)} 秒前`;
  if (sec < 3600) return `${Math.round(sec / 60)} 分钟前`;
  if (sec < 86400) return `${Math.round(sec / 3600)} 小时前`;
  return `${Math.round(sec / 86400)} 天前`;
}

async function loadTokenStatus(accounts: Account[]) {
  // 并行拉所有账号的 token 状态。任一失败只影响那一行的 hint,不让整页崩。
  await Promise.all(
    accounts.map(async (acc) => {
      try {
        tokenStatus.value[acc.id] = await getWebMSSDKTokens(acc.id);
      } catch (err) {
        tokenStatus.value[acc.id] = {
          available: false,
          hint: String(err),
          ms_token_preview: '',
          web_id: '',
          web_id_signature: '',
          device_id: '',
          tea_uuid: '',
          pc_version: '',
          fetched_at: 0,
          age_seconds: null,
        };
      }
    }),
  );
}

async function refreshOne(account: Account) {
  refreshing.value[account.id] = true;
  refreshError.value[account.id] = '';
  try {
    tokenStatus.value[account.id] = await refreshWebMSSDKTokens(account.id);
  } catch (err) {
    refreshError.value[account.id] = String(err);
  } finally {
    refreshing.value[account.id] = false;
  }
}

onMounted(() => {
  if (props.accounts?.length) loadTokenStatus(props.accounts);
});

// 父级换账号列表(添加 / 删除 / 刷新)时,重新拉 token 状态。
watch(
  () => props.accounts.map((a) => a.id).join(','),
  () => {
    if (props.accounts?.length) loadTokenStatus(props.accounts);
  },
);
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
          <th style="width: 22%">账号</th>
          <th style="width: 11%">状态</th>
          <th style="width: 22%">今日额度(mini / fast)</th>
          <th style="width: 14%">限额恢复</th>
          <th style="width: 13%">Token</th>
          <th style="width: 9%">参与调度</th>
          <th style="width: 9%">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="loading">
          <td colspan="7">
            <DpEmpty title="正在加载…">
              <template #icon><UsersRound :size="18" /></template>
            </DpEmpty>
          </td>
        </tr>
        <tr v-else-if="!accounts.length">
          <td colspan="7">
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
            <div class="token-cell">
              <div class="token-status">
                <DpBadge
                  :tone="tokenStatus[account.id]?.available ? 'active' : 'expired'"
                  dot
                >
                  {{ tokenStatus[account.id]?.available ? '正常' : '缺失' }}
                </DpBadge>
                <small class="token-age muted">{{ formatTokenAge(tokenStatus[account.id]) }}</small>
              </div>
              <small
                v-if="tokenStatus[account.id] && !tokenStatus[account.id]!.available"
                class="token-hint"
              >
                {{ tokenStatus[account.id]!.hint }}
              </small>
              <small v-if="refreshError[account.id]" class="token-hint error">
                {{ refreshError[account.id] }}
              </small>
            </div>
          </td>
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
                size="sm"
                :disabled="!!refreshing[account.id]"
                :aria-label="`刷新 ${account.display_name} 的 token`"
                @click="refreshOne(account)"
              >
                <RefreshCw :size="12" :class="{ spinning: refreshing[account.id] }" />
                {{ refreshing[account.id] ? '刷新中…' : '🔄 刷新 token' }}
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

.token-cell {
  display: flex;
  flex-direction: column;
  gap: 3px;
  min-width: 130px;
}

.token-status {
  display: flex;
  align-items: center;
  gap: 6px;
}

.token-age {
  font-size: 11px;
  color: var(--text-muted);
}

.token-hint {
  color: var(--danger-text);
  font-size: 10.5px;
  line-height: 1.35;
  word-break: break-all;
}

.token-hint.error {
  color: var(--danger-text);
}

.spinning {
  animation: token-spin 0.9s linear infinite;
}

@keyframes token-spin {
  to { transform: rotate(360deg); }
}
</style>
