# Video Task Table Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the video task cards with a searchable, filterable Linear-style task table that supports prompt/error expansion, result actions, and retrying failed tasks.

**Architecture:** Add a focused `VideoTaskTable.vue` presentation component that owns search/filter/row-expansion state and emits retry events. Keep API calls and task creation in `App.vue`, where a retry creates a new task from the failed task's original parameters. Unknown quota data is rendered as `—` until the quota scheduler adds fields to the API.

**Tech Stack:** Vue 3 Composition API, TypeScript, Testing Library Vue, Vitest, existing Vite build.

## Global Constraints

- Preserve the existing dark Linear visual language.
- Render eight columns: status, task description, model parameters, account, quota, creation time, result, actions.
- Long prompts and errors must be expandable without rendering HTML.
- Search, status filter, and account filter must work together.
- Narrow windows must scroll horizontally instead of crushing columns.
- Do not show cancel actions until a backend cancel endpoint exists.
- Retrying creates a new task and preserves history.

---

### Task 1: Searchable Video Task Table Component

**Files:**
- Create: `frontend/src/components/VideoTaskTable.vue`
- Create: `frontend/src/__tests__/VideoTaskTable.test.ts`

**Interfaces:**
- Consumes prop: `tasks: VideoTaskRow[]`, where each row contains `id`, `account_name`, `prompt`, `model`, `ratio`, `duration`, `status`, optional result/error/quota fields, and `created_at`.
- Produces event: `retry(task: VideoTaskRow)` when the user retries a failed row.

- [ ] **Step 1: Write the failing component test**

Create `frontend/src/__tests__/VideoTaskTable.test.ts` with two tasks and assertions for eight headers, combined filtering, prompt/error expansion, quota fallback, result links, and retry emission:

```ts
import { fireEvent, render, screen } from '@testing-library/vue';
import { expect, it } from 'vitest';
import VideoTaskTable from '../components/VideoTaskTable.vue';

const tasks = [
  { id:'failed-1', account_name:'莲韵', prompt:'一段很长的小狗跳舞画面描述', model:'seedance_v2.0_mini', ratio:'9:16', duration:5, status:'failed', error:'rate limited', created_at:'2026-07-13T12:06:23' },
  { id:'done-1', account_name:'账号 02', prompt:'橘猫窗边看雨', model:'seedance_v2.0_std', ratio:'16:9', duration:5, status:'succeeded', result_url:'https://example.test/video.mp4', quota_used:2, quota_total:5, created_at:'2026-07-13T11:12:26' },
];

it('renders and filters the video task table', async () => {
  const view = render(VideoTaskTable, { props:{ tasks } });
  for (const name of ['状态','任务描述','模型参数','执行账号','额度','创建时间','结果','操作']) expect(screen.getByText(name)).toBeTruthy();
  expect(screen.getByText('2/5')).toBeTruthy();
  expect(screen.getByText('—')).toBeTruthy();
  await fireEvent.update(screen.getByLabelText('搜索任务'), '橘猫');
  expect(screen.queryByText('莲韵')).toBeNull();
  await fireEvent.update(screen.getByLabelText('状态筛选'), 'succeeded');
  await fireEvent.update(screen.getByLabelText('账号筛选'), '账号 02');
  expect(screen.getByText('橘猫窗边看雨')).toBeTruthy();
  await fireEvent.update(screen.getByLabelText('搜索任务'), '');
  await fireEvent.update(screen.getByLabelText('状态筛选'), '');
  await fireEvent.update(screen.getByLabelText('账号筛选'), '');
  await fireEvent.click(screen.getByRole('button', { name:'展开任务 failed-1' }));
  expect(screen.getByText('rate limited')).toBeTruthy();
  await fireEvent.click(screen.getByRole('button', { name:'重试任务 failed-1' }));
  expect(view.emitted().retry?.[0]?.[0]).toEqual(tasks[0]);
});
```

- [ ] **Step 2: Run the focused test and verify RED**

Run: `npm --prefix frontend test -- --run src/__tests__/VideoTaskTable.test.ts`

Expected: FAIL because `VideoTaskTable.vue` does not exist.

- [ ] **Step 3: Implement the component**

Create `frontend/src/components/VideoTaskTable.vue` with:

```vue
<script setup lang="ts">
import { computed, ref } from 'vue';

export type VideoTaskRow = {
  id:string; account_name?:string; prompt:string; model:string; ratio:string; duration:number;
  status:string; result_url?:string; error?:string; quota_used?:number; quota_total?:number;
  created_at:string;
};
const props = defineProps<{ tasks:VideoTaskRow[] }>();
defineEmits<{ retry:[task:VideoTaskRow] }>();
const query=ref(''); const status=ref(''); const account=ref(''); const expanded=ref(new Set<string>());
const accounts=computed(()=>[...new Set(props.tasks.map(t=>t.account_name).filter(Boolean))] as string[]);
const visible=computed(()=>props.tasks.filter(t=>(!query.value||t.prompt.toLowerCase().includes(query.value.toLowerCase()))&&(!status.value||t.status===status.value)&&(!account.value||t.account_name===account.value)));
const labels:Record<string,string>={queued:'排队中',starting:'分配账号',generating:'生成中',resolving:'获取无水印',succeeded:'已完成',failed:'失败',cancelled:'已取消'};
const models:Record<string,string>={'seedance_v2.0_std':'2.0','seedance_v2.0':'2.0 Fast','seedance_v2.0_mini':'2.0 Mini'};
function toggle(id:string){const next=new Set(expanded.value);next.has(id)?next.delete(id):next.add(id);expanded.value=next}
function time(value:string){return new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit'}).format(new Date(value))}
</script>

<template>
  <div class="task-table-shell panel">
    <div class="task-filters"><input v-model="query" aria-label="搜索任务" placeholder="搜索任务描述…"><select v-model="status" aria-label="状态筛选"><option value="">全部状态</option><option v-for="(label,key) in labels" :key="key" :value="key">{{ label }}</option></select><select v-model="account" aria-label="账号筛选"><option value="">全部账号</option><option v-for="name in accounts" :key="name">{{ name }}</option></select><span>{{ visible.length }} 个任务</span></div>
    <div class="table-scroll"><table><thead><tr><th>状态</th><th>任务描述</th><th>模型参数</th><th>执行账号</th><th>额度</th><th>创建时间</th><th>结果</th><th>操作</th></tr></thead><tbody>
      <template v-for="task in visible" :key="task.id"><tr><td><mark :class="task.status">● {{ labels[task.status]||task.status }}</mark></td><td><button class="prompt" :aria-label="`展开任务 ${task.id}`" @click="toggle(task.id)">{{ task.prompt }}</button></td><td>{{ models[task.model]||task.model }} · {{ task.duration }}s · {{ task.ratio }}</td><td>{{ task.account_name||'等待分配' }}</td><td>{{ task.quota_used==null||task.quota_total==null?'—':`${task.quota_used}/${task.quota_total}` }}</td><td>{{ time(task.created_at) }}</td><td><template v-if="task.result_url"><a :href="task.result_url" target="_blank" rel="noreferrer">预览</a><a :href="task.result_url" download>无水印下载</a></template><span v-else>—</span></td><td><button v-if="task.status==='failed'" :aria-label="`重试任务 ${task.id}`" @click="$emit('retry',task)">重新加入队列</button></td></tr><tr v-if="expanded.has(task.id)" class="detail"><td colspan="8"><p>{{ task.prompt }}</p><p v-if="task.error" class="error">{{ task.error }}</p></td></tr></template>
    </tbody></table></div>
  </div>
</template>
```

Add scoped CSS for a minimum-width table, sticky-style subdued header, compact rows, ellipsized prompt button, horizontal scrolling, status badges, and hover states. Use `min-width: 1120px` on the table and `overflow-x:auto` on `.table-scroll`.

- [ ] **Step 4: Run the focused test and verify GREEN**

Run: `npm --prefix frontend test -- --run src/__tests__/VideoTaskTable.test.ts`

Expected: PASS with 1 test.

- [ ] **Step 5: Commit the component**

```bash
git add frontend/src/components/VideoTaskTable.vue frontend/src/__tests__/VideoTaskTable.test.ts
git commit -m "feat: add searchable video task table"
```

### Task 2: Integrate Table and Failed-Task Retry

**Files:**
- Modify: `frontend/src/App.vue`
- Modify: `frontend/src/__tests__/VideoTasks.test.ts`

**Interfaces:**
- Consumes: `VideoTaskTable` and its `retry` event from Task 1.
- Produces: `retryVideoTask(task)` which calls the existing `createVideoTask` API with the original task parameters and `account_id:null`.

- [ ] **Step 1: Extend the existing page test for retry integration**

Update `frontend/src/__tests__/VideoTasks.test.ts` so `listVideoTasks` returns a failed task, then click its retry action and assert the API receives the original parameters:

```ts
listVideoTasks.mockResolvedValue([{ id:'failed-1', account_name:'莲韵', prompt:'小狗跳舞', model:'seedance_v2.0_mini', ratio:'9:16', duration:5, status:'failed', error:'rate limited', created_at:'2026-07-13T12:06:23' }]);
await fireEvent.click(screen.getByText(/视频任务/));
await screen.findByText('小狗跳舞');
await fireEvent.click(screen.getByRole('button',{name:'重试任务 failed-1'}));
await waitFor(()=>expect(createVideoTask).toHaveBeenCalledWith({prompt:'小狗跳舞',model:'seedance_v2.0_mini',ratio:'9:16',duration:5,account_id:null}));
```

- [ ] **Step 2: Run the page test and verify RED**

Run: `npm --prefix frontend test -- --run src/__tests__/VideoTasks.test.ts`

Expected: FAIL because the current card list does not render the retry action.

- [ ] **Step 3: Replace card markup and implement retry**

In `frontend/src/App.vue`:

```ts
import VideoTaskTable from './components/VideoTaskTable.vue';

async function retryVideoTask(task:VideoTask) {
  creating.value=true;
  try {
    await createVideoTask({prompt:task.prompt,model:task.model,ratio:task.ratio,duration:task.duration,account_id:null});
    state.value='succeeded'; message.value='任务已重新加入队列'; await refreshTasks();
  } catch(error) {
    state.value='failed'; message.value=error instanceof Error?error.message:'重试任务失败';
  } finally { creating.value=false; }
}
```

Replace the existing `.task-panel` card loop with:

```vue
<VideoTaskTable :tasks="tasks" @retry="retryVideoTask" />
```

Remove card-only helpers and styles that no longer have callers, while retaining `statusName` only if another view still uses it.

- [ ] **Step 4: Run all frontend tests**

Run: `npm --prefix frontend test -- --run`

Expected: all component and page tests PASS.

- [ ] **Step 5: Run TypeScript and production build verification**

Run: `npm --prefix frontend run build`

Expected: `vue-tsc --noEmit` and `vite build` exit 0.

- [ ] **Step 6: Commit integration**

```bash
git add frontend/src/App.vue frontend/src/__tests__/VideoTasks.test.ts
git commit -m "feat: use table for video task queue"
```

### Task 3: Full Regression Verification

**Files:**
- Verify only; no planned source changes.

**Interfaces:**
- Consumes the completed frontend table and existing Python backend.
- Produces fresh verification evidence for handoff.

- [ ] **Step 1: Run backend regression tests**

Run: `uv run pytest -q`

Expected: all Python tests PASS.

- [ ] **Step 2: Run frontend regression tests**

Run: `npm --prefix frontend test -- --run`

Expected: all Vitest tests PASS.

- [ ] **Step 3: Build production frontend**

Run: `npm --prefix frontend run build`

Expected: TypeScript checking and Vite build PASS.

- [ ] **Step 4: Confirm the implementation diff**

Run: `git status --short && git diff --check`

Expected: no whitespace errors and no unrelated files added by this plan.
