<script setup lang="ts">
defineProps<{
  title?: string;
  description?: string;
  wide?: boolean;
}>();
</script>

<template>
  <section class="dp-card" :class="{ 'is-wide': wide }">
    <div v-if="title || description || $slots.header" class="dp-card-head">
      <h2 v-if="title">{{ title }}</h2>
      <p v-if="description">{{ description }}</p>
      <slot name="header" />
    </div>
    <div class="dp-card-body">
      <slot />
    </div>
    <div v-if="$slots.footer" class="dp-card-footer">
      <slot name="footer" />
    </div>
  </section>
</template>

<style scoped>
.dp-card {
  padding: 20px;
  border: 1px solid var(--border);
  border-radius: var(--r-lg);
  background: var(--bg-panel);
  box-shadow: var(--shadow-sm);
}

.dp-card.is-wide {
  grid-column: 1 / -1;
}

.dp-card-head {
  margin-bottom: 18px;
}

.dp-card-head h2 {
  margin: 0 0 4px;
  font-size: 14px;
  font-weight: 650;
  letter-spacing: -0.01em;
}

.dp-card-head p {
  margin: 0;
  color: var(--text-muted);
  font-size: 12px;
}

.dp-card-body {
  min-width: 0;
}

.dp-card-footer {
  margin-top: 16px;
}
</style>
