<script setup lang="ts">
import { computed, ref } from 'vue';
import {
  Download,
  ExternalLink,
  Link2,
  Film,
  Sparkles,
  ChevronDown,
  ChevronRight,
  FolderDown,
  Save,
} from '@lucide/vue';
import { DpButton, DpEmpty, DpLink, DpPanel, DpTable } from '@/ui';
import DownloadButton from '@/ui/DownloadButton.vue';
import { groupDownload } from '@/api';

type ResultTask = {
  id: string;
  prompt: string;
  model: string;
  ratio: string;
  duration: number;
  account_name?: string;
  status: string;
  result_url?: string;
  // 无水印视频由 zhuceka 接口处理后回填;存在时优先给用户下载,
  // 不存在时退回到 result_url(原画视频),并标"未去水印"提示。
  clean_video_url?: string;
  clean_error?: string;
  // v0.2.28:批量任务(单 prompt「第一段/第二段」分隔或传 prompts: list[str])
  // 会被后端打成同一 group_id,结果页按组折叠展示。
  group_id?: string;
  group_index?: number;
  created_at: string;
};

// v0.2.22 Q4:DownloadButton 三层 fallback 全失败时(reason 来自签名
// CDN URL 过期),冒到 App.vue → onResultDownloadFailed → POST
// /api/results/:id/refresh-url 重解析签名 URL。
const props = defineProps<{ tasks: ResultTask[] }>();
const emit = defineEmits<{ 'download-failed': [taskId: string] }>();

// v0.2.11:去掉 seedance_v2.0(收费模型)label,std 改名为 Fast。
// 如果有老任务用 v2,fallback 到原值给用户看,不静默消失。
const models: Record<string, string> = {
  'seedance_v2.0_std': 'Seedance Fast',
  'seedance_v2.0_mini': 'Seedance Mini',
  'seedance_v2.0': 'Seedance 2.0',
};

/**
 * 优先返回 zhuceka 处理后的无水印视频,fallback 到 result_url。
 * 这样即便去水印失败,用户依然能下载到原画视频。
 */
function pickDownloadUrl(task: ResultTask): string | undefined {
  return task.clean_video_url || task.result_url;
}

function hasCleanVideo(task: ResultTask): boolean {
  return Boolean(task.clean_video_url);
}

async function copy(value: string) {
  await navigator.clipboard?.writeText(value);
}

// v0.2.28:按 group_id 聚合。group_id 为空的任务归入 UNGROUPED 虚拟组
// (沿用扁平布局,与 v0.2.27 行为一致,避免老任务被强制显示组头)。
const UNGROUPED = '__ungrouped__';

type GroupBucket = {
  key: string;
  group_id?: string;
  tasks: ResultTask[];
};

const groupedTasks = computed<GroupBucket[]>(() => {
  const buckets = new Map<string, GroupBucket>();
  for (const task of props.tasks) {
    const key = task.group_id || UNGROUPED;
    let bucket = buckets.get(key);
    if (!bucket) {
      bucket = { key, group_id: task.group_id, tasks: [] };
      buckets.set(key, bucket);
    }
    bucket.tasks.push(task);
  }
  // 顺序:有组的组(group_id 不为空)按最小 created_at 升序排前面;
  // UNGROUPED 桶最后,内部按 created_at 降序。
  // 组内按 group_index 升序(后端 list_tasks_by_group 也是这个序,前端再保证一次)。
  const list = Array.from(buckets.values());
  for (const bucket of list) {
    bucket.tasks.sort((a, b) => {
      if (bucket.key !== UNGROUPED) {
        const ai = a.group_index ?? 0;
        const bi = b.group_index ?? 0;
        if (ai !== bi) return ai - bi;
      }
      return a.created_at < b.created_at ? -1 : 1;
    });
  }
  return list.sort((a, b) => {
    if (a.key === UNGROUPED) return 1;
    if (b.key === UNGROUPED) return -1;
    return a.tasks[0]!.created_at < b.tasks[0]!.created_at ? -1 : 1;
  });
});

// v0.2.28:组头默认全部展开(用户在结果页,折叠反而添堵)。
// 用 Set 是为了 key 多时仍然 O(1) 查询 + 响应式追踪。
const expandedGroups = ref<Set<string>>(new Set(groupedTasks.value.map((g) => g.key)));

function toggleGroup(key: string) {
  const next = new Set(expandedGroups.value);
  next.has(key) ? next.delete(key) : next.add(key);
  expandedGroups.value = next;
}

// v0.2.28:组 ID 前 8 位 + HHMMSS,作为浏览器下载管理器自动建子目录的
// 文件名前缀。仅用于 filename 拼接,不在 FS 真建目录(那是后端的事)。
function batchFolderName(groupId: string): string {
  const d = new Date();
  const hh = String(d.getHours()).padStart(2, '0');
  const mm = String(d.getMinutes()).padStart(2, '0');
  const ss = String(d.getSeconds()).padStart(2, '0');
  return `${groupId.slice(0, 8)}_${hh}${mm}${ss}`;
}

function filenameForTask(task: ResultTask, groupId: string | undefined): string {
  const stem = `doubao-${task.id}${hasCleanVideo(task) ? '-clean' : ''}.mp4`;
  return groupId ? `${batchFolderName(groupId)}/${stem}` : stem;
}

/**
 * 把 blob 喂给 <a download>。从 DownloadButton.vue 抽出来给批量下载复用
 * (DownloadButton 是 Vue 单文件组件,内部 helper 不能直接调用)。
 * 返回 false 表示 blob 不可用(空 body / 无 type),外层 fallback。
 */
function triggerBlobDownload(blob: Blob, filename: string): boolean {
  if (!blob || blob.size === 0) return false;
  try {
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = filename;
    a.rel = 'noopener';
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(url), 1500);
    return true;
  } catch {
    return false;
  }
}

function openInSystemBrowser(url: string): boolean {
  try {
    return window.open(url, '_blank', 'noopener,noreferrer') !== null;
  } catch {
    return false;
  }
}

/**
 * 单条任务的 fetch + blob 下载。和 DownloadButton 同款三层 fallback,
 * 只是 filename 走 `${folder}/doubao-${id}.mp4` 让浏览器下载管理器
 * 自动建子目录(实测 WebView2/Edge 桌面版 Chromium 内核支持子目录字符)。
 */
async function downloadOne(task: ResultTask, groupId: string | undefined): Promise<boolean> {
  const url = pickDownloadUrl(task);
  if (!url) return false;
  const filename = filenameForTask(task, groupId);

  // 1) cors
  try {
    const resp = await fetch(url, { mode: 'cors' });
    if (resp.ok) {
      const blob = await resp.blob();
      if (triggerBlobDownload(blob, filename)) return true;
    }
  } catch {
    /* cors 拒绝 → 下一层 */
  }
  // 2) no-cors opaque
  try {
    const resp = await fetch(url, { mode: 'no-cors' });
    const blob = await resp.blob();
    if (triggerBlobDownload(blob, filename)) return true;
  } catch {
    /* opaque 失败 → 兜底 */
  }
  // 3) 系统浏览器
  return openInSystemBrowser(url);
}

// v0.2.28:组级 busy —— 一个组一按钮在跑就把这组都 disable。
// 用 Set 是为了并行点两个组的下载不互相阻塞。
const groupBusy = ref<Set<string>>(new Set());

function setGroupBusy(key: string, on: boolean) {
  const next = new Set(groupBusy.value);
  on ? next.add(key) : next.delete(key);
  groupBusy.value = next;
}

/**
 * v0.2.28 Q2 前端方案:循环触发组内每条下载,间隔 350ms 让浏览器下载
 * 管理器从容接住(实测 WebView2 下 <300ms 会偶发丢失)。任意一条三层
 * fallback 全失败,只让那一组标红,不影响其他组。
 */
async function downloadGroupFrontend(group: GroupBucket) {
  if (groupBusy.value.has(group.key)) return;
  setGroupBusy(group.key, true);
  try {
    for (const task of group.tasks) {
      if (!pickDownloadUrl(task)) continue;
      // 单条失败不阻断整组(其他任务可能签名 URL 还活着)
      const ok = await downloadOne(task, group.group_id);
      if (!ok) {
        // 触发 App.vue 的 refresh 兜底链(单条刷新,不影响组里其他条)
        emit('download-failed', task.id);
      }
      await new Promise((r) => setTimeout(r, 350));
    }
  } finally {
    setGroupBusy(group.key, false);
  }
}

/**
 * v0.2.28 Q2 后端方案:整组一次性 POST 给后端,后端用 httpx 流式写到
 * settings.download_dir/<batch_folder>/。返回路径 alert 出来。
 * PyWebView 没有 toast,用 window.alert 简单可靠。
 */
async function downloadGroupBackend(group: GroupBucket) {
  if (!group.group_id) return;
  if (groupBusy.value.has(group.key)) return;
  setGroupBusy(group.key, true);
  try {
    const result = await groupDownload(group.group_id);
    window.alert(`已保存 ${result.file_count} 个文件到:\n${result.saved_dir}`);
  } catch (err) {
    const msg = err instanceof Error ? err.message : '保存失败';
    window.alert(`保存批量视频失败:${msg}`);
  } finally {
    setGroupBusy(group.key, false);
  }
}
</script>

<template>
  <DpPanel title="生成结果" :subtitle="`${tasks.length} 个视频`">
    <DpTable min-width="850px">
      <thead>
        <tr>
          <th style="width: 36%">任务描述</th>
          <th style="width: 16%">模型参数</th>
          <th style="width: 14%">执行账号</th>
          <th style="width: 16%">完成时间</th>
          <th style="width: 18%">操作</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="!tasks.length">
          <td colspan="5">
            <DpEmpty title="暂无已完成的视频" description="任务成功后，无水印结果会显示在这里。">
              <template #icon><Film :size="18" /></template>
            </DpEmpty>
          </td>
        </tr>
        <template v-for="group in groupedTasks" :key="group.key">
          <!-- v0.2.28:组头 —— 只对有 group_id 的桶渲染。
               UNGROUPED 桶(老任务)不显示组头,保持 v0.2.27 扁平布局。 -->
          <tr v-if="group.group_id" class="group-header">
            <td colspan="5">
              <button
                type="button"
                class="group-toggle"
                :aria-label="expandedGroups.has(group.key) ? '折叠组' : '展开组'"
                @click="toggleGroup(group.key)"
              >
                <ChevronDown v-if="expandedGroups.has(group.key)" :size="14" />
                <ChevronRight v-else :size="14" />
                <span class="group-title">
                  组 #{{ group.group_id.slice(0, 8) }} · {{ group.tasks.length }} 个视频
                </span>
                <span class="group-hint">
                  下载会自动归到独立文件夹
                </span>
              </button>
              <div class="group-actions" @click.stop>
                <DpButton
                  size="sm"
                  :disabled="groupBusy.has(group.key)"
                  :title="groupBusy.has(group.key) ? '下载中…' : '浏览器下载管理器会自动建子文件夹'"
                  @click="downloadGroupFrontend(group)"
                >
                  <FolderDown :size="12" />
                  下载全部
                </DpButton>
                <DpButton
                  size="sm"
                  variant="solid"
                  :disabled="groupBusy.has(group.key)"
                  :title="groupBusy.has(group.key) ? '保存中…' : '保存到设置里的下载目录'"
                  @click="downloadGroupBackend(group)"
                >
                  <Save :size="12" />
                  保存到下载目录
                </DpButton>
              </div>
            </td>
          </tr>
          <tr
            v-for="task in group.tasks"
            v-show="group.group_id ? expandedGroups.has(group.key) : true"
            :key="task.id"
          >
            <td>
              <div class="prompt" :title="task.prompt">{{ task.prompt }}</div>
            </td>
            <td class="params">
              {{ models[task.model] || task.model }} · {{ task.duration }}s · {{ task.ratio }}
            </td>
            <td>{{ task.account_name || '—' }}</td>
            <td class="time">{{ new Date(task.created_at).toLocaleString('zh-CN') }}</td>
            <td>
              <div class="row-actions">
                <DpLink v-if="task.result_url" :href="task.result_url" external>
                  <ExternalLink :size="12" />
                  预览
                </DpLink>
                <!-- 下载走 DownloadButton(走 Blob 中转),
                     避免 WebView2/Chromium 在 cross-origin URL 上把 <a download>
                     静默降级成导航离开应用。 -->
                <DownloadButton
                  v-if="pickDownloadUrl(task)"
                  :href="pickDownloadUrl(task)!"
                  :filename="filenameForTask(task, group.group_id)"
                  @download-failed="() => emit('download-failed', task.id)"
                >
                  <Download :size="12" />
                  {{ hasCleanVideo(task) ? '下载无水印' : '下载视频' }}
                </DownloadButton>
                <DpLink v-if="pickDownloadUrl(task)" as-button @click="copy(pickDownloadUrl(task)!)">
                  <Link2 :size="12" />
                  复制链接
                </DpLink>
                <span
                  v-if="task.clean_error"
                  class="clean-badge"
                  :title="task.clean_error"
                  aria-label="去水印失败"
                >
                  <Sparkles :size="11" />
                  去水印失败
                </span>
              </div>
            </td>
          </tr>
        </template>
      </tbody>
    </DpTable>
  </DpPanel>
</template>

<style scoped>
.prompt {
  max-width: 420px;
  color: var(--text);
  font-weight: 600;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.params,
.time {
  color: var(--text-muted);
  font-size: 12px;
  white-space: nowrap;
}

.row-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 10px;
}

/* 去水印失败的小红标 —— 用户能一眼看到这条不是无水印版本 */
.clean-badge {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  padding: 2px 6px;
  border-radius: 999px;
  background: var(--danger-soft, rgba(240, 113, 90, 0.12));
  color: var(--danger-text, #c14a3a);
  font-size: 10.5px;
  font-weight: 500;
  white-space: nowrap;
  cursor: help;
}

/* v0.2.28:组头 —— 整行横跨 5 列,左边是折叠 + 标题,右边是双按钮 */
.group-header td {
  padding: 10px 16px !important;
  background: var(--bg-muted);
  border-top: 1px solid var(--border-soft);
}

.group-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text-secondary);
  font: inherit;
  font-weight: 600;
  cursor: pointer;
}

.group-toggle:hover {
  color: var(--text);
}

.group-toggle:hover .group-hint {
  color: var(--text-muted);
}

.group-title {
  font-size: 13px;
}

.group-hint {
  margin-left: 8px;
  font-size: 11.5px;
  font-weight: 400;
  color: var(--text-faint);
}

.group-actions {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  margin-left: 12px;
}
</style>