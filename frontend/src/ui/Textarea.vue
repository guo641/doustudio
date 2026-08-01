<script setup lang="ts">
withDefaults(
  defineProps<{
    rows?: number;
    maxlength?: number;
    placeholder?: string;
    disabled?: boolean;
    id?: string;
    autofocus?: boolean;
  }>(),
  {
    rows: 4,
    disabled: false,
    autofocus: false,
  },
);

const model = defineModel<string>({ default: '' });
</script>

<template>
  <textarea
    :id="id"
    v-model="model"
    class="dp-textarea"
    :rows="rows"
    :maxlength="maxlength"
    :placeholder="placeholder"
    :disabled="disabled"
    :autofocus="autofocus"
  />
</template>

<style scoped>
.dp-textarea {
  width: 100%;
  min-height: 120px;
  padding: 10px 12px;
  border: 1px solid var(--border-strong);
  border-radius: var(--r-md);
  background: var(--bg-input);
  color: var(--text);
  outline: none;
  resize: vertical;
  line-height: 1.6;
  transition: border-color 0.12s ease, box-shadow 0.12s ease;
}

.dp-textarea:focus {
  border-color: var(--border-focus);
  box-shadow: var(--shadow-glow);
}

.dp-textarea::placeholder {
  color: var(--text-faint);
}
</style>
