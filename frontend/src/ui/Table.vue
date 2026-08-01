<script setup lang="ts">
withDefaults(
  defineProps<{
    minWidth?: string;
  }>(),
  {
    minWidth: '720px',
  },
);
</script>

<template>
  <div class="dp-table-scroll">
    <table class="dp-table" :style="{ minWidth }">
      <slot />
    </table>
  </div>
</template>

<style scoped>
.dp-table-scroll {
  overflow: auto;
}

.dp-table {
  width: 100%;
  border-collapse: collapse;
  table-layout: fixed;
}

.dp-table :deep(th) {
  height: 36px;
  padding: 0 14px;
  border-bottom: 1px solid var(--border);
  background: var(--bg-muted);
  color: var(--text-muted);
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 0.04em;
  text-align: left;
  white-space: nowrap;
}

.dp-table :deep(td) {
  height: 58px;
  padding: 0 14px;
  border-bottom: 1px solid var(--border);
  color: var(--text-secondary);
  font-size: 12.5px;
  vertical-align: middle;
}

.dp-table :deep(tbody tr:last-child td) {
  border-bottom: 0;
}

.dp-table :deep(tbody tr:not(.detail-row):hover td) {
  background: var(--bg-hover);
}
</style>
