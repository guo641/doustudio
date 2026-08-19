<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { Clapperboard, Download, ScrollText, Settings, UsersRound, Plus, Trash2 } from '@lucide/vue';
import {
  clearResults,
  clearVideoTasks,
  createVideoTask,
  deleteAccount,
  deleteVideoTask,
  getLicenseStatus,
  getSettings,
  listAccounts,
  listVideoTasks,
  loginEvents,
  quitApp,
  refreshResultUrl,
  startLogin,
  updateAccount,
  type LicenseState,
} from './api';
import { splitBySegmentMarkers } from './utils/promptParser';
import AccountTable from './components/AccountTable.vue';
import ActivationDialog from './components/ActivationDialog.vue';
import LogsPage from './components/LogsPage.vue';
import ResultsTable from './components/ResultsTable.vue';
import SettingsPage from './components/SettingsPage.vue';
import VideoTaskTable from './components/VideoTaskTable.vue';
import {
  DpButton,
  DpDialog,
  DpField,
  DpInput,
  DpSelect,
  DpTextarea,
  DpToast,
} from '@/ui';

type Page = 'accounts' | 'videos' | 'results' | 'logs' | 'settings';
type Account = {
  id: string;
  display_name: string;
  nickname?: string;
  status: string;
  enabled: boolean;
  last_verified_at?: string;
  // v0.2.29:共享额度池(豆包按账号每日总配额,不区分模型)。
  video_quota_used_shared?: number;
  video_quota_total_shared?: number;
  video_limited_until?: string;
};
type VideoTask = {
  id: string;
  account_name?: string;
  prompt: string;
  model: string;
  ratio: string;
  duration: number;
  mode?: string;
  image_count?: number;
  status: string;
  result_url?: string;
  backup_result_url?: string;
  fallback_result_url?: string;
  // v0.2.28:结果页用,zhuceka 处理后的无水印视频;存在时优先于 result_url 给用户下载。
  clean_video_url?: string;
  clean_error?: string;
  cover_url?: string;
  error?: string;
  quota_used?: number;
  quota_total?: number;
  // v0.2.28:批量任务(单 prompt「第一段/第二段」分隔,或传 prompts: list[str])
  // 会被后端打成同一 group_id。结果页按 group_id 折叠展示,组内点「下载全部」
  // /「保存到下载目录」自动建独立文件夹。
  group_id?: string;
  group_index?: number;
  group_name?: string;
  created_at: string;
  // v0.2.35:后端 _video_task_dict 注入的建议文件名,与 group_download 同源
  // (`{group_index:02d}_{HHMMSS}_{prompt前12字符}[-clean].mp4`)。
  download_filename?: string;
};

const page = ref<Page>('accounts');
const accounts = ref<Account[]>([]);
const tasks = ref<VideoTask[]>([]);
const loading = ref(true);
const busy = ref(false);
const creating = ref(false);
const showTaskDialog = ref(false);
const state = ref('');
const message = ref('');
const prompt = ref('');
const groupName = ref('');
const model = ref('seedance_v2.0_mini');
const FIXED_VIDEO_DURATION_SECONDS = 10;

// v0.3.0:激活闸门 —— 'loading' 是首屏瞬间;'valid' 渲染主 UI;
// 'needs-activation' / 'expired' / 'revoked' 渲染 ActivationDialog。
// licenseInfo 缓存 fingerprint + expires_at 给 dialog 显示。
const licenseState = ref<LicenseState>('loading');
const licenseInfo = ref<{ fingerprint: string; expires_at: number | null }>({
  fingerprint: '',
  expires_at: null,
});
const ratio = ref('1:1');
const duration = ref(FIXED_VIDEO_DURATION_SECONDS);
// v0.2.21:refreshTasks() 拿来判断「本轮新出现的终态任务」,触发实时刷 accounts。
const TERMINAL_STATUSES = ['succeeded', 'failed', 'cancelled'] as const;
let taskTimer: ReturnType<typeof setInterval> | undefined;

/**
 * refreshTasks 的竞态保护:
 * 4 秒轮询时,后到的响应可能盖掉先到的(比如用户在 UI 上 retry 后立刻
 * 自己 fetch 一次)。给每次 fetch 一个递增 seq,只有最新 seq 的响应才能
 * 写回 tasks,stale 响应直接丢弃。
 */
let tasksFetchSeq = 0;

// 首次启动和重新激活共用完整初始化。序号避免较早的一轮异步设置响应
// 覆盖较新授权状态；任务列表自身仍由 tasksFetchSeq 防止乱序覆盖。
let licensedWorkspaceInitSeq = 0;

/**
 * 当前激活的登录 EventSource。
 * 提到顶层是为了:
 * 1) 启动新 attempt 前先 close 旧的(避免快速连点"添加账号"导致多个
 *    SSE 流并存,UI 状态混乱)
 * 2) onBeforeUnmount 时关掉(否则 dialog 关闭后流还会推 toast)
 */
let activeLoginSource: EventSource | null = null;

const running = computed(() =>
  tasks.value.filter((t) => ['queued', 'starting', 'generating', 'resolving'].includes(t.status)).length,
);
const results = computed(() => tasks.value.filter((t) => t.status === 'succeeded' && t.result_url));
const activeAccounts = computed(() => accounts.value.filter((a) => a.enabled && a.status === 'active').length);
// v0.2.35:一键清除 —— 给按钮显示「N 个待清除」hint,N=0 时按钮 disable。
const completedTaskCount = computed(
  () => tasks.value.filter((t) => ['succeeded', 'failed', 'cancelled'].includes(t.status)).length,
);
const queuedTaskCount = computed(
  () => tasks.value.filter((t) => t.status === 'queued').length,
);
const downloadedResultsCount = computed(
  () =>
    tasks.value.filter(
      (t) => t.status === 'succeeded' && (t.clean_video_url || t.result_url),
    ).length,
);
const allResultsCount = computed(
  () => tasks.value.filter((t) => t.status === 'succeeded').length,
);
// v0.2.11:实时算当前文本会被切成几段,给底部 char-hint 显示提示
const segmentCount = computed(() => splitBySegmentMarkers(prompt.value).length);

const pageMeta: Record<Page, [string, string]> = {
  accounts: ['账号池', '登录与会话管理'],
  videos: ['视频任务', '文生视频队列'],
  results: ['生成结果', '无水印视频'],
  logs: ['运行日志', '本地运行记录'],
  settings: ['设置', '应用与调度配置'],
};

const navItems: { id: Page; label: string; icon: typeof UsersRound; group: 'workspace' | 'system' }[] = [
  { id: 'accounts', label: '账号池', icon: UsersRound, group: 'workspace' },
  { id: 'videos', label: '视频任务', icon: Clapperboard, group: 'workspace' },
  { id: 'results', label: '生成结果', icon: Download, group: 'workspace' },
  { id: 'logs', label: '运行日志', icon: ScrollText, group: 'system' },
  { id: 'settings', label: '设置', icon: Settings, group: 'system' },
];

function navBadge(id: Page): number | null {
  if (id === 'accounts') return accounts.value.length || null;
  if (id === 'videos') return running.value || null;
  if (id === 'results') return results.value.length || null;
  return null;
}

async function refreshAccounts() {
  try {
    accounts.value = await listAccounts();
  } catch (e) {
    // 不能 unhandled rejection;loading 也要复位,否则页面永远转圈
    showToast('failed', e instanceof Error ? e.message : '账号加载失败');
  } finally {
    loading.value = false;
  }
}

async function refreshTasks() {
  const mySeq = ++tasksFetchSeq;
  try {
    const fresh = await listVideoTasks();
    // 只有最新的 fetch 才写回,避免 stale 响应盖掉新数据
    if (mySeq !== tasksFetchSeq) return;
    // v0.2.22 Q3:hadTerminal 必须在 fresh 覆盖 tasks.value 之前构建。
    // v0.2.21 的实现顺序是「tasks.value = fresh; hadTerminal = ...」——
    // 首次进入 videos 页时 fresh 就是首次数据,hadTerminal ≡ fresh.terminal,
    // newTerminal 永远空,refreshAccounts() 从不触发,用户停在账号面板
    // 永远看不到 quota 变化。挪到 fresh 覆盖前就拿到「上一轮终态」。
    const hadTerminal = new Set(
      tasks.value
        .filter((t) => (TERMINAL_STATUSES as readonly string[]).includes(t.status))
        .map((t) => `${t.id}:${t.status}`),
    );
    tasks.value = fresh;
    const newTerminal = fresh.filter(
      (t) => (TERMINAL_STATUSES as readonly string[]).includes(t.status) && !hadTerminal.has(`${t.id}:${t.status}`),
    );
    if (newTerminal.length) void refreshAccounts();
  } catch (e) {
    if (mySeq !== tasksFetchSeq) return;
    showToast('failed', e instanceof Error ? e.message : '任务加载失败');
  }
}

async function showPage(next: Page) {
  page.value = next;
  if (next === 'videos' || next === 'results') {
    await refreshTasks();
    if (!taskTimer) {
      taskTimer = setInterval(() => {
        if (page.value === 'videos' || page.value === 'results') void refreshTasks();
      }, 4000);
    }
  }
}

async function add() {
  busy.value = true;
  showToast('launching', '正在启动豆包登录窗口…');
  // 启动新 attempt 前先关闭旧的 EventSource,避免快速连点产生多个 SSE 流
  if (activeLoginSource) {
    activeLoginSource.close();
    activeLoginSource = null;
  }
  try {
    const attempt = await startLogin();
    activeLoginSource = loginEvents(attempt.id, async (event) => {
      showToast(event.state, event.message);
      if (['succeeded', 'failed', 'cancelled', 'timed_out'].includes(event.state)) {
        busy.value = false;
        if (activeLoginSource) {
          activeLoginSource.close();
          activeLoginSource = null;
        }
        if (event.state === 'succeeded') await refreshAccounts();
      }
    });
  } catch (error) {
    busy.value = false;
    showToast('failed', error instanceof Error ? error.message : '启动失败');
  }
}

async function toggleAccount(value: { id: string; enabled: boolean }) {
  try {
    await updateAccount(value.id, { enabled: value.enabled });
    await refreshAccounts();
  } catch (error) {
    showToast('failed', error instanceof Error ? error.message : '账号更新失败');
  }
}

async function removeAccount(id: string) {
  try {
    await deleteAccount(id);
    await refreshAccounts();
    showToast('succeeded', '账号已删除');
  } catch (error) {
    showToast('failed', error instanceof Error ? error.message : '账号删除失败');
  }
}

function openTaskDialog() {
  showTaskDialog.value = true;
}

function closeTaskDialog() {
  showTaskDialog.value = false;
}

async function submitVideo() {
  // v0.2.11:不再按换行切分,按「第一段/第二段/段一/1.」段标记切。
  // 整段没标记就当一个 prompt,避免用户原文写整段自然语言时被误切。
  const promptSegments = splitBySegmentMarkers(prompt.value);
  if (promptSegments.length === 0) return;
  creating.value = true;
  try {
    // 多段 → 自动归组;单段 → 走原 prompt 字段(向后兼容)
    const isGroup = promptSegments.length > 1;
    const response = await createVideoTask({
      prompt: isGroup ? '' : promptSegments[0],
      prompts: isGroup ? promptSegments : undefined,
      model: model.value,
      ratio: ratio.value,
      duration: FIXED_VIDEO_DURATION_SECONDS,
      account_id: null,
      mode: 't2v',
      images: [],
      group_name: groupName.value.trim() || undefined,
    });
    prompt.value = '';
    groupName.value = '';
    showTaskDialog.value = false;
    // v0.2.35:跨账号凑余额 —— 后端 200 OK + {task, partial_rejected};
    // partial_rejected 非空时告知用户哪几条 prompt 暂时无账号可用、稍后会被自动重试
    const rejected = response?.partial_rejected ?? [];
    const queuedCount = promptSegments.length - rejected.length;
    let baseMsg = isGroup
      ? `${promptSegments.length} 段 prompt 已自动归组`
      : '文生任务已加入队列';
    if (isGroup) {
      baseMsg = `${queuedCount}/${promptSegments.length} 段已入队`;
    }
    if (rejected.length === 0) {
      showToast('succeeded', baseMsg);
    } else {
      // v0.2.35:部分 prompt 因跨账号凑余额暂时无账号可用 —— warning Toast
      // 给出明细让用户知道是哪几条,而不是只显示"已入队"造成疑惑
      const detail = rejected
        .map((r: { index: number; prompt: string }) => `#${r.index}「${r.prompt.slice(0, 16)}」`)
        .join('、');
      showToast(
        'warning',
        `${baseMsg};\n${rejected.length} 条暂无可用账号,稍后自动重试:${detail}`,
      );
    }
    await refreshTasks();
  } catch (error) {
    showToast('failed', error instanceof Error ? error.message : '创建任务失败');
  } finally {
    creating.value = false;
  }
}

async function onDeleteVideoTask(task: VideoTask) {
  try {
    await deleteVideoTask(task.id);
    showToast('succeeded', '任务已删除');
    await refreshTasks();
  } catch (error) {
    showToast('failed', error instanceof Error ? error.message : '任务删除失败');
  }
}

async function retryVideoTask(task: VideoTask) {
  if ((task.mode || 't2v') !== 't2v') {
    showToast('failed', '当前版本仅支持文生视频');
    return;
  }
  creating.value = true;
  try {
    await createVideoTask({
      prompt: task.prompt,
      model: task.model,
      ratio: task.ratio,
      duration: FIXED_VIDEO_DURATION_SECONDS,
      account_id: null,
      mode: 't2v',
      images: [],
      // v0.2.32:继承原 group_id,确保手动重试产生的新任务仍出现在
      // 原组(结果页按组聚合时不再丢失)。无 group 的任务(group_id 空)
      // 也允许,后端透传 None 即可,不影响无组任务行为。
      group_id: task.group_id || undefined,
    });
    showToast('succeeded', '任务已重新加入队列');
    await refreshTasks();
  } catch (error) {
    showToast('failed', error instanceof Error ? error.message : '重试任务失败');
  } finally {
    creating.value = false;
  }
}

function applyDefaults(value: any) {
  if (!value || typeof value !== 'object') return;
  if (value.default_model) model.value = value.default_model;
  if (value.default_ratio) ratio.value = value.default_ratio;
  duration.value = FIXED_VIDEO_DURATION_SECONDS;
}

// v0.2.22 Q4:DownloadButton 三层 fallback (cors / no-cors / window.open) 全
// 失败时(典型:签名 CDN URL 过期,Edge 报 ERR_INVALID_RESPONSE),自动调
// /api/results/:id/refresh-url 拿新签名 URL,再让用户重试下载。
//
// 防刷:Set 记录已为本轮调过 refresh-url 的 task_id,避免同 task 多次失败
// 把后端打爆。Set 在每次成功刷到 URL 后清空(下一次失败允许再刷 —— 这次
// 拿到的 URL 可能也过期了)。
const refreshedResultIds = new Set<string>();

async function onResultDownloadFailed(taskId: string) {
  if (refreshedResultIds.has(taskId)) {
    showToast('failed', '已尝试刷新链接,仍无法下载,请稍后再试');
    return;
  }
  showToast('launching', '下载链接已过期,正在重新获取…');
  try {
    const fresh = await refreshResultUrl(taskId);
    refreshedResultIds.add(taskId);
    const idx = tasks.value.findIndex((t) => t.id === taskId);
    if (idx >= 0) {
      tasks.value[idx] = {
        ...tasks.value[idx],
        result_url: fresh.result_url,
        backup_result_url: fresh.backup_result_url,
        fallback_result_url: fresh.fallback_result_url,
        error: undefined,
      };
    }
    showToast('succeeded', '链接已刷新,请重新点击下载');
    // 给用户几秒看到 toast,然后清掉 Set —— 下次再点下载(新过期 URL)
    // 还能再刷一次。后端 schedule_refresh_url 同步等待最长 60s。
    window.setTimeout(() => refreshedResultIds.delete(taskId), 8000);
  } catch (err) {
    showToast('failed', err instanceof Error ? err.message : '刷新下载链接失败');
  }
}

function showToast(nextState: string, nextMessage: string) {
  state.value = nextState;
  message.value = nextMessage;
}

function dismissToast() {
  message.value = '';
  state.value = '';
}

// v0.2.35:一键清除 —— 任务表「清完成」「清排队」按钮共用 confirm + 调端点。
// running 状态服务端不会动(避免打断正在生成),所以前置 warn 主要为用户提示。
async function onClearTasks(target: 'completed' | 'queued') {
  if (busy.value) return;
  const label = target === 'completed' ? '已完成(成功/失败/已取消)' : '排队中(未开始生成)';
  const ok = window.confirm(
    target === 'completed'
      ? `确定清除所有 ${label} 的任务吗?\n将删除这些任务的记录(本地视频文件保留)。`
      : `确定清除所有 ${label} 的任务吗?\n将删除这些任务的记录,并退还已预扣的额度。`,
  );
  if (!ok) return;
  busy.value = true;
  try {
    const result = await clearVideoTasks(target);
    showToast('succeeded', `已清除 ${result.deleted_count} 个任务`);
    await refreshTasks();
    await refreshAccounts();
  } catch (error) {
    showToast(
      'failed',
      error instanceof Error ? error.message : '清除任务失败',
    );
  } finally {
    busy.value = false;
  }
}

// v0.2.35:一键清除结果 —— 已下载 vs 全部。succeeded 已结算额度,无需退。
async function onClearResults(downloadedOnly: boolean) {
  if (busy.value) return;
  const label = downloadedOnly ? '已下载(存在可下载链接)' : '全部';
  const ok = window.confirm(
    `确定清除所有 ${label} 的生成结果吗?\n将删除这些任务的记录(本地视频文件保留)。`,
  );
  if (!ok) return;
  busy.value = true;
  try {
    const result = await clearResults(downloadedOnly);
    showToast('succeeded', `已清除 ${result.deleted_count} 个结果`);
    await refreshTasks();
  } catch (error) {
    showToast(
      'failed',
      error instanceof Error ? error.message : '清除结果失败',
    );
  } finally {
    busy.value = false;
  }
}

// v0.3.0:激活闸门刷新 —— onMounted 第一件事就拉一次。
// 后端不强制要求授权,所以 fetch 不会 401。
async function refreshLicense() {
  try {
    const status = await getLicenseStatus();
    licenseInfo.value = {
      fingerprint: status.fingerprint,
      expires_at: status.expires_at,
    };
    if (status.status === 'valid') {
      licenseState.value = 'valid';
    } else if (status.status === 'expired') {
      licenseState.value = 'expired';
    } else if (status.status === 'revoked') {
      licenseState.value = 'revoked';
    } else {
      // 'missing' / 'uncompiled'(开发态) → 都要求激活
      licenseState.value = 'needs-activation';
    }
  } catch {
    // 网络断了 / 后端崩了 —— 让用户看到激活窗 + 「请重试」提示
    licenseState.value = 'needs-activation';
  }
}

async function initializeLicensedWorkspace() {
  const initSeq = ++licensedWorkspaceInitSeq;
  await refreshLicense();
  if (initSeq !== licensedWorkspaceInitSeq || licenseState.value !== 'valid') return;

  const settingsPromise = getSettings().catch(() => null);
  await Promise.all([refreshAccounts(), refreshTasks()]);
  const settings = await settingsPromise;
  if (
    settings &&
    initSeq === licensedWorkspaceInitSeq &&
    licenseState.value === 'valid'
  ) {
    applyDefaults(settings);
  }
}

function onLicenseActivated() {
  // 激活成功后必须完整加载账号、任务和设置，不能只切换 UI 状态。
  void initializeLicensedWorkspace();
}

async function onLicenseQuit() {
  await quitApp();
}

onMounted(async () => {
  // 闸门最优先 —— 未激活就不去拉账号/任务,减少无意义请求 + 防止 race
  await initializeLicensedWorkspace();
});

onBeforeUnmount(() => {
  if (taskTimer) clearInterval(taskTimer);
  // 关闭登录 SSE,避免 dialog 卸载后流还在跑、还在推 toast
  if (activeLoginSource) {
    activeLoginSource.close();
    activeLoginSource = null;
  }
});
</script>

<template>
  <div class="app">
    <!-- v0.3.0:激活闸门 —— 'valid' 时渲染主 UI,其他态渲染 ActivationDialog。
         未激活状态下整个 sidebar + workspace 都不挂载,用户看不到主功能入口。
         license 状态由 onMounted → refreshLicense() 拉取。 -->
    <aside v-if="licenseState === 'valid'" class="sidebar">
      <div class="brand">
        <span class="brand-mark">D</span>
        <div class="brand-text">
          <span class="brand-name">Doubao</span>
          <span class="brand-tag">Manager</span>
        </div>
      </div>

      <div class="nav-group">
        <label class="nav-label">Workspace</label>
        <button
          v-for="item in navItems.filter((n) => n.group === 'workspace')"
          :key="item.id"
          class="nav-item"
          :class="{ active: page === item.id }"
          :aria-label="item.label"
          @click="showPage(item.id)"
        >
          <component :is="item.icon" :size="17" :stroke-width="1.75" />
          {{ item.label }}
          <b v-if="navBadge(item.id) != null" class="nav-badge">{{ navBadge(item.id) }}</b>
        </button>

        <label class="nav-label">System</label>
        <button
          v-for="item in navItems.filter((n) => n.group === 'system')"
          :key="item.id"
          class="nav-item"
          :class="{ active: page === item.id }"
          :aria-label="item.label"
          @click="showPage(item.id)"
        >
          <component :is="item.icon" :size="17" :stroke-width="1.75" />
          {{ item.label }}
        </button>
      </div>

      <div class="sidebar-footer">
        <div class="status-pill">
          <span class="dot" />
          调度器在线
          <span v-if="activeAccounts" style="margin-left: auto; opacity: 0.75">{{ activeAccounts }} 可用</span>
        </div>
      </div>
    </aside>

    <main v-if="licenseState === 'valid'" class="workspace">
      <header class="topbar">
        <strong class="topbar-title">{{ pageMeta[page][0] }}</strong>
        <span class="topbar-sep">/</span>
        <span class="topbar-sub">{{ pageMeta[page][1] }}</span>
      </header>

      <section class="content">
        <AccountTable
          v-if="page === 'accounts'"
          :accounts="accounts"
          :loading="loading"
          :busy="busy"
          @add="add"
          @toggle="toggleAccount"
          @delete="removeAccount"
          @relogin="add"
          @refresh="refreshAccounts"
        />

        <template v-else-if="page === 'videos'">
          <div class="page-actions">
            <div class="meta">
              <strong>{{ running }}</strong>
              <span>个任务运行中</span>
              <span style="color: var(--text-faint)">·</span>
              <span>{{ tasks.length }} 总计</span>
            </div>
            <div class="action-group">
              <!-- v0.2.35:一键清除 —— 双按钮 + N=0 时 disable,confirm 弹窗后
                   调后端,预扣的额度走退路,本地视频文件保留。 -->
              <DpButton
                size="sm"
                variant="ghost"
                :disabled="busy || completedTaskCount === 0"
                :title="completedTaskCount === 0 ? '没有已完成任务可清' : `清除 ${completedTaskCount} 个成功/失败/已取消任务`"
                @click="onClearTasks('completed')"
              >
                <Trash2 :size="13" :stroke-width="2" />
                清已完成 ({{ completedTaskCount }})
              </DpButton>
              <DpButton
                size="sm"
                variant="ghost"
                :disabled="busy || queuedTaskCount === 0"
                :title="queuedTaskCount === 0 ? '没有排队中任务可清' : `清除 ${queuedTaskCount} 个排队任务(退还预扣额度)`"
                @click="onClearTasks('queued')"
              >
                <Trash2 :size="13" :stroke-width="2" />
                清排队 ({{ queuedTaskCount }})
              </DpButton>
              <DpButton variant="solid" @click="openTaskDialog">
                <Plus :size="15" :stroke-width="2.25" />
                ＋ 添加任务
              </DpButton>
            </div>
          </div>
          <VideoTaskTable :tasks="tasks" @retry="retryVideoTask" @delete="onDeleteVideoTask" @download-failed="onResultDownloadFailed" />
        </template>

        <template v-else-if="page === 'results'">
          <div class="page-actions">
            <div class="meta">
              <strong>{{ results.length }}</strong>
              <span>个结果</span>
            </div>
            <div class="action-group">
              <!-- v0.2.35:一键清除结果 —— 双按钮,本地视频文件保留,DB row 物理删。
                   succeeded 任务在生成成功时已结算额度,无需退额度。 -->
              <DpButton
                size="sm"
                variant="ghost"
                :disabled="busy || downloadedResultsCount === 0"
                :title="downloadedResultsCount === 0 ? '没有已下载结果可清' : `清除 ${downloadedResultsCount} 个已下载结果`"
                @click="onClearResults(true)"
              >
                <Trash2 :size="13" :stroke-width="2" />
                清已下载 ({{ downloadedResultsCount }})
              </DpButton>
              <DpButton
                size="sm"
                variant="ghost"
                :disabled="busy || allResultsCount === 0"
                :title="allResultsCount === 0 ? '没有结果可清' : `清除全部 ${allResultsCount} 个结果`"
                @click="onClearResults(false)"
              >
                <Trash2 :size="13" :stroke-width="2" />
                清全部 ({{ allResultsCount }})
              </DpButton>
            </div>
          </div>
          <ResultsTable :tasks="results" @download-failed="onResultDownloadFailed" />
        </template>
        <LogsPage v-else-if="page === 'logs'" />
        <SettingsPage v-else @saved="applyDefaults" />
      </section>
    </main>

    <ActivationDialog
      v-if="licenseState !== 'valid'"
      :state="licenseState"
      :fingerprint="licenseInfo.fingerprint"
      :expires_at="licenseInfo.expires_at"
      @activated="onLicenseActivated"
      @quit="onLicenseQuit"
    />

    <DpDialog
      :open="showTaskDialog"
      title="添加视频任务"
      description="后台自动分配可用账号，同账号任务串行执行"
      @close="closeTaskDialog"
    >
      <form @submit.prevent="submitVideo">
        <DpField label="画面描述" for-id="video-prompt">
          <DpTextarea
            id="video-prompt"
            v-model="prompt"
            :rows="7"
            :maxlength="5000"
            autofocus
            placeholder="描述主体、动作、场景、镜头和光线…\n\n多段 prompt 用「第一段」「第二段」…分隔,自动归到同一组"
          />
        </DpField>
        <div class="char-hint">
          {{ prompt.length }} / 5000 ·
          {{ segmentCount }} 段(用「第一段」「第二段」分隔自动归组)
        </div>

        <DpField label="组名" for-id="video-group-name" hint="可选；填写后该任务会归入此组">
          <DpInput
            id="video-group-name"
            v-model="groupName"
            :maxlength="40"
            placeholder="例如：美女蛇"
          />
        </DpField>

        <div class="form-grid">
          <DpField label="模型" for-id="video-model">
            <DpSelect id="video-model" v-model="model">
              <!-- v0.2.11:去掉 seedance_v2.0(收费模型,只留 UI 选项)。
                   std 改名为 Fast,跟用户口语对齐。 -->
              <option value="seedance_v2.0_mini">Seedance Mini</option>
              <option value="seedance_v2.0_std">Seedance Fast</option>
            </DpSelect>
          </DpField>
          <DpField label="时长" for-id="video-duration">
            <DpInput
              id="video-duration"
              v-model.number="duration"
              type="number"
              :min="FIXED_VIDEO_DURATION_SECONDS"
              :max="FIXED_VIDEO_DURATION_SECONDS"
              :step="1"
              disabled
            />
          </DpField>
          <DpField label="比例" for-id="video-ratio">
            <DpSelect id="video-ratio" v-model="ratio">
              <option v-for="value in ['1:1', '3:4', '4:3', '9:16', '16:9', '21:9']" :key="value">
                {{ value }}
              </option>
            </DpSelect>
          </DpField>
        </div>

        <div class="dialog-actions">
          <DpButton type="button" @click="closeTaskDialog">取消</DpButton>
          <DpButton type="submit" variant="primary" :disabled="creating || !prompt.trim()">
            {{ creating ? '正在添加…' : '添加文生任务' }}
          </DpButton>
        </div>
      </form>
    </DpDialog>

    <DpToast :message="message" :type="state" @close="dismissToast" />
  </div>
</template>

<style scoped>
.char-hint {
  display: flex;
  justify-content: flex-end;
  margin: 6px 0 14px;
  color: var(--text-faint);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
}

.form-grid {
  display: grid;
  grid-template-columns: 1.5fr 0.75fr 0.75fr;
  gap: 10px;
}

.dialog-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
}

@media (max-width: 900px) {
  .form-grid {
    grid-template-columns: 1fr;
  }
}
</style>
