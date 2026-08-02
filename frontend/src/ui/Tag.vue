<script setup lang="ts">
withDefaults(
  defineProps<{
    tone?: 'accent' | 'success' | 'muted' | 'info' | 'warning' | 'danger';
  }>(),
  { tone: 'accent' },
);
</script>

<template>
  <span class="dp-tag" :class="`is-${tone}`"><slot /></span>
</template>

<style scoped>
.dp-tag {
  display: inline-flex;
  padding: 2px 6px;
  border-radius: 5px;
  font-size: 10.5px;
  font-weight: 600;
}

.dp-tag.is-accent {
  background: var(--accent-soft);
  color: var(--accent-text);
}
.dp-tag.is-success {
  background: var(--success-soft);
  color: var(--success-text);
}
.dp-tag.is-muted {
  background: var(--bg-muted);
  color: var(--text-muted);
}
.dp-tag.is-info {
  background: rgba(99, 102, 241, 0.12);
  color: #6366f1;
}
.dp-tag.is-warning {
  background: rgba(245, 158, 11, 0.14);
  color: #b45309;
}
.dp-tag.is-danger {
  background: rgba(239, 68, 68, 0.14);
  color: #b91c1c;
}
</style>