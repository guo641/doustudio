<script setup lang="ts">
import { computed, ref } from 'vue';
import { Clapperboard, RotateCcw, ExternalLink, Download } from '@lucide/vue';
import { DpBadge, DpButton, DpEmpty, DpLink, DpPanel, DpSearchInput, DpSelect, DpTable, DpTag } from '@/ui';

type VideoTaskRow = {
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
  error?: string;
  quota_used?: number;
  quota_total?: number;
  created_at: string;
};

const props = defineProps<{ tasks: VideoTaskRow[] }>();
defineEmits<{ retry: [task: VideoTaskRow] }>();

const query = ref('');
const status = ref('');
const account = ref('');
const expanded = ref(new Set<string>());

const statusLabels: Record<string, string> = {
  queued: '排队中',
  starting: '分配账号',
  generating: '生成中',
  resolving: '获取无水印',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
};

const modelLabels: Record<string, string> = {
  'seedance_v2.0_std': '2.0',
  'seedance_v2.0': '2.0 Fast',
  'seedance_v2.0_mini': '2.0 Mini',
};
const modeLabels: Record<string, string> = {
  t2v: '文生',
  i2v: '图生',
};

const accounts = computed(
  () => [...new Set(props.tasks.map((task) => task.account_name).filter(Boolean))] as string[],
);

const visibleTasks = computed(() => {
  const needle = query.value.trim().toLocaleLowerCase();
  return props.tasks.filter(
    (task) =>
      (!needle || task.prompt.toLocaleLowerCase().includes(needle)) &&
      (!status.value || task.status === status.value) &&
      (!account.value || task.account_name === account.value),
  );
});

function toggle(id: string) {
  const next = new Set(expanded.value);
  next.has(id) ? next.delete(id) : next.add(id);
  expanded.value = next;
}

function formatTime(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return '—';
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date);
}

function paramsText(task: VideoTaskRow) {
  return `${modelLabels[task.model] || task.model} · ${task.duration}s · ${task.ratio}`;
}
</script>

<template>
  <DpPanel>
    <template #filters>
      <DpSearchInput v-model="query" aria-label="搜索任务" placeholder="搜索任务描述…" />
      <DpSelect v-model="status" compact aria-label="状态筛选">
        <option value="">全部状态</option>
        <option v-for="(label, key) in statusLabels" :key="key" :value="key">{{ label }}</option>
      </DpSelect>
      <DpSelect v-model="account" compact aria-label="账号筛选">
        <option value="">全部账号</option>
        <option v-for="name in accounts" :key="name" :value="name">{{ name }}</option>
      </DpSelect>
      <span class="count">{{ visibleTasks.length }} 个任务</span>
    </template>

    <DpTable min-width="720px">
      <thead>
        <tr>
          <th style="width: 100px">状态</th>
          <th>任务</th>
          <th style="width: 120px">账号</th>
          <th style="width: 100px">时间</th>
          <th style="width: 180px">操作</th>
        </tr>
      </thead>
      <tbody>
        <template v-for="task in visibleTasks" :key="task.id">
          <tr>
            <td>
              <DpBadge :tone="task.status" dot>
                {{ statusLabels[task.status] || task.status }}
              </DpBadge>
            </td>
            <td class="task-cell">
              <button
                class="prompt-btn"
                :aria-label="`展开任务 ${task.id}`"
                :title="task.prompt"
                @click="toggle(task.id)"
              >
                {{ task.prompt }}
              </button>
              <div class="meta">
                <DpTag>{{ modeLabels[task.mode || 't2v'] || '文生' }}</DpTag>
                <DpTag v-if="(task.mode || 't2v') === 'i2v' && task.image_count" tone="success">
                  {{ task.image_count }} 图
                </DpTag>
                <span class="params">{{ paramsText(task) }}</span>
              </div>
            </td>
            <td class="account">{{ task.account_name || '等待分配' }}</td>
            <td class="time">{{ formatTime(task.created_at) }}</td>
            <td class="actions">
              <template v-if="task.result_url">
                <DpLink :href="task.result_url" external>
                  <ExternalLink :size="12" />
                  预览
                </DpLink>
                <DpLink :href="task.result_url" download>
                  <Download :size="12" />
                  下载
                </DpLink>
              </template>
              <DpButton
                v-if="task.status === 'failed'"
                size="sm"
                :aria-label="`重试任务 ${task.id}`"
                @click="$emit('retry', task)"
              >
                <RotateCcw :size="12" />
                重试
              </DpButton>
              <span v-if="!task.result_url && task.status !== 'failed'" class="dash">—</span>
            </td>
          </tr>
          <tr v-if="expanded.has(task.id)" class="detail-row">
            <td colspan="5">
              <div class="detail-body">
                <p>{{ task.prompt }}</p>
                <p class="detail-meta">
                  {{ modeLabels[task.mode || 't2v'] || '文生' }}
                  <template v-if="(task.mode || 't2v') === 'i2v' && task.image_count">
                    · {{ task.image_count }} 张图
                  </template>
                  · {{ paramsText(task) }}
                  · {{ task.account_name || '未分配账号' }}
                </p>
                <p v-if="task.error" class="error">{{ task.error }}</p>
              </div>
            </td>
          </tr>
        </template>
        <tr v-if="!visibleTasks.length">
          <td colspan="5">
            <DpEmpty title="没有匹配的任务" description="尝试调整搜索或筛选条件，或新建一条视频任务。">
              <template #icon><Clapperboard :size="18" /></template>
            </DpEmpty>
          </td>
        </tr>
      </tbody>
    </DpTable>
  </DpPanel>
</template>

<style scoped>
.count {
  margin-left: auto;
  color: var(--text-muted);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}

.task-cell {
  min-width: 220px;
  padding-top: 10px !important;
  padding-bottom: 10px !important;
}

.prompt-btn {
  display: block;
  width: 100%;
  overflow: hidden;
  padding: 0;
  border: 0;
  background: transparent;
  color: var(--text);
  font: inherit;
  font-weight: 600;
  text-align: left;
  text-overflow: ellipsis;
  white-space: nowrap;
  cursor: pointer;
}

.prompt-btn:hover {
  color: #fff;
}

.meta {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px;
  margin-top: 5px;
}

.params,
.time {
  color: var(--text-muted);
  white-space: nowrap;
  font-size: 12px;
}

.account {
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.actions {
  white-space: nowrap;
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px;
}

.dash {
  color: var(--text-faint);
}

.detail-row td {
  height: auto !important;
  padding: 0 !important;
  background: var(--bg-muted);
}

.detail-body {
  padding: 14px 16px 16px 116px;
}

.detail-body p {
  max-width: 850px;
  margin: 0;
  color: var(--text-secondary);
  line-height: 1.65;
  white-space: pre-wrap;
}

.detail-body .detail-meta {
  margin-top: 8px;
  color: var(--text-muted);
  font-size: 12px;
}

.detail-body .error {
  margin-top: 10px;
  padding: 8px 12px;
  border-left: 2px solid var(--danger);
  border-radius: 0 var(--r-sm) var(--r-sm) 0;
  background: var(--danger-soft);
  color: var(--danger-text);
}
</style>
