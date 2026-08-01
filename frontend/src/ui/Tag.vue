<script setup lang="ts">
withDefaults(
  defineProps<{
    tone?: 'accent' | 'success' | 'muted';
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
</style>
