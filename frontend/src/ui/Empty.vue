<script setup lang="ts">
defineProps<{
  title?: string;
  description?: string;
}>();
</script>

<template>
  <div class="dp-empty">
    <div v-if="$slots.icon" class="dp-empty-icon">
      <slot name="icon" />
    </div>
    <b v-if="title">{{ title }}</b>
    <p v-if="description">{{ description }}</p>
    <slot />
  </div>
</template>

<style scoped>
.dp-empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 6px;
  min-height: 220px;
  padding: 40px 24px;
  text-align: center;
  color: var(--text-muted);
}

.dp-empty b {
  color: var(--text-secondary);
  font-size: 13.5px;
  font-weight: 600;
}

.dp-empty p {
  margin: 0;
  max-width: 280px;
  font-size: 12.5px;
  line-height: 1.55;
  color: var(--text-faint);
}

.dp-empty-icon {
  display: grid;
  place-items: center;
  width: 44px;
  height: 44px;
  margin-bottom: 8px;
  border-radius: 12px;
  background: var(--bg-muted);
  border: 1px solid var(--border);
  color: var(--text-faint);
}
</style>
