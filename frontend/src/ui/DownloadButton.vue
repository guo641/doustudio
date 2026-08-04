<script setup lang="ts">
/**
 * DownloadButton —— 跨域安全的下载按钮。
 *
 * 三层 fallback,WebView2/Chromium 桌面端可靠下载:
 *   1. fetch(URL, mode='cors') → blob + <a download> → 走应用内下载
 *      (服务端开了 CORS 才走得到,大部分 CDN 不开)
 *   2. fetch(URL, mode='no-cors') → opaque blob + <a download>
 *      大多数情况下能下载,但 WebView2 对 opaque response 出来的 blob
 *      处理不一致 —— body 可能空 / type 空 / 下载管理器不接
 *   3. window.open(URL, '_blank', 'noopener,noreferrer') → 走系统默认浏览器
 *      兜底必能下(WebView2 在 pywebview 里把新窗口代理到 OS 默认浏览器),
 *      不会让用户「点了没反应」
 *
 * 历史:曾经只用 <a download href=cdnUrl>,WebView2 在跨域 URL 上会静默
 * 降级成导航离开应用,所以才有 fetch+blob 方案。但 fetch+blob 也不是
 * 100% 可靠(WebView2 对 opaque blob 的下载管理器行为不一致),所以保留
 * window.open 作为最后一层兜底。
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

/**
 * 用 <a download> + blob URL 触发下载。blob 为空 / 没有 type 时直接
 * 返回 false,外层 fallback 走 window.open。
 */
function triggerBlobDownload(blob: Blob, filename: string): boolean {
  if (!blob || blob.size === 0) {
    return false;
  }
  try {
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
    return true;
  } catch {
    return false;
  }
}

/**
 * 最后兜底:打开系统浏览器下载。WebView2 + pywebview 下 window.open
 * 会被代理到 OS 默认浏览器(Edge/Chrome),浏览器对 cross-origin URL
 * 下载行为一致,确保用户点了有反应。
 */
function openInSystemBrowser(url: string): boolean {
  try {
    const win = window.open(url, '_blank', 'noopener,noreferrer');
    return win !== null;
  } catch {
    return false;
  }
}

async function handleClick() {
  if (busy.value || !props.href) return;
  busy.value = true;
  try {
    const filename = inferFilename(
      props.href,
      props.filename ?? `${Date.now()}.bin`,
    );

    if (props.bypass) {
      // 同源路径:fetch 一下主要是为了拿 Content-Type 来补扩展名。
      const resp = await fetch(props.href);
      if (!resp.ok) throw new Error(`下载失败:${resp.status}`);
      const blob = await resp.blob();
      const ext = inferExtension(resp.headers.get('content-type') || '', props.href);
      const finalName = ext && !filename.toLowerCase().endsWith(`.${ext.toLowerCase()}`)
        ? `${filename}.${ext}`
        : filename;
      if (!triggerBlobDownload(blob, finalName)) {
        if (!openInSystemBrowser(props.href)) {
          throw new Error('下载失败:无法打开浏览器');
        }
      }
      return;
    }

    // 跨域:先试 CORS(服务端开了就能拿真 Content-Type / 字节)。
    let downloaded = false;
    try {
      const resp = await fetch(props.href, { mode: 'cors' });
      if (resp.ok) {
        const blob = await resp.blob();
        const contentType = resp.headers.get('content-type') || '';
        const ext = inferExtension(contentType || blob.type, props.href);
        const finalName = ext && !filename.toLowerCase().endsWith(`.${ext.toLowerCase()}`)
          ? `${filename}.${ext}`
          : filename;
        downloaded = triggerBlobDownload(blob, finalName);
      }
    } catch {
      // CORS 拒绝或网络错 → 下一层
    }

    if (downloaded) return;

    // 兜底:no-cors opaque response → blob。WebView2 对空 / 无 type blob
    // 不一定触发下载管理器,triggerBlobDownload 会判断 size 并返回 false。
    try {
      const opaque = await fetch(props.href, { mode: 'no-cors' });
      const blob = await opaque.blob();
      const ext = inferExtension(blob.type, props.href);
      const finalName = ext && !filename.toLowerCase().endsWith(`.${ext.toLowerCase()}`)
        ? `${filename}.${ext}`
        : filename;
      downloaded = triggerBlobDownload(blob, finalName);
    } catch {
      // opaque fetch 失败也走下一层
    }

    if (downloaded) return;

    // 最后一层:打开系统浏览器(WebView2 + pywebview 代理到 OS 默认浏览器)。
    if (!openInSystemBrowser(props.href)) {
      throw new Error('下载失败:所有路径都不可用');
    }
  } catch (err) {
    const msg = err instanceof Error ? err.message : '下载失败';
    // 把消息冒给上层 toast
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