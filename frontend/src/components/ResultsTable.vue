<script setup lang="ts">
import { Download, ExternalLink, Link2, Film, Sparkles } from '@lucide/vue';
import { DpEmpty, DpLink, DpPanel, DpTable } from '@/ui';
import DownloadButton from '@/ui/DownloadButton.vue';

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
              <!-- 下载走 DownloadButton(走 Blob 中转),
                   避免 WebView2/Chromium 在 cross-origin URL 上把 <a download>
                   静默降级成导航离开应用。 -->
              <DownloadButton
                v-if="pickDownloadUrl(task)"
                :href="pickDownloadUrl(task)!"
                :filename="`doubao-${task.id}${hasCleanVideo(task) ? '-clean' : ''}.mp4`"
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
</style>