<script setup lang="ts">
defineProps<{
  title?: string;
  subtitle?: string;
}>();
</script>

<template>
  <div class="dp-panel">
    <div v-if="title || $slots.toolbar || $slots.actions" class="dp-panel-toolbar">
      <div class="dp-panel-title">
        <b v-if="title">{{ title }}</b>
        <span v-if="subtitle">{{ subtitle }}</span>
        <slot name="toolbar" />
      </div>
      <div v-if="$slots.actions" class="dp-panel-actions">
        <slot name="actions" />
      </div>
    </div>
    <div v-if="$slots.filters" class="dp-panel-filters">
      <slot name="filters" />
    </div>
    <slot />
  </div>
</template>

<style scoped>
.dp-panel {
  border: 1px solid var(--border);
  background: var(--bg-panel);
  border-radius: var(--r-lg);
  overflow: hidden;
  box-shadow: var(--shadow-sm);
}

.dp-panel-toolbar {
  min-height: 52px;
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 0 16px;
  border-bottom: 1px solid var(--border);
  background: rgba(255, 255, 255, 0.01);
}

.dp-panel-title {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
}

.dp-panel-title b {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
}

.dp-panel-title span {
  color: var(--text-muted);
  font-size: 12px;
}

.dp-panel-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}

.dp-panel-filters {
  min-height: 52px;
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 10px 14px;
  border-bottom: 1px solid var(--border);
  background: rgba(255, 255, 255, 0.01);
  flex-wrap: wrap;
}
</style>
