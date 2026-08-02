<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { Clapperboard, Download, ScrollText, Settings, UsersRound, Plus } from '@lucide/vue';
import {
  createVideoTask,
  deleteAccount,
  fileToBase64,
  getSettings,
  listAccounts,
  listVideoTasks,
  loginEvents,
  startLogin,
  updateAccount,
} from './api';
import AccountTable from './components/AccountTable.vue';
import LogsPage from './components/LogsPage.vue';
import ResultsTable from './components/ResultsTable.vue';
import SettingsPage from './components/SettingsPage.vue';
import VideoTaskTable from './components/VideoTaskTable.vue';
import {
  DpButton,
  DpDialog,
  DpField,
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
  video_quota_used?: number;
  video_quota_total?: number;
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
  cover_url?: string;
  error?: string;
  quota_used?: number;
  quota_total?: number;
  created_at: string;
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
const model = ref('seedance_v2.0_mini');
const ratio = ref('1:1');
const duration = ref(5);
const imageFiles = ref<File[]>([]);
const imagePreviews = ref<string[]>([]);
const MAX_I2V_IMAGES = 9;
const MAX_I2V_BYTES = 15 * 1024 * 1024;
let taskTimer: ReturnType<typeof setInterval> | undefined;

/**
 * refreshTasks 的竞态保护:
 * 4 秒轮询时,后到的响应可能盖掉先到的(比如用户在 UI 上 retry 后立刻
 * 自己 fetch 一次)。给每次 fetch 一个递增 seq,只有最新 seq 的响应才能
 * 写回 tasks,stale 响应直接丢弃。
 */
let tasksFetchSeq = 0;

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
/** 有图=图生，无图=文生 */
const submitMode = computed(() => (imageFiles.value.length > 0 ? 'i2v' : 't2v'));

const pageMeta: Record<Page, [string, string]> = {
  accounts: ['账号池', '登录与会话管理'],
  videos: ['视频任务', '文生 / 图生队列'],
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
    tasks.value = fresh;
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

function clearImages() {
  for (const url of imagePreviews.value) URL.revokeObjectURL(url);
  imageFiles.value = [];
  imagePreviews.value = [];
}

function onImageSelected(event: Event) {
  const input = event.target as HTMLInputElement;
  const picked = Array.from(input.files || []);
  input.value = '';
  if (!picked.length) return;
  const room = MAX_I2V_IMAGES - imageFiles.value.length;
  if (room <= 0) {
    showToast('failed', `最多上传 ${MAX_I2V_IMAGES} 张图片`);
    return;
  }
  // 前端先按 15MB 过滤,避免大文件先 base64 编码塞进 WebView 进程内存
  // 后端 service.py:171 也会校验,这里是 belt-and-suspenders 防御。
  const oversized = picked.filter((file) => file.size > MAX_I2V_BYTES);
  if (oversized.length) {
    showToast(
      'failed',
      `已跳过 ${oversized.length} 张超大图片(>15MB):${oversized.map((f) => f.name).join(', ')}`,
    );
  }
  const accepted = picked.filter((file) => file.size <= MAX_I2V_BYTES);
  const next = accepted.slice(0, room);
  if (accepted.length > room) {
    showToast('failed', `最多 ${MAX_I2V_IMAGES} 张,已截取前 ${room} 张`);
  }
  imageFiles.value = [...imageFiles.value, ...next];
  imagePreviews.value = [
    ...imagePreviews.value,
    ...next.map((file) => URL.createObjectURL(file)),
  ];
}

function removeImage(index: number) {
  const url = imagePreviews.value[index];
  if (url) URL.revokeObjectURL(url);
  imageFiles.value = imageFiles.value.filter((_, i) => i !== index);
  imagePreviews.value = imagePreviews.value.filter((_, i) => i !== index);
}

function openTaskDialog() {
  clearImages();
  showTaskDialog.value = true;
}

/**
 * 关闭/取消 dialog 的统一入口。务必走这里,以便清掉 imageFiles +
 * 释放 blob URL,避免内存泄漏。
 */
function closeTaskDialog() {
  showTaskDialog.value = false;
  clearImages();
}

async function submitVideo() {
  const promptLines = prompt.value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  if (promptLines.length === 0) return;
  if (imageFiles.value.length > MAX_I2V_IMAGES) {
    showToast('failed', `最多支持 ${MAX_I2V_IMAGES} 张图片`);
    return;
  }
  creating.value = true;
  try {
    const mode = submitMode.value;
    const images =
      mode === 'i2v'
        ? await Promise.all(
            imageFiles.value.map(async (file) => ({
              name: file.name,
              data_base64: await fileToBase64(file),
            })),
          )
        : [];
    // 多行 prompt → 自动归组;单行 → 走原 prompt 字段(向后兼容)
    const isGroup = promptLines.length > 1;
    await createVideoTask({
      prompt: isGroup ? '' : promptLines[0],
      prompts: isGroup ? promptLines : undefined,
      model: model.value,
      ratio: ratio.value,
      duration: duration.value,
      account_id: null,
      mode,
      images,
    });
    prompt.value = '';
    clearImages();
    showTaskDialog.value = false;
    showToast(
      'succeeded',
      mode === 'i2v'
        ? `图生任务已加入队列（${images.length} 张图）`
        : isGroup
          ? `${promptLines.length} 段 prompt 已自动归组`
          : '文生任务已加入队列',
    );
    await refreshTasks();
  } catch (error) {
    showToast('failed', error instanceof Error ? error.message : '创建任务失败');
  } finally {
    creating.value = false;
  }
}

async function retryVideoTask(task: VideoTask) {
  if ((task.mode || 't2v') !== 't2v') {
    showToast('failed', '图生视频需重新上传图片，请新建任务');
    return;
  }
  creating.value = true;
  try {
    await createVideoTask({
      prompt: task.prompt,
      model: task.model,
      ratio: task.ratio,
      duration: task.duration,
      account_id: null,
      mode: 't2v',
      images: [],
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
  if (value.default_duration) duration.value = value.default_duration;
}

function showToast(nextState: string, nextMessage: string) {
  state.value = nextState;
  message.value = nextMessage;
}

function dismissToast() {
  message.value = '';
  state.value = '';
}

onMounted(async () => {
  await refreshAccounts();
  try {
    applyDefaults(await getSettings());
  } catch {
    /* defaults stay as-is */
  }
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
    <aside class="sidebar">
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

    <main class="workspace">
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
        />

        <template v-else-if="page === 'videos'">
          <div class="page-actions">
            <div class="meta">
              <strong>{{ running }}</strong>
              <span>个任务运行中</span>
              <span style="color: var(--text-faint)">·</span>
              <span>{{ tasks.length }} 总计</span>
            </div>
            <DpButton variant="solid" @click="openTaskDialog">
              <Plus :size="15" :stroke-width="2.25" />
              ＋ 添加任务
            </DpButton>
          </div>
          <VideoTaskTable :tasks="tasks" @retry="retryVideoTask" />
        </template>

        <ResultsTable v-else-if="page === 'results'" :tasks="results" />
        <LogsPage v-else-if="page === 'logs'" />
        <SettingsPage v-else @saved="applyDefaults" />
      </section>
    </main>

    <DpDialog
      :open="showTaskDialog"
      title="添加视频任务"
      description="后台自动分配可用账号，同账号任务串行执行"
      @close="closeTaskDialog"
    >
      <form @submit.prevent="submitVideo">
        <div class="image-field">
          <DpField label="图片（可选，最多 9 张）" for-id="video-image">
            <input
              id="video-image"
              class="file-input"
              type="file"
              accept="image/png,image/jpeg,image/webp,image/gif"
              multiple
              aria-label="图片"
              @change="onImageSelected"
            />
          </DpField>
          <div v-if="imagePreviews.length" class="image-preview-list">
            <div v-for="(src, index) in imagePreviews" :key="`${src}-${index}`" class="image-preview-item">
              <img :src="src" :alt="`图片 ${index + 1}`" />
              <DpButton size="sm" type="button" @click="removeImage(index)">移除</DpButton>
            </div>
          </div>
          <p class="image-hint">
            {{ imageFiles.length ? `图生 · 已选 ${imageFiles.length}/${MAX_I2V_IMAGES} 张` : '不传图为文生，传图为图生' }}
            · png / jpeg / webp / gif · 单张最大 {{ MAX_I2V_BYTES / 1024 / 1024 }}MB
          </p>
        </div>

        <DpField label="画面描述" for-id="video-prompt">
          <DpTextarea
            id="video-prompt"
            v-model="prompt"
            :rows="7"
            :maxlength="2000"
            autofocus
            :placeholder="
              imageFiles.length
                ? '描述图片如何运动、镜头和氛围…'
                : '描述主体、动作、场景、镜头和光线…\n\n一行一段 prompt 时自动归组到同一文件夹'
            "
          />
        </DpField>
        <div class="char-hint">
          {{ prompt.length }} / 2000 ·
          {{ prompt.split(/\r?\n/).filter((l) => l.trim()).length }} 段(多段自动归组)
        </div>

        <div class="form-grid">
          <DpField label="模型" for-id="video-model">
            <DpSelect id="video-model" v-model="model">
              <option value="seedance_v2.0_mini">Seedance 2.0 Mini</option>
              <option value="seedance_v2.0">Seedance 2.0 Fast</option>
              <option value="seedance_v2.0_std">Seedance 2.0</option>
            </DpSelect>
          </DpField>
          <DpField label="时长" for-id="video-duration">
            <DpSelect id="video-duration" v-model.number="duration">
              <option :value="5">5 秒</option>
              <option :value="10">10 秒</option>
            </DpSelect>
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
            {{ creating ? '正在添加…' : imageFiles.length ? '添加图生任务' : '添加文生任务' }}
          </DpButton>
        </div>
      </form>
    </DpDialog>

    <DpToast :message="message" :type="state" @close="dismissToast" />
  </div>
</template>

<style scoped>
.image-field {
  margin-bottom: 14px;
}

.file-input {
  width: 100%;
  padding: 10px;
  border: 1px dashed var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-input);
  color: var(--text-secondary);
}

.image-hint {
  margin: 8px 0 0;
  color: var(--text-faint);
  font-size: 12px;
}

.image-preview-list {
  display: flex;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 10px;
}

.image-preview-item {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 6px;
}

.image-preview-item img {
  width: 72px;
  height: 72px;
  object-fit: cover;
  border-radius: 8px;
  border: 1px solid var(--border);
  background: #111;
}

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
