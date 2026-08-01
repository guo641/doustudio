<script setup lang="ts">
withDefaults(
  defineProps<{
    href?: string;
    download?: boolean | string;
    external?: boolean;
    asButton?: boolean;
  }>(),
  {
    external: false,
    asButton: false,
  },
);

defineEmits<{ click: [] }>();
</script>

<template>
  <button v-if="asButton" type="button" class="dp-link" @click="$emit('click')">
    <slot />
  </button>
  <a
    v-else
    class="dp-link"
    :href="href"
    :download="download === true ? '' : download || undefined"
    :target="external ? '_blank' : undefined"
    :rel="external ? 'noreferrer' : undefined"
  >
    <slot />
  </a>
</template>

<style scoped>
.dp-link {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  padding: 0;
  border: 0;
  background: none;
  color: var(--accent-text);
  text-decoration: none;
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  cursor: pointer;
}

.dp-link:hover {
  text-decoration: underline;
}
</style>
