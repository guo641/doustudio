<script setup lang="ts">
defineProps<{
  open: boolean;
  title?: string;
  description?: string;
}>();

defineEmits<{ close: [] }>();
</script>

<template>
  <div v-if="open" class="dp-dialog-overlay" @click.self="$emit('close')">
    <div
      class="dp-dialog"
      role="dialog"
      :aria-label="title"
    >
      <div class="dp-dialog-head">
        <div class="dp-dialog-head-text">
          <b v-if="title">{{ title }}</b>
          <small v-if="description">{{ description }}</small>
          <slot name="header" />
        </div>
        <button type="button" class="dp-dialog-close" aria-label="关闭" @click="$emit('close')">×</button>
      </div>
      <div class="dp-dialog-body">
        <slot />
      </div>
      <div v-if="$slots.footer" class="dp-dialog-footer">
        <slot name="footer" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.dp-dialog-overlay {
  position: fixed;
  inset: 0;
  z-index: 40;
  display: grid;
  place-items: center;
  padding: 24px;
  background: rgba(4, 5, 8, 0.72);
  backdrop-filter: blur(10px);
  animation: dp-fade-in 0.15s ease;
}

.dp-dialog {
  width: min(560px, calc(100vw - 48px));
  max-height: calc(100vh - 48px);
  overflow: auto;
  padding: 22px 22px 18px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-xl);
  background: linear-gradient(180deg, #12151c 0%, #0e1015 100%);
  box-shadow: var(--shadow-lg), 0 0 0 1px rgba(255, 255, 255, 0.03) inset;
  animation: dp-scale-in 0.18s ease;
}

.dp-dialog-head {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  margin-bottom: 20px;
}

.dp-dialog-head-text {
  flex: 1;
  min-width: 0;
}

.dp-dialog-head-text b {
  display: block;
  font-size: 15px;
  font-weight: 650;
  letter-spacing: -0.01em;
}

.dp-dialog-head-text small {
  display: block;
  margin-top: 4px;
  color: var(--text-muted);
  font-size: 12px;
}

.dp-dialog-close {
  width: 32px;
  height: 32px;
  display: grid;
  place-items: center;
  padding: 0;
  border: 0;
  border-radius: var(--r-md);
  background: transparent;
  color: var(--text-muted);
  font-size: 20px;
  line-height: 1;
  flex-shrink: 0;
}

.dp-dialog-close:hover {
  background: var(--bg-hover);
  color: var(--text);
}

.dp-dialog-body {
  min-width: 0;
}

.dp-dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  margin-top: 18px;
  padding-top: 16px;
  border-top: 1px solid var(--border);
}

@keyframes dp-fade-in {
  from { opacity: 0; }
  to { opacity: 1; }
}

@keyframes dp-scale-in {
  from { opacity: 0; transform: scale(0.96) translateY(6px); }
  to { opacity: 1; transform: scale(1) translateY(0); }
}
</style>
