<script setup lang="ts">
import { Search } from '@lucide/vue';
import { useAttrs } from 'vue';

defineProps<{
  placeholder?: string;
}>();

defineOptions({ inheritAttrs: false });

const model = defineModel<string>({ default: '' });
const attrs = useAttrs();
</script>

<template>
  <div class="dp-search">
    <Search :size="14" :stroke-width="1.75" />
    <input
      v-model="model"
      v-bind="attrs"
      :placeholder="placeholder"
    />
  </div>
</template>

<style scoped>
.dp-search {
  box-sizing: border-box;
  display: flex;
  align-items: center;
  gap: 8px;
  min-width: 220px;
  height: 36px;
  padding: 0 12px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-input);
  color: var(--text-faint);
  transition: border-color 0.12s ease, box-shadow 0.12s ease;
}

.dp-search:hover {
  border-color: #3a4050;
}

.dp-search:focus-within {
  border-color: var(--border-focus);
  box-shadow: var(--shadow-glow);
}

.dp-search input {
  width: 100%;
  height: 100%;
  padding: 0;
  border: 0;
  outline: 0;
  background: transparent;
  color: var(--text);
  font-size: 13px;
  appearance: none;
}

.dp-search input::placeholder {
  color: var(--text-faint);
}
</style>
