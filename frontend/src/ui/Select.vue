<script setup lang="ts">
withDefaults(
  defineProps<{
    disabled?: boolean;
    id?: string;
    compact?: boolean;
  }>(),
  {
    disabled: false,
    compact: false,
  },
);

const model = defineModel<string | number>({ default: '' });
</script>

<template>
  <div class="dp-select-wrap" :class="{ 'is-compact': compact }">
    <select
      :id="id"
      v-model="model"
      class="dp-select"
      :disabled="disabled"
    >
      <slot />
    </select>
    <span class="dp-select-chevron" aria-hidden="true">
      <svg width="12" height="12" viewBox="0 0 12 12" fill="none">
        <path
          d="M3 4.5L6 7.5L9 4.5"
          stroke="currentColor"
          stroke-width="1.5"
          stroke-linecap="round"
          stroke-linejoin="round"
        />
      </svg>
    </span>
  </div>
</template>

<style scoped>
.dp-select-wrap {
  position: relative;
  width: 100%;
  display: block;
}

.dp-select-wrap.is-compact {
  width: auto;
  min-width: 120px;
  display: inline-block;
}

.dp-select {
  box-sizing: border-box;
  width: 100%;
  height: 36px;
  padding: 0 34px 0 12px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-input);
  color: var(--text);
  font-size: 13px;
  line-height: 1.3;
  outline: none;
  cursor: pointer;
  appearance: none;
  -webkit-appearance: none;
  -moz-appearance: none;
  transition: border-color 0.12s ease, box-shadow 0.12s ease, background 0.12s ease;
}

.dp-select-wrap.is-compact .dp-select {
  height: 34px;
  min-width: 120px;
  padding: 0 30px 0 10px;
  font-size: 12.5px;
}

.dp-select:hover:not(:disabled) {
  border-color: #3a4050;
}

.dp-select:focus {
  border-color: var(--border-focus);
  box-shadow: var(--shadow-glow);
}

.dp-select:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

/* Hide legacy IE arrow */
.dp-select::-ms-expand {
  display: none;
}

.dp-select-chevron {
  position: absolute;
  top: 50%;
  right: 11px;
  display: grid;
  place-items: center;
  color: var(--text-muted);
  pointer-events: none;
  transform: translateY(-50%);
}

.dp-select-wrap.is-compact .dp-select-chevron {
  right: 9px;
}

.dp-select:disabled + .dp-select-chevron {
  opacity: 0.5;
}

.dp-select:focus + .dp-select-chevron {
  color: var(--accent-text);
}
</style>
