<script setup lang="ts">
defineProps<{
  label?: string;
  forId?: string;
  hint?: string;
  span2?: boolean;
}>();
</script>

<template>
  <div class="dp-field" :class="{ 'is-span-2': span2 }">
    <label v-if="label" class="dp-field-label" :for="forId">{{ label }}</label>
    <slot />
    <span v-if="hint" class="dp-field-hint">{{ hint }}</span>
  </div>
</template>

<style scoped>
.dp-field {
  display: block;
  color: var(--text-muted);
  font-size: 11.5px;
  font-weight: 500;
}

.dp-field-label {
  display: block;
  margin-bottom: 6px;
}

.dp-field :deep(.dp-input),
.dp-field :deep(.dp-select-wrap),
.dp-field :deep(.dp-select),
.dp-field :deep(.dp-textarea) {
  margin-top: 0;
  width: 100%;
}

.dp-field-hint {
  display: block;
  margin-top: 6px;
  color: var(--text-faint);
  font-size: 12px;
  font-weight: 400;
}

.dp-field.is-span-2 {
  grid-column: 1 / -1;
}
</style>
