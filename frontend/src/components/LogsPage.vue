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
const clearError = ref('');
let timer: ReturnType<typeof setInterval> | undefined;

/**
 * refresh / clear 的竞态保护:
 *
 * 1) 每次 refresh 给一个递增的 seq,只有当前 seq 对应的响应才能写回 logs。
 *    否则快速连续点刷新按钮时,前一次慢响应回来会盖掉最新数据。
 *
 * 2) clear 用一个 inflight flag 阻止 5 秒定时器在清空流程中途拉回老数据。
 *    时间线:
 *      t=0:  用户点清空 → 调 DELETE
 *      t=1:  5s 定时器触发 refresh → 拿到一份老 logs,正准备赋值
 *      t=2:  DELETE 完成 → logs = []
 *      t=3:  refresh 完成 → 把老 logs 写回 ← BUG,数据"复活"
 *    修法:clear 开始置 inflight=true,refresh 在 inflight=true 时
 *    不写回 logs;DELETE 完成 + 主动 refresh 后,清 inflight=false。
 */
let fetchSeq = 0;
let clearInFlight = false;

const visible = computed(() =>
  logs.value.filter(
    (row) =>
      (!level.value || row.level === level.value) &&
      (!query.value ||
        `${row.module} ${row.event} ${row.message}`.toLowerCase().includes(query.value.toLowerCase())),
  ),
);

async function refresh() {
  const mySeq = ++fetchSeq;
  try {
    const fresh = await listLogs();
    // clear 进行中时,丢弃本次结果,避免"复活"
    if (clearInFlight) return;
    if (mySeq !== fetchSeq) return; // 后到的旧响应
    logs.value = fresh;
    error.value = '';
  } catch (e) {
    if (mySeq !== fetchSeq) return;
    error.value = e instanceof Error ? e.message : '日志加载失败';
  }
}

async function clear() {
  if (!confirm('确定清空全部运行日志吗？')) return;
  clearInFlight = true;
  clearError.value = '';
  try {
    await clearLogs();
    // 主动拉一次最新数据(理论上就是空,但服务端可能有未提交缓冲)
    const fresh = await listLogs();
    logs.value = fresh;
  } catch (e) {
    clearError.value = e instanceof Error ? e.message : '日志清空失败';
  } finally {
    clearInFlight = false;
  }
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
    <p v-if="clearError" class="error-banner">{{ clearError }}</p>

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
