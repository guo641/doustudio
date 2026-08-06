<script setup lang="ts">
import { onMounted, ref } from 'vue';
import { Database, Save, Download } from '@lucide/vue';
import { backupDatabase, checkUpdate, getSettings, saveSettings } from '../api';
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
  watermark_enabled: boolean;
  watermark_uid: string;
  watermark_key: string;
  // v0.2.22 Q1:豆包拒绝后改写 prompt 重试次数,0 = 关闭(沿用 v0.2.21);1-3 = 最大改写次数。
  max_reject_retries: number;
  // v0.2.22 Q2:视频生成时 Chromium 窗口是否可见,默认隐藏(opt-in)。
  runner_window_visible: boolean;
  // v0.2.27:每个任务等待豆包生成的最长时长(分钟)。超时未成功自动退还额度。
  default_timeout_minutes: number;
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

const currentVersion = ref('未知');
const latestVersion = ref('');
const releaseUrl = ref('');
const hasUpdate = ref(false);
const updateNote = ref('');
const checkingUpdate = ref(false);

onMounted(async () => {
  try {
    const health = await fetch('/api/health').then((r) => r.json());
    if (health?.version) currentVersion.value = health.version;
  } catch {
    /* 离线模式,version 维持未知 */
  }
});

async function onCheckUpdate() {
  checkingUpdate.value = true;
  try {
    const info = await checkUpdate();
    latestVersion.value = info.latest_version;
    releaseUrl.value = info.release_url;
    hasUpdate.value = info.has_update;
    updateNote.value = info.has_update
      ? `发现新版本 ${info.latest_version}(当前 ${info.current_version})`
      : `已是最新版本 ${info.latest_version}`;
  } catch (e) {
    message.value = e instanceof Error ? e.message : '检查更新失败';
  } finally {
    checkingUpdate.value = false;
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
        <!-- v0.2.23:默认改为 2,拒绝类常见改写一次就过;想完全关闭显式设 0 -->
        <DpField label="豆包拒绝改写重试" for-id="setting-max-reject-retries">
          <DpInput
            id="setting-max-reject-retries"
            v-model.number="settings.max_reject_retries"
            type="number"
            :min="0"
            :max="3"
          />
          <span class="watermark-hint">0=关闭(失败立即报错);1-3=改写最大重试次数(默认 2,quota 限流不会触发)</span>
        </DpField>
        <!-- v0.2.24 Q2:生成时 Chromium 窗口可见,默认开启(用户反馈看不到
             浏览器在工作)。Playwright launch_persistent_context 创建后无法
             改 window-position,改动只对新 profile_dir / 重启进程生效。 -->
        <DpField label="显示 Chromium 窗口" for-id="setting-runner-window-visible" span2>
          <input
            id="setting-runner-window-visible"
            v-model="settings.runner_window_visible"
            type="checkbox"
            class="checkbox"
          />
          <span class="watermark-hint">
            开启后,视频生成时 Chromium 窗口会显示在屏幕 (80,80)。用户视角:能
            看到浏览器在跑、确认有在生成。关闭则窗口放屏幕外 (-2000,-2000)。
            可能被风控识别为异常登录态,生产建议关闭。修改后只对新启动的
            profile / 重启进程生效。
          </span>
        </DpField>
        <!-- v0.2.27:每个任务等待豆包生成的最长时长。超过该时间仍未成功 →
             任务回 queued,quota 自动退还(不再误扣)。范围 1-20 分钟,
             默认 7 —— 超时上限 20 是产品决策:再长会拖慢整批任务周转。 -->
        <DpField label="任务超时(分钟)" for-id="setting-default-timeout-minutes">
          <DpInput
            id="setting-default-timeout-minutes"
            v-model.number="settings.default_timeout_minutes"
            type="number"
            :min="1"
            :max="20"
          />
          <span class="watermark-hint">
            1-20 分钟,默认 7。每个任务等待豆包生成的最长时间,超时未成功将自动退还额度。
            修改后对下一个提交的任务生效,正在跑的任务不受影响。
          </span>
        </DpField>
      </div>
    </DpCard>

    <DpCard title="视频默认值" description="新建任务时自动填入的参数">
      <div class="fields">
        <DpField label="默认模型">
          <DpSelect v-model="settings.default_model">
            <!-- v0.2.11:去掉 seedance_v2.0(收费模型),std 改名为 Fast -->
            <option value="seedance_v2.0_mini">Seedance Mini</option>
            <option value="seedance_v2.0_std">Seedance Fast</option>
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

    <DpCard wide title="去水印(zhuceka)" description="视频生成后自动调 https://api.zhuceka.cn 去除水印,失败不影响主任务状态">
      <div class="fields">
        <DpField label="启用去水印" for-id="setting-watermark-enabled">
          <input
            id="setting-watermark-enabled"
            v-model="settings.watermark_enabled"
            type="checkbox"
            class="checkbox"
          />
        </DpField>
        <DpField label="UID" for-id="setting-watermark-uid">
          <DpInput
            id="setting-watermark-uid"
            v-model="settings.watermark_uid"
            placeholder="zhuceka 用户 UID"
          />
        </DpField>
        <DpField label="KEY" for-id="setting-watermark-key" span2>
          <DpInput
            id="setting-watermark-key"
            v-model="settings.watermark_key"
            type="password"
            placeholder="zhuceka 接口 KEY"
          />
        </DpField>
        <DpField span2>
          <span class="watermark-hint">
            开启后,每个视频任务生成完成时,会自动用上述 KEY 调
            <code>https://api.zhuceka.cn/home/api?type=dsp&uid=...&key=...&url=...</code>
            把无水印链接写到任务的「clean_video_url」字段。
          </span>
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

    <DpCard wide title="检查更新" description="自动检查 GitHub Releases 是否有新版本">
      <div class="fields">
        <DpField label="当前版本" span2>
          <DpInput :model-value="currentVersion" readonly />
        </DpField>
        <DpField label="最新版本" span2>
          <DpInput :model-value="latestVersion || '尚未检查'" readonly />
        </DpField>
        <DpField span2 v-if="updateNote">
          <span class="watermark-hint">{{ updateNote }}</span>
        </DpField>
      </div>
      <template #footer>
        <DpButton @click="onCheckUpdate" :disabled="checkingUpdate">
          <Download :size="14" />
          {{ checkingUpdate ? '检查中…' : '检查更新' }}
        </DpButton>
        <a
          v-if="releaseUrl && hasUpdate"
          :href="releaseUrl"
          target="_blank"
          rel="noopener"
          class="release-link"
        >
          下载 {{ latestVersion }} →
        </a>
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

.checkbox {
  width: 18px;
  height: 18px;
  cursor: pointer;
  accent-color: var(--accent, #3b82f6);
}

.watermark-hint {
  color: var(--text-muted);
  font-size: 12.5px;
  line-height: 1.6;
}

.watermark-hint code {
  background: var(--bg-elev, #f3f4f6);
  padding: 1px 6px;
  border-radius: 4px;
  font-size: 11.5px;
}

.release-link {
  margin-left: 12px;
  color: var(--accent, #3b82f6);
  text-decoration: none;
  font-size: 12.5px;
  font-weight: 500;
}

.release-link:hover {
  text-decoration: underline;
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
