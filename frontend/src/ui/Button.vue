<script setup lang="ts">
withDefaults(
  defineProps<{
    variant?: 'default' | 'primary' | 'solid' | 'ghost' | 'danger';
    size?: 'md' | 'sm';
    type?: 'button' | 'submit' | 'reset';
    disabled?: boolean;
  }>(),
  {
    variant: 'default',
    size: 'md',
    type: 'button',
    disabled: false,
  },
);
</script>

<template>
  <button
    class="dp-btn"
    :class="[`is-${variant}`, `is-${size}`]"
    :type="type"
    :disabled="disabled"
  >
    <slot />
  </button>
</template>

<style scoped>
.dp-btn {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  height: 36px;
  padding: 0 12px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-elevated);
  color: var(--text-secondary);
  font-size: 12.5px;
  font-weight: 550;
  white-space: nowrap;
  transition: background 0.12s ease, border-color 0.12s ease, color 0.12s ease;
}

.dp-btn:hover:not(:disabled) {
  background: var(--bg-hover);
  border-color: #3a4050;
  color: var(--text);
}

.dp-btn.is-primary {
  border-color: transparent;
  background: linear-gradient(180deg, #8b7cf7 0%, #6d5df7 100%);
  color: #fff;
  font-weight: 650;
  box-shadow: 0 1px 0 rgba(255, 255, 255, 0.12) inset, 0 2px 8px rgba(109, 93, 247, 0.3);
}

.dp-btn.is-primary:hover:not(:disabled) {
  background: linear-gradient(180deg, #9789f8 0%, #7a6af8 100%);
  border-color: transparent;
  color: #fff;
}

.dp-btn.is-solid {
  border: 0;
  background: #f0f1f3;
  color: #111;
  font-weight: 650;
}

.dp-btn.is-solid:hover:not(:disabled) {
  background: #fff;
  color: #111;
}

.dp-btn.is-ghost {
  border-color: transparent;
  background: transparent;
  color: var(--text-muted);
}

.dp-btn.is-ghost:hover:not(:disabled) {
  background: var(--bg-hover);
  color: var(--text);
}

.dp-btn.is-danger {
  color: var(--danger-text);
}

.dp-btn.is-danger:hover:not(:disabled) {
  border-color: rgba(240, 113, 90, 0.35);
  background: var(--danger-soft);
  color: var(--danger-text);
}

.dp-btn.is-sm {
  height: 28px;
  padding: 0 9px;
  font-size: 11.5px;
  border-radius: var(--r-sm);
}
</style>
