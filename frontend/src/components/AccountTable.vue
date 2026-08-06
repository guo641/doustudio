<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from 'vue';
import { UsersRound, Plus, RefreshCw, Trash2, Globe2, Globe, RotateCcw } from '@lucide/vue';
import { DpBadge, DpButton, DpEmpty, DpPanel, DpSwitch, DpTable } from '@/ui';
import {
  getWebMSSDKTokens,
  refreshWebMSSDKTokens,
  openAccountBrowser,
  closeAccountBrowser,
  getAccountBrowserStatus,
  resetAccountQuota,
  resetAllQuotas,
  type WebMSSDKTokensResponse,
} from '@/api';

type Account = {
  id: string;
  display_name: string;
  nickname?: string;
  status: string;
  enabled: boolean;
  last_verified_at?: string;
  // v0.2.29:豆包官方按账号每日总配额(共享池,不区分模型)。
  // 老 mini/std 字段后端 alias 到 shared 兜底,前端不再读。
  video_quota_used_shared?: number;
  video_quota_total_shared?: number;
  video_limited_until?: string;
};

const props = defineProps<{ accounts: Account[]; loading?: boolean; busy?: boolean }>();
const emit = defineEmits<{
  add: [];
  toggle: [value: { id: string; enabled: boolean }];
  delete: [id: string];
  relogin: [id: string];
  // v0.2.22 Q3:用户停在账号面板时,后台视频任务跑完 quota 变化不会自动
  // 推送(只有 videos / results 页有 setInterval(refreshTasks, 4s))。
  // 加一个手动「🔄 刷新额度」按钮,父级 (App.vue) 接到后调 listAccounts()。
  refresh: [];
}>();

// v0.2.17:token 状态。Record<accountId, bundle>,无 token 时 hint 引导用户去刷。
const tokenStatus = ref<Record<string, WebMSSDKTokensResponse | null>>({});
const refreshing = ref<Record<string, boolean>>({});
const refreshError = ref<Record<string, string>>({});

// v0.2.20:「📂 打开浏览器」按钮状态 —— 哪些账号当前有 Chromium 窗口打开。
// 后端用 profile_dir 做 registry,前端用轮询(browser-status)跟住状态。
const browserOpen = ref<Record<string, boolean>>({});
const browserBusy = ref<Record<string, boolean>>({});
const browserError = ref<Record<string, string>>({});
// 兜底兜底:open-browser 后端是异步起 Playwright,我们要周期 re-pull status
// 才知道用户什么时候自己关了窗口。每 3 秒轮一次,只对正在打开的账号轮。
let browserPollTimer: number | null = null;

// v0.2.29:重置额度按钮 loading + 错误提示。
// 行级按钮靠 accountId 索引;顶部「一键全部」按钮用 resetAllBusy 单值。
const resetBusy = ref<Record<string, boolean>>({});
const resetAllBusy = ref(false);
const resetError = ref<string>('');

function remove(account: Account) {
  if (confirm(`确定删除账号“${account.display_name}”及其本地会话吗？`)) {
    emit('delete', account.id);
  }
}

function initial(name: string) {
  return (name || '?').trim().charAt(0).toUpperCase();
}

// v0.2.29:单桶视图 —— 共享池。后端不再返 mini/std 拆分,直接用 shared 字段。
// 兜底:老 DB / 老缓存可能没这两个字段,默认 0 / 50。
function sharedUsed(account: Account): number {
  return account.video_quota_used_shared ?? 0;
}
function sharedTotal(account: Account): number {
  return account.video_quota_total_shared ?? 50;
}
function sharedRatio(account: Account): number {
  const total = sharedTotal(account);
  return total > 0 ? Math.min(1, sharedUsed(account) / total) : 0;
}
function sharedWidth(account: Account): number {
  return Math.round(sharedRatio(account) * 100);
}
function sharedExhausted(account: Account): boolean {
  return sharedRatio(account) >= 1;
}

// v0.2.29:重置额度 —— 跨日 cron 卡住时的兜底按钮。
// 单账号:行级按钮 + confirm;一键全部:顶部按钮 + confirm(避免误触)。
async function resetOne(account: Account) {
  const ok = confirm(
    `确定重置账号「${account.display_name}」的今日共享额度?\n(豆包限流未自动解除时使用,操作不可撤销)`,
  );
  if (!ok) return;
  resetBusy.value[account.id] = true;
  resetError.value = '';
  try {
    await resetAccountQuota(account.id);
    emit('refresh');
  } catch (err) {
    resetError.value = err instanceof Error ? err.message : '重置失败';
  } finally {
    resetBusy.value[account.id] = false;
  }
}

async function resetAll() {
  const ok = confirm(
    `确定一键重置全部账号的今日共享额度?\n(豆包限流未自动解除时使用,操作不可撤销)`,
  );
  if (!ok) return;
  resetAllBusy.value = true;
  resetError.value = '';
  try {
    await resetAllQuotas();
    emit('refresh');
  } catch (err) {
    resetError.value = err instanceof Error ? err.message : '一键重置失败';
  } finally {
    resetAllBusy.value = false;
  }
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

// v0.2.20:复用账号 profile 拉起 Chromium 窗口。
// 后端 Playwright 是异步的(daemon thread),服务端 202 后窗口已开始启动;
// 我们立刻把状态设 true,启动轮询定时器,过几秒跟真实状态对一下。
async function openBrowser(account: Account) {
  browserBusy.value[account.id] = true;
  browserError.value[account.id] = '';
  try {
    await openAccountBrowser(account.id);
    browserOpen.value[account.id] = true;
    startBrowserPolling();
  } catch (err) {
    browserError.value[account.id] = String(err);
  } finally {
    browserBusy.value[account.id] = false;
  }
}

async function closeBrowser(account: Account) {
  browserBusy.value[account.id] = true;
  browserError.value[account.id] = '';
  try {
    await closeAccountBrowser(account.id);
    browserOpen.value[account.id] = false;
  } catch (err) {
    browserError.value[account.id] = String(err);
  } finally {
    browserBusy.value[account.id] = false;
  }
}

// v0.2.20:轮询「窗口是否还开着」。只在有窗口打开的账号上跑,避免无意义请求。
// 后端 Playwright 一旦 context 关掉,cancel event set → thread 退出 →
// registry.unregister。所以即使用户点叉叉,我们 3s 内也能跟住。
async function pollBrowserStatus() {
  const openIds = Object.entries(browserOpen.value)
    .filter(([, isOpen]) => isOpen)
    .map(([id]) => id);
  if (!openIds.length) {
    if (browserPollTimer != null) {
      window.clearInterval(browserPollTimer);
      browserPollTimer = null;
    }
    return;
  }
  await Promise.all(
    openIds.map(async (id) => {
      try {
        const res = await getAccountBrowserStatus(id);
        if (browserOpen.value[id] && !res.open) {
          // 状态变更(用户主动关掉 / 异常退出),前端跟住
          browserOpen.value[id] = false;
        }
      } catch {
        // 单个失败不影响其他账号的轮询
      }
    }),
  );
}

function startBrowserPolling() {
  if (browserPollTimer != null) return;
  browserPollTimer = window.setInterval(pollBrowserStatus, 3000);
}

onMounted(() => {
  if (props.accounts?.length) loadTokenStatus(props.accounts);
});

// v0.2.20:页面挂载时拉一遍 baseline 状态,避免「之前打开过的窗口」按钮文案错。
// (用户在前一个页面开过浏览器,跳回来应该看到按钮是「关闭」状态)
onMounted(async () => {
  await Promise.all(
    (props.accounts ?? []).map(async (acc) => {
      try {
        const res = await getAccountBrowserStatus(acc.id);
        browserOpen.value[acc.id] = res.open;
      } catch {
        /* 单个失败不阻断其他账号 */
      }
    }),
  );
  startBrowserPolling();
});

onUnmounted(() => {
  if (browserPollTimer != null) {
    window.clearInterval(browserPollTimer);
    browserPollTimer = null;
  }
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
      <DpButton
        aria-label="刷新账号额度"
        @click="$emit('refresh')"
      >
        <RefreshCw :size="15" :stroke-width="2.25" />
        刷新额度
      </DpButton>
      <!-- v0.2.29:一键全部重置额度 —— 跨日 cron 卡住时的兜底按钮。
           confirm 二次确认防误触。 -->
      <DpButton
        :disabled="resetAllBusy || !accounts.length"
        :aria-label="'一键重置全部账号的今日额度'"
        @click="resetAll"
      >
        <RotateCcw :size="15" :stroke-width="2.25" />
        {{ resetAllBusy ? '重置中…' : '重置全部额度' }}
      </DpButton>
      <DpButton variant="solid" :disabled="busy" @click="$emit('add')">
        <Plus :size="15" :stroke-width="2.25" />
        {{ busy ? '等待登录…' : '＋ 添加账号' }}
      </DpButton>
    </template>

    <small v-if="resetError" class="reset-banner">{{ resetError }}</small>

    <DpTable min-width="980px">
      <thead>
        <tr>
          <th style="width: 22%">账号</th>
          <th style="width: 11%">状态</th>
          <th style="width: 22%">今日共享额度</th>
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
              <div class="quota-row" :class="{ limited: account.video_limited_until }">
                <span class="quota-label">共享</span>
                <span class="quota-text">
                  {{ sharedUsed(account) }}/{{ sharedTotal(account) }}
                </span>
                <div
                  class="quota-bar"
                  :class="{
                    exhausted: sharedExhausted(account),
                    limited: account.video_limited_until,
                  }"
                >
                  <div
                    class="quota-bar-fill"
                    :style="{ width: sharedWidth(account) + '%' }"
                  />
                </div>
              </div>
              <small v-if="sharedExhausted(account) && !account.video_limited_until" class="quota-hint">
                今日额度已用完(等跨日重置或点行末「重置」)
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
                :variant="browserOpen[account.id] ? 'solid' : 'ghost'"
                :disabled="!!browserBusy[account.id]"
                :aria-label="browserOpen[account.id]
                  ? `关闭 ${account.display_name} 的浏览器窗口`
                  : `打开 ${account.display_name} 的浏览器窗口`"
                @click="browserOpen[account.id] ? closeBrowser(account) : openBrowser(account)"
              >
                <component
                  :is="browserOpen[account.id] ? Globe : Globe2"
                  :size="12"
                />
                {{
                  browserOpen[account.id]
                    ? browserBusy[account.id]
                      ? '关闭中…'
                      : '🟢 关闭浏览器'
                    : browserBusy[account.id]
                      ? '打开中…'
                      : '📂 打开浏览器'
                }}
              </DpButton>
              <DpButton
                size="sm"
                :disabled="!!refreshing[account.id]"
                :aria-label="`刷新 ${account.display_name} 的 token`"
                @click="refreshOne(account)"
              >
                <RefreshCw :size="12" :class="{ spinning: refreshing[account.id] }" />
                {{ refreshing[account.id] ? '刷新中…' : '🔄 刷新 token' }}
              </DpButton>
              <!-- v0.2.29:行级重置额度 —— 跨日 cron 卡住时的兜底按钮。 -->
              <DpButton
                size="sm"
                :disabled="!!resetBusy[account.id]"
                :aria-label="`重置 ${account.display_name} 的今日额度`"
                @click="resetOne(account)"
              >
                <RotateCcw :size="12" />
                {{ resetBusy[account.id] ? '重置中…' : '重置' }}
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
            <small v-if="browserError[account.id]" class="token-hint error">
              {{ browserError[account.id] }}
            </small>
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

/* v0.2.29:重置额度失败 / 顶部 banner —— 复用 token-hint 的红色字体,顶部加一点间距。 */
.reset-banner {
  display: block;
  margin: 0 0 8px;
  padding: 6px 10px;
  border-radius: 6px;
  background: var(--danger-bg, rgba(220, 38, 38, 0.08));
  color: var(--danger-text);
  font-size: 12px;
}
</style>
