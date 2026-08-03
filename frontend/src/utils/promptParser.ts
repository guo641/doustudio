// v0.2.11:解析「第一段」「段一」「1.」这类带段标记的 prompt。
// 不带标记的整段当一个 prompt(避免用户原文写整段自然语言时,无意换行被错切)。
// 行为必须和 src/doupool/prompt_parser.py 完全对齐(同一族 regex)。
//
// 可识别:
//   第一段 / 第二段 / 第三段 ...
//   段一 / 段二 / 段三 ...
//   1. / 2. / 3. / 1、 / 1)
//
// 行内出现的「第一段」字眼不切(只切行首的换行分段),避免误伤 prompt 文本。

const SEGMENT_MARKER_RE = new RegExp(
  '(?:(?<=^)|(?<=\n))[ \\t]*' +
    '(?:' +
      '第\\s*[一二三四五六七八九十百千万零〇\\d]+\\s*段' +
      '|' +
      '段\\s*[一二三四五六七八九十百千万零〇\\d]+' +
      '|' +
      '\\d+\\s*[.、)]' +
    ')' +
    '\\s*[:：]?' +
    '\\s*',
  'g',
);

export function splitBySegmentMarkers(text: string): string[] {
  const trimmed = (text ?? '').trim();
  if (!trimmed) return [];
  // 先探测一次决定走哪个分支(避免空切)
  if (!new RegExp(SEGMENT_MARKER_RE.source, '').test(trimmed)) return [trimmed];
  const parts = trimmed.split(new RegExp(SEGMENT_MARKER_RE.source, ''));
  const segments = parts.map((p) => p.trim()).filter((p) => p.length > 0);
  return segments.length > 0 ? segments : [trimmed];
}