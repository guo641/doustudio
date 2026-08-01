<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { Database, Save } from '@lucide/vue';
import { backupDatabase, getSettings, saveSettings } from '../api';
import { DpButton, DpCard, DpEmpty, DpField, DpInput, DpSelect } from '@/ui';

type Settings = {
  max_concurrency: number;
  daily_quota: number;
  quota_reset_time: string;
  scheduler_strategy: string;
  default_model: string;
  default_duration: number;
  default_ratio: string;
  download_dir: string;
  log_level: string;
  log_retention_days: number;
  data_dir: string;
};

const settings = ref<Settings | null>(null);
const message = ref('');
const saving = ref(false);
const emit = defineEmits<{ saved: [value: Settings] }>();

onMounted(async () => {
  try {
    settings.value = await getSettings();
  } catch (e) {
    message.value = e instanceof Error ? e.message : '设置加载失败';
  }
});

async function save() {
  if (!settings.value) return;
  saving.value = true;
  try {
    const { data_dir, ...editable } = settings.value;
    settings.value = await saveSettings(editable);
    message.value = '设置已保存';
    emit('saved', settings.value!);
  } catch (e) {
    message.value = e instanceof Error ? e.message : '设置保存失败';
  } finally {
    saving.value = false;
  }
}

async function backup() {
  try {
    const result = await backupDatabase();
    message.value = `备份已保存：${result.path}`;
  } catch (e) {
    message.value = e instanceof Error ? e.message : '备份失败';
  }
}
</script>

<template>
  <div v-if="settings" class="settings-grid">
    <DpCard title="调度" description="控制账号池的并发与额度策略">
      <div class="fields">
        <DpField label="全局并发数">
          <DpInput v-model.number="settings.max_concurrency" type="number" :min="1" :max="10" />
        </DpField>
        <DpField label="每日额度" for-id="setting-daily-quota">
          <DpInput
            id="setting-daily-quota"
            v-model.number="settings.daily_quota"
            type="number"
            :min="1"
            :max="100"
          />
        </DpField>
        <DpField label="额度重置时间">
          <DpInput v-model="settings.quota_reset_time" type="time" />
        </DpField>
        <DpField label="调度策略">
          <DpSelect v-model="settings.scheduler_strategy">
            <option value="least_used">最少使用优先</option>
            <option value="round_robin">轮询</option>
          </DpSelect>
        </DpField>
      </div>
    </DpCard>

    <DpCard title="视频默认值" description="新建任务时自动填入的参数">
      <div class="fields">
        <DpField label="默认模型">
          <DpSelect v-model="settings.default_model">
            <option value="seedance_v2.0_mini">Seedance 2.0 Mini</option>
            <option value="seedance_v2.0">Seedance 2.0 Fast</option>
            <option value="seedance_v2.0_std">Seedance 2.0</option>
          </DpSelect>
        </DpField>
        <DpField label="默认时长">
          <DpSelect v-model.number="settings.default_duration">
            <option :value="5">5 秒</option>
            <option :value="10">10 秒</option>
          </DpSelect>
        </DpField>
        <DpField label="默认比例">
          <DpSelect v-model="settings.default_ratio">
            <option v-for="value in ['1:1', '3:4', '4:3', '9:16', '16:9', '21:9']" :key="value">
              {{ value }}
            </option>
          </DpSelect>
        </DpField>
      </div>
    </DpCard>

    <DpCard wide title="文件与日志" description="本地下载路径、日志级别与数据备份">
      <div class="fields">
        <DpField label="视频下载目录" span2>
          <DpInput v-model="settings.download_dir" />
        </DpField>
        <DpField label="日志级别">
          <DpSelect v-model="settings.log_level">
            <option v-for="value in ['DEBUG', 'INFO', 'WARNING', 'ERROR']" :key="value">
              {{ value }}
            </option>
          </DpSelect>
        </DpField>
        <DpField label="日志保留天数">
          <DpInput v-model.number="settings.log_retention_days" type="number" :min="1" :max="365" />
        </DpField>
        <DpField label="数据目录" span2>
          <DpInput :model-value="settings.data_dir" readonly />
        </DpField>
      </div>
      <template #footer>
        <DpButton @click="backup">
          <Database :size="14" />
          备份数据库
        </DpButton>
      </template>
    </DpCard>

    <div class="footer">
      <span class="footer-msg" :class="{ ok: message === '设置已保存' }">{{ message }}</span>
      <DpButton variant="primary" :disabled="saving" @click="save">
        <Save :size="14" />
        {{ saving ? '保存中…' : '保存设置' }}
      </DpButton>
    </div>
  </div>
  <div v-else class="loading-state">
    <DpEmpty :title="message || '正在加载设置…'" />
  </div>
</template>

<style scoped>
.settings-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}

.fields {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
}

.footer {
  grid-column: 1 / -1;
  display: flex;
  align-items: center;
  justify-content: flex-end;
  gap: 14px;
  padding-top: 4px;
}

.footer-msg {
  color: var(--text-muted);
  font-size: 12.5px;
}

.footer-msg.ok {
  color: var(--success-text);
}

.loading-state {
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  background: var(--bg-panel);
}

@media (max-width: 900px) {
  .settings-grid {
    grid-template-columns: 1fr;
  }

  .fields {
    grid-template-columns: 1fr;
  }
}
</style>
