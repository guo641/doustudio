<script setup lang="ts">
import { computed, onBeforeUnmount, watch } from 'vue';
import { AlertCircle, CheckCircle2, Info, Loader2, X } from '@lucide/vue';

const props = withDefaults(
  defineProps<{
    message?: string;
    type?: string;
    /** Auto-dismiss after ms. `0` keeps it open. Omit for smart defaults. */
    duration?: number | null;
  }>(),
  {
    message: '',
    type: 'succeeded',
    duration: null,
  },
);

const emit = defineEmits<{ close: [] }>();

const ERROR_TYPES = new Set(['failed', 'cancelled', 'timed_out', 'error']);
const LOADING_TYPES = new Set([
  'launching',
  'waiting_qr',
  'waiting_confirm',
  'loading',
  'pending',
  'info',
]);

// v0.2.35:warning(橙色)—— 任务部分入队等"非致命但需告知"的场景。
const WARNING_TYPES = new Set(['warning']);

const variant = computed(() => {
  if (ERROR_TYPES.has(props.type)) return 'error';
  if (LOADING_TYPES.has(props.type)) return 'info';
  if (WARNING_TYPES.has(props.type)) return 'warning';
  if (props.type === 'succeeded' || props.type === 'success') return 'success';
  return 'success';
});

const resolvedDuration = computed(() => {
  if (props.duration != null) return props.duration;
  if (variant.value === 'info') return 0;
  if (variant.value === 'error') return 5000;
  if (variant.value === 'warning') return 6000;
  return 3500;
});

const Icon = computed(() => {
  if (variant.value === 'error') return AlertCircle;
  if (variant.value === 'warning') return AlertCircle;
  if (variant.value === 'info') return Loader2;
  if (variant.value === 'success') return CheckCircle2;
  return Info;
});

let timer: ReturnType<typeof setTimeout> | undefined;

function clearTimer() {
  if (timer) {
    clearTimeout(timer);
    timer = undefined;
  }
}

function scheduleDismiss() {
  clearTimer();
  const ms = resolvedDuration.value;
  if (!props.message || ms <= 0) return;
  timer = setTimeout(() => emit('close'), ms);
}

function onClose() {
  clearTimer();
  emit('close');
}

watch(
  () => [props.message, props.type, props.duration] as const,
  () => scheduleDismiss(),
  { immediate: true },
);

onBeforeUnmount(clearTimer);
</script>

<template>
  <Transition name="toast">
    <div
      v-if="message"
      class="toast"
      :class="[type, `is-${variant}`]"
      role="status"
      aria-live="polite"
    >
      <span class="toast-icon" :class="{ spin: variant === 'info' }">
        <component :is="Icon" :size="16" :stroke-width="2" />
      </span>
      <span class="toast-message">{{ message }}</span>
      <button type="button" class="toast-close" aria-label="关闭通知" @click="onClose">
        <X :size="14" :stroke-width="2" />
      </button>
    </div>
  </Transition>
</template>

<style scoped>
.toast {
  position: fixed;
  right: 24px;
  bottom: 24px;
  z-index: 50;
  display: flex;
  align-items: flex-start;
  gap: 10px;
  min-width: 280px;
  max-width: min(420px, calc(100vw - 48px));
  padding: 12px 12px 12px 14px;
  border: 1px solid transparent;
  border-radius: var(--r-md, 9px);
  box-shadow: var(--shadow-md, 0 8px 24px rgba(0, 0, 0, 0.35));
  backdrop-filter: blur(10px);
}

.toast.is-success {
  border-color: rgba(62, 207, 142, 0.28);
  background: rgba(15, 26, 20, 0.94);
  color: var(--success-text, #6ee7a8);
}

.toast.is-error,
.toast.failed,
.toast.cancelled,
.toast.timed_out {
  border-color: rgba(240, 113, 90, 0.32);
  background: rgba(26, 16, 14, 0.94);
  color: var(--danger-text, #f59a86);
}

.toast.is-info,
.toast.launching,
.toast.waiting_qr,
.toast.waiting_confirm {
  border-color: rgba(124, 106, 245, 0.32);
  background: rgba(18, 16, 28, 0.94);
  color: var(--accent-text, #c4bbff);
}

/* v0.2.35:warning(橙色)—— 部分入队等"非致命但需告知"的场景。 */
.toast.is-warning,
.toast.warning {
  border-color: rgba(245, 158, 11, 0.32);
  background: rgba(28, 22, 12, 0.94);
  color: var(--warning-text, #fbbf24);
}

.toast-icon {
  display: grid;
  place-items: center;
  width: 20px;
  height: 20px;
  margin-top: 1px;
  flex-shrink: 0;
  opacity: 0.95;
}

.toast-icon.spin {
  animation: toast-spin 0.9s linear infinite;
}

.toast-message {
  flex: 1;
  min-width: 0;
  padding-top: 1px;
  font-size: 13px;
  line-height: 1.45;
  word-break: break-word;
}

.toast-close {
  display: grid;
  place-items: center;
  width: 24px;
  height: 24px;
  margin: -2px -2px 0 0;
  padding: 0;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: currentColor;
  opacity: 0.55;
  flex-shrink: 0;
  cursor: pointer;
}

.toast-close:hover {
  opacity: 1;
  background: rgba(255, 255, 255, 0.06);
}

.toast-enter-active,
.toast-leave-active {
  transition:
    opacity 0.2s ease,
    transform 0.2s ease;
}

.toast-enter-from,
.toast-leave-to {
  opacity: 0;
  transform: translateY(10px);
}

@keyframes toast-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>
