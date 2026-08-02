<script setup lang="ts">
withDefaults(
  defineProps<{
    type?: string;
    placeholder?: string;
    disabled?: boolean;
    readonly?: boolean;
    min?: number | string;
    max?: number | string;
    id?: string;
    ariaLabel?: string;
  }>(),
  {
    type: 'text',
    disabled: false,
    readonly: false,
  },
);

const model = defineModel<string | number>({ default: '' });
</script>

<template>
  <input
    :id="id"
    v-model="model"
    class="dp-input"
    :class="{ 'is-readonly': readonly }"
    :type="type"
    :placeholder="placeholder"
    :disabled="disabled"
    :readonly="readonly"
    :min="min"
    :max="max"
    :aria-label="ariaLabel"
  />
</template>

<style scoped>
.dp-input {
  box-sizing: border-box;
  width: 100%;
  height: 36px;
  padding: 0 12px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-input);
  color: var(--text);
  font-size: 13px;
  line-height: 1.3;
  outline: none;
  appearance: none;
  -webkit-appearance: none;
  transition: border-color 0.12s ease, box-shadow 0.12s ease, background 0.12s ease;
}

.dp-input:hover:not(:disabled):not(:read-only) {
  border-color: #3a4050;
}

.dp-input:focus {
  border-color: var(--border-focus);
  box-shadow: var(--shadow-glow);
}

.dp-input:disabled {
  opacity: 0.55;
  cursor: not-allowed;
}

.dp-input.is-readonly,
.dp-input:read-only {
  color: var(--text-faint);
  cursor: default;
}

.dp-input::placeholder {
  color: var(--text-faint);
}

/* number: hide spin buttons for a clean look */
.dp-input[type='number'] {
  -moz-appearance: textfield;
}

.dp-input[type='number']::-webkit-outer-spin-button,
.dp-input[type='number']::-webkit-inner-spin-button {
  -webkit-appearance: none;
  margin: 0;
}

/* time: normalize native chrome */
.dp-input[type='time'] {
  color-scheme: dark;
  min-width: 0;
}

.dp-input[type='time']::-webkit-calendar-picker-indicator {
  opacity: 0.55;
  cursor: pointer;
  filter: invert(0.85);
}

.dp-input[type='time']::-webkit-datetime-edit {
  padding: 0;
}

.dp-input[type='time']::-webkit-datetime-edit-fields-wrapper {
  padding: 0;
}
</style>
