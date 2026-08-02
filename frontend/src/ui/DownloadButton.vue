<script setup lang="ts">
/**
 * DownloadButton —— 跨域安全的下载按钮。
 *
 * 为什么不用 <a :download>?
 *   WebView2 / Chromium 在跨域 URL 上可能直接导航离开应用(浏览器对
 *   cross-origin `download` 属性的 silent fallback),Doubao/zhuceka
 *   这些 CDN URL 几乎都是 cross-origin,所以光靠 <a download> 在桌面
 *   端不可靠。
 *
 * 解决思路:
 *   1. fetch(URL) → blob
 *   2. URL.createObjectURL(blob) → 同源 blob URL
 *   3. 触发隐藏 <a download> → 下载对话框
 *   4. URL.revokeObjectURL → 释放内存
 *
 * 这样同源 / 异源统一走 Blob,完全规避 cross-origin download 静默
 * 降级导航的问题。
 *
 * 注意:有些 CDN 会带 Content-Disposition 强制浏览器在 WebView 内
 * 播放(而不是下载);走 Blob 后下载文件名由我们传入的 filename 控制。
 */
import { ref } from 'vue';
import { Loader2 } from '@lucide/vue';

const props = withDefaults(
  defineProps<{
    href: string;
    filename?: string;
    /** 同源 URL 时是否跳过 fetch,直接走原生 <a download>。默认 false 一律走 Blob */
    bypass?: boolean;
  }>(),
  { bypass: false },
);

defineEmits<{ error: [message: string] }>();

const busy = ref(false);

function inferFilename(url: string, fallback?: string): string {
  if (fallback) return fallback;
  try {
    const u = new URL(url, window.location.href);
    const last = u.pathname.split('/').filter(Boolean).pop();
    if (last) return last;
  } catch {
    /* ignore malformed URL */
  }
  return 'download';
}

function inferExtension(contentType: string, url: string): string {
  if (contentType) {
    const slash = contentType.indexOf('/');
    if (slash > 0) {
      const ext = contentType.slice(slash + 1).split(';')[0].trim();
      if (ext && ext !== 'octet-stream') return ext;
    }
  }
  try {
    const u = new URL(url, window.location.href);
    const dot = u.pathname.lastIndexOf('.');
    if (dot >= 0) {
      const ext = u.pathname.slice(dot + 1).split('?')[0];
      if (ext && ext.length <= 5) return ext;
    }
  } catch {
    /* ignore */
  }
  return '';
}

async function handleClick() {
  if (busy.value || !props.href) return;
  busy.value = true;
  try {
    let blob: Blob;
    let contentType = '';
    if (props.bypass) {
      // 同源路径:fetch 一下主要是为了拿 Content-Type 来补扩展名。
      const resp = await fetch(props.href);
      if (!resp.ok) throw new Error(`下载失败:${resp.status}`);
      blob = await resp.blob();
      contentType = resp.headers.get('content-type') || '';
    } else {
      // 跨域:很多 CDN 不带 CORS,直接 fetch 会 CORS error。
      // 用 no-cors mode 拿到 opaque blob,然后从 URL/filename 推断类型。
      try {
        const resp = await fetch(props.href, { mode: 'cors' });
        if (resp.ok) {
          blob = await resp.blob();
          contentType = resp.headers.get('content-type') || '';
        } else {
          throw new Error(`下载失败:${resp.status}`);
        }
      } catch {
        // fallback:直接拿 opaque response(Content-Type / size 都不可见,
        // 但至少能触发浏览器下载对话框)
        const opaque = await fetch(props.href, { mode: 'no-cors' });
        blob = await opaque.blob();
      }
    }

    let filename = inferFilename(props.href, props.filename);
    const ext = inferExtension(contentType || blob.type, props.href);
    if (ext && !filename.toLowerCase().endsWith(`.${ext.toLowerCase()}`)) {
      filename = `${filename}.${ext}`;
    }

    const blobUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = blobUrl;
    a.download = filename;
    a.rel = 'noopener';
    a.style.display = 'none';
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    // 给浏览器一点时间把 blob URL 喂给下载管理器,再回收。
    setTimeout(() => URL.revokeObjectURL(blobUrl), 1500);
  } catch (err) {
    const msg = err instanceof Error ? err.message : '下载失败';
    // 把消息冒给上层 toast;同时退回到 window.open —— 至少用户能拿到文件
    // (在 WebView2 里这会打开默认浏览器,比"点了没反应"好)
    try {
      window.open(props.href, '_blank', 'noopener');
    } catch {
      /* ignore */
    }
    throw err instanceof Error ? err : new Error(msg);
  } finally {
    busy.value = false;
  }
}
</script>

<template>
  <button
    type="button"
    class="dp-download"
    :disabled="busy"
    :aria-busy="busy"
    @click="handleClick"
  >
    <Loader2 v-if="busy" :size="12" class="spin" />
    <slot v-else />
  </button>
</template>

<style scoped>
.dp-download {
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

.dp-download:hover {
  text-decoration: underline;
}

.dp-download:disabled {
  opacity: 0.6;
  cursor: progress;
}

.spin {
  animation: dp-spin 1s linear infinite;
}

@keyframes dp-spin {
  to {
    transform: rotate(360deg);
  }
}
</style>