<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from 'vue';
import { RefreshCw, Trash2, ScrollText } from '@lucide/vue';
import { clearLogs, listLogs } from '../api';
import { DpBadge, DpButton, DpEmpty, DpPanel, DpSearchInput, DpSelect, DpTable } from '@/ui';

type Log = {
  id: number;
  level: string;
  module: string;
  event: string;
  message: string;
  created_at: string;
};

const logs = ref<Log[]>([]);
const level = ref('');
const query = ref('');
const error = ref('');
let timer: ReturnType<typeof setInterval> | undefined;

const visible = computed(() =>
  logs.value.filter(
    (row) =>
      (!level.value || row.level === level.value) &&
      (!query.value ||
        `${row.module} ${row.event} ${row.message}`.toLowerCase().includes(query.value.toLowerCase())),
  ),
);

async function refresh() {
  try {
    logs.value = await listLogs();
    error.value = '';
  } catch (e) {
    error.value = e instanceof Error ? e.message : '日志加载失败';
  }
}

async function clear() {
  if (!confirm('确定清空全部运行日志吗？')) return;
  await clearLogs();
  logs.value = [];
}

onMounted(() => {
  void refresh();
  timer = setInterval(refresh, 5000);
});

onBeforeUnmount(() => {
  if (timer) clearInterval(timer);
});
</script>

<template>
  <DpPanel>
    <template #filters>
      <DpSearchInput
        v-model="query"
        class="grow"
        aria-label="搜索日志"
        placeholder="搜索模块、事件或消息…"
      />
      <DpSelect v-model="level" compact aria-label="日志级别">
        <option value="">全部级别</option>
        <option v-for="value in ['DEBUG', 'INFO', 'WARNING', 'ERROR']" :key="value">{{ value }}</option>
      </DpSelect>
      <DpButton size="sm" @click="refresh">
        <RefreshCw :size="13" />
        刷新
      </DpButton>
      <DpButton size="sm" variant="danger" @click="clear">
        <Trash2 :size="13" />
        清空日志
      </DpButton>
    </template>

    <p v-if="error" class="error-banner">{{ error }}</p>

    <DpTable min-width="940px">
      <thead>
        <tr>
          <th style="width: 160px">时间</th>
          <th style="width: 90px">级别</th>
          <th style="width: 150px">模块</th>
          <th style="width: 140px">事件</th>
          <th>消息</th>
        </tr>
      </thead>
      <tbody>
        <tr v-if="!visible.length">
          <td colspan="5">
            <DpEmpty title="暂无日志" description="应用运行时产生的结构化日志会显示在这里。">
              <template #icon><ScrollText :size="18" /></template>
            </DpEmpty>
          </td>
        </tr>
        <tr v-for="row in visible" :key="row.id">
          <td class="time">{{ new Date(row.created_at).toLocaleString('zh-CN') }}</td>
          <td>
            <DpBadge :tone="row.level.toLowerCase()">{{ row.level }}</DpBadge>
          </td>
          <td class="mono">{{ row.module }}</td>
          <td class="event">{{ row.event }}</td>
          <td class="message" :title="row.message">{{ row.message }}</td>
        </tr>
      </tbody>
    </DpTable>
  </DpPanel>
</template>

<style scoped>
.grow {
  flex: 1;
  min-width: 220px;
}

.error-banner {
  margin: 0;
  padding: 10px 14px;
  border-bottom: 1px solid rgba(240, 113, 90, 0.2);
  background: var(--danger-soft);
  color: var(--danger-text);
  font-size: 12.5px;
}

.time {
  color: var(--text-muted);
  font-size: 12px;
  white-space: nowrap;
  font-variant-numeric: tabular-nums;
}

.mono {
  font-family: var(--mono);
  font-size: 11.5px;
  color: var(--text-secondary);
}

.event {
  color: var(--text-secondary);
}

.message {
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
  color: var(--text-secondary);
}
</style>
