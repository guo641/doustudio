<script setup lang="ts">
defineProps<{
  on?: boolean;
}>();

defineEmits<{ click: [] }>();
</script>

<template>
  <button
    type="button"
    class="dp-switch"
    :class="{ off: !on }"
    @click="$emit('click')"
  >
    <i />
  </button>
</template>

<style scoped>
.dp-switch {
  width: 34px;
  height: 18px;
  padding: 2px;
  border: 0;
  border-radius: var(--r-full);
  background: var(--accent);
  transition: background 0.15s ease;
}

.dp-switch i {
  display: block;
  width: 14px;
  height: 14px;
  margin-left: auto;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.25);
  transition: margin 0.15s ease;
}

.dp-switch.off {
  background: #2e3340;
}

.dp-switch.off i {
  margin-left: 0;
}
</style>
