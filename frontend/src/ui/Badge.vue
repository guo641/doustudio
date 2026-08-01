<script setup lang="ts">
withDefaults(
  defineProps<{
    tone?: string;
    dot?: boolean;
  }>(),
  {
    tone: '',
    dot: false,
  },
);
</script>

<template>
  <mark class="dp-badge" :class="tone">
    <span v-if="dot">●</span>
    <slot />
  </mark>
</template>

<style scoped>
.dp-badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 4px 8px;
  border-radius: var(--r-sm);
  font-size: 11px;
  font-weight: 550;
  font-style: normal;
  white-space: nowrap;
  background: var(--bg-muted);
  color: var(--text-secondary);
}

.dp-badge.queued,
.dp-badge.starting {
  background: var(--warning-soft);
  color: var(--warning-text);
}

.dp-badge.generating,
.dp-badge.active,
.dp-badge.succeeded {
  background: var(--success-soft);
  color: var(--success-text);
}

.dp-badge.resolving {
  background: var(--purple-soft);
  color: var(--purple);
}

.dp-badge.failed,
.dp-badge.cancelled,
.dp-badge.error {
  background: var(--danger-soft);
  color: var(--danger-text);
}

.dp-badge.expired,
.dp-badge.warning {
  background: var(--warning-soft);
  color: var(--warning-text);
}

.dp-badge.debug {
  background: var(--bg-muted);
  color: var(--text-muted);
}

.dp-badge.info {
  background: var(--info-soft);
  color: var(--info-text);
}
</style>
