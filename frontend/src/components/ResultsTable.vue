<script setup lang="ts">
import { Download, ExternalLink, Link2, Film } from '@lucide/vue';
import { DpEmpty, DpLink, DpPanel, DpTable } from '@/ui';

type ResultTask = {
  id: string;
  prompt: string;
  model: string;
  ratio: string;
  duration: number;
  account_name?: string;
  status: string;
  result_url?: string;
  created_at: string;
};

defineProps<{ tasks: ResultTask[] }>();

const models: Record<string, string> = {
  'seedance_v2.0_std': '2.0',
  'seedance_v2.0': '2.0 Fast',
  'seedance_v2.0_mini': '2.0 Mini',
};

async function copy(value: string) {
  await navigator.clipboard?.writeText(value);
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
        <tr v-for="task in tasks" :key="task.id">
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
              <DpLink v-if="task.result_url" :href="task.result_url" download>
                <Download :size="12" />
                无水印下载
              </DpLink>
              <DpLink v-if="task.result_url" as-button @click="copy(task.result_url!)">
                <Link2 :size="12" />
                复制链接
              </DpLink>
            </div>
          </td>
        </tr>
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
</style>
