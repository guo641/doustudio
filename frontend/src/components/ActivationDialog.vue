<!--
  v0.3.0:激活闸门 — 用户在首次启动 / 激活码到期时看到的全屏弹窗。

  设计目标:
    1. 关闭键 / 点击遮罩关闭 / Escape —— 全部失效。强制用户在「激活」和「退出」之间二选一,
       否则离开激活态就再也进不来(后端 /api/accounts 全部 403)。
    2. 用户粘贴激活码时自动 strip 空白 / dash / 等号 —— 不少用户把激活码按 4-char 切分,
       后端接受的是不带 padding 的 base32 字符串,前端去掉非字母数字字符即可。
    3. 机器码展示 + 复制 —— 开发者要让用户发回机器码才能签发,所以复制要稳。
       用 navigator.clipboard(异步);权限拒绝时静默(用户可手动框选)。
    4. expired 状态输入框禁用 —— 没法「再输一次就续期」,必须让开发者签发新码。
       只留「退出」+「复制机器码」两个出口。

  Props:
    state: 'loading' | 'needs-activation' | 'expired' | 'revoked'
    fingerprint: 64-hex HMAC,激活码生成时绑定的就是它
    expires_at: 仅 expired 状态有值(Unix 秒)

  Emits:
    activated — 激活成功,App 切到主 UI
    quit — 用户点「退出」,App 调 /api/license/quit → os._exit(0)
-->
<script setup lang="ts">
import { computed, ref } from 'vue';
import { Copy, KeyRound, ShieldAlert } from '@lucide/vue';
import { DpButton, DpDialog, DpInput } from '@/ui';
import { activateLicense, type LicenseState } from '../api';

const props = defineProps<{
  state: LicenseState;
  fingerprint: string;
  // null/undefined 在 'loading' 态下出现,后端响应回来之前 fingerprint 也没值。
  // 写成 optional + default,避免 Vue 3 在初始 mount 时报「Missing required prop」。
  expires_at?: number | null;
}>();

const emit = defineEmits<{
  (e: 'activated'): void;
  (e: 'quit'): void;
}>();

const code = ref('');
const submitting = ref(false);
const error = ref('');

const title = computed(() => {
  if (props.state === 'revoked') return '授权已被撤销';
  if (props.state === 'expired') return '激活码已过期';
  if (props.state === 'loading') return '正在校验激活';
  return '请输入激活码';
});

const description = computed(() => {
  if (props.state === 'revoked') {
    return '当前授权已被管理员撤销，无法继续使用。请联系管理员获取新的激活码。';
  }
  if (props.state === 'expired') {
    return '当前激活码已到期。请联系开发者续期,并将下方机器码一并发给开发者。';
  }
  if (props.state === 'loading') {
    return '正在读取机器码并校验激活状态…';
  }
  return '请将开发者提供的激活码粘贴到下方输入框。激活成功后将自动进入软件主界面。';
});

// 输入框在 expired / submitting 时禁用 —— 防用户复制粘贴碰运气。
const inputDisabled = computed(
  () => props.state === 'expired' || props.state === 'loading' || submitting.value,
);

async function onSubmit() {
  if (submitting.value) return;
  // 用户复制时常常按 4 字符分组,或者包含换行 / 短横线 —— 后端只接受裸 base32。
  const clean = code.value.replace(/[\s\-=]+/g, '');
  if (clean.length < 10) {
    error.value = '激活码格式错误(至少 10 字符)';
    return;
  }
  submitting.value = true;
  error.value = '';
  try {
    await activateLicense(clean);
    emit('activated');
  } catch (e) {
    // 后端 400 detail 直接显示,通常是 「与本机不匹配」 / 「已过期」 / 「签名验证失败」之一。
    error.value = e instanceof Error ? e.message : '激活失败,请检查激活码';
  } finally {
    submitting.value = false;
  }
}

async function copyFingerprint() {
  if (!props.fingerprint) return;
  try {
    await navigator.clipboard.writeText(props.fingerprint);
  } catch {
    /* 浏览器拒绝权限 / 非安全上下文 → 静默,用户可手动框选 */
  }
}

function onQuit() {
  emit('quit');
}
</script>

<template>
  <DpDialog :open="true" :title="title" :description="description" @close="() => {}">
    <template #default>
      <div class="activation-content">
        <div v-if="state === 'loading'" class="loading-state">
          正在读取机器码…
        </div>
        <template v-else>
          <div class="machine-code">
            <label>您的机器码</label>
            <div class="machine-code-row">
              <input
                type="text"
                readonly
                :value="fingerprint"
                class="machine-code-input"
                aria-label="机器码"
                @focus="(e: FocusEvent) => (e.target as HTMLInputElement).select()"
                @click="(e: MouseEvent) => (e.target as HTMLInputElement).select()"
              />
              <DpButton size="sm" variant="ghost" :title="`复制机器码: ${fingerprint}`" @click="copyFingerprint">
                <Copy :size="13" :stroke-width="2" />
                复制
              </DpButton>
            </div>
          </div>

          <div v-if="state === 'expired'" class="expired-info">
            <ShieldAlert :size="14" :stroke-width="2" />
            <span>
              到期时间:{{ expires_at ? new Date(expires_at * 1000).toLocaleString('zh-CN') : '未知' }}
            </span>
          </div>

          <div v-if="state === 'revoked'" class="revoked-info" role="alert">
            <ShieldAlert :size="16" :stroke-width="2" />
            <div>
              <strong>授权已被撤销</strong>
              <p>请联系管理员获取新的激活码。</p>
            </div>
          </div>

          <div class="code-field">
            <label>
              <KeyRound :size="12" :stroke-width="2.25" />
              激活码
            </label>
            <DpInput
              v-model="code"
              placeholder="粘贴激活码(自动去空格 / 短横线)"
              :disabled="inputDisabled"
              class="code-input"
              aria-label="激活码"
              @keydown.enter="onSubmit"
            />
            <div v-if="error" class="error-msg" role="alert">{{ error }}</div>
          </div>
        </template>
      </div>
    </template>

    <template #footer>
      <template v-if="state === 'expired'">
        <DpButton variant="ghost" @click="onQuit">退出软件</DpButton>
        <DpButton variant="primary" @click="copyFingerprint">
          <Copy :size="13" :stroke-width="2" />
          复制机器码
        </DpButton>
      </template>
      <template v-else-if="state === 'loading'">
        <!-- loading 阶段不显按钮,等 status 返回 -->
      </template>
      <template v-else>
        <DpButton variant="ghost" :disabled="submitting" @click="onQuit">退出</DpButton>
        <DpButton variant="primary" :disabled="submitting || !code.trim()" @click="onSubmit">
          {{ submitting ? '正在激活…' : '激活' }}
        </DpButton>
      </template>
    </template>
  </DpDialog>

  <!-- 作者信息:用户首次激活时给一个可信的联系方式,免得找不到人 -->
  <div class="activation-footer" aria-label="作者信息">
    <span class="footer-label">开发者</span>
    <span class="footer-author">天辰</span>
    <span class="footer-sep">·</span>
    <span class="footer-contact">微信 ChatGPT02468</span>
    <span class="footer-sep">·</span>
    <span class="footer-hint">续期或换机请把上方机器码发给开发者</span>
  </div>
</template>

<style scoped>
.activation-content {
  display: flex;
  flex-direction: column;
  gap: 18px;
}
.loading-state {
  padding: 12px 0;
  color: var(--text-muted);
  font-size: 13px;
}
.machine-code label,
.code-field label {
  display: flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-weight: 600;
  color: var(--text-muted);
  margin-bottom: 6px;
}
.machine-code-row {
  display: flex;
  gap: 8px;
  align-items: stretch;
}
.machine-code-input {
  flex: 1;
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--border);
  border-radius: var(--r-md);
  background: var(--bg-soft);
  color: var(--text);
  font-family: ui-monospace, 'JetBrains Mono', Menlo, Consolas, monospace;
  font-size: 12px;
  letter-spacing: 0.02em;
  cursor: text;
}
.machine-code-input:focus {
  outline: none;
  border-color: var(--border-focus);
}
.hint {
  display: block;
  margin-top: 6px;
  font-size: 11px;
  color: var(--text-faint);
  line-height: 1.45;
}
.expired-info {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 10px 12px;
  border-radius: var(--r-md);
  background: var(--bg-soft);
  font-size: 12px;
  color: var(--text-muted);
}
.revoked-info {
  display: flex;
  align-items: flex-start;
  gap: 9px;
  padding: 11px 12px;
  border: 1px solid color-mix(in srgb, var(--text-failed, #e85c3c) 35%, transparent);
  border-radius: var(--r-md);
  background: color-mix(in srgb, var(--text-failed, #e85c3c) 8%, var(--bg-soft));
  color: var(--text-failed, #e85c3c);
  font-size: 12px;
  line-height: 1.45;
}
.revoked-info strong {
  display: block;
  margin-bottom: 2px;
}
.revoked-info p {
  margin: 0;
  color: var(--text-muted);
}
.code-input {
  font-family: ui-monospace, 'JetBrains Mono', Menlo, Consolas, monospace;
  font-size: 12px;
  letter-spacing: 0.02em;
}
.error-msg {
  margin-top: 6px;
  font-size: 12px;
  color: var(--text-failed, #e85c3c);
  line-height: 1.4;
}
/* 激活窗不可关闭 —— 隐藏 DpDialog 自带的 × 按钮 */
:deep(.dp-dialog-close) {
  display: none;
}
/* 作者信息条 —— 写在 DpDialog 外部,让用户任何状态下都能找到开发者 */
.activation-footer {
  position: fixed;
  left: 50%;
  bottom: 18px;
  transform: translateX(-50%);
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 16px;
  border-radius: 999px;
  background: var(--bg-soft);
  border: 1px solid var(--border);
  font-size: 11px;
  color: var(--text-muted);
  box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
  pointer-events: auto;
  z-index: 999;
}
.footer-label {
  color: var(--text-faint);
  font-weight: 500;
}
.footer-author {
  color: var(--text);
  font-weight: 600;
  font-family: ui-monospace, 'JetBrains Mono', Menlo, Consolas, monospace;
}
.footer-link {
  color: var(--accent, #4a6cf7);
  text-decoration: none;
}
.footer-link:hover {
  text-decoration: underline;
}
.footer-sep {
  color: var(--text-faint);
  opacity: 0.6;
}
.footer-hint {
  color: var(--text-faint);
}
</style>
