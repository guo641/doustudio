"""v0.2.11:解析「第一段」「段一」「1.」这类带段标记的 prompt。

不带标记的整段当一个 prompt(用户原文写整段自然语言时不会被错切)。
带标记时,只切行首出现标记的换行分段(行内出现的「段」字不切,避免误伤)。
"""
from __future__ import annotations

import re


# 行首允许缩进;可识别三族:
#   第一段 / 第二段 / ...   (中文任意序数词 + 段)
#   段一 / 段二 / ...       (段 + 中文任意序数词)
#   1. / 2. / 3. / 1、 / 1) (阿拉伯数字 + 句点/顿号/右括号)
# 标记后可跟 : 或 : 可选空白
#
# 用 (?<=^)|(?<=\n) 模拟行首(避免 re.MULTILINE 下 ^ 同时匹配字符串开头+换行
# 在某些边缘 case 出乱),干净地"必须出现在行首"。
_SEGMENT_MARKER_RE = re.compile(
    r"""
    (?:(?<=^)|(?<=\n))[ \t]*      # 行首(允许行首缩进)
    (?:
      第\s*[一二三四五六七八九十百千万零〇\d]+\s*段
      |
      段\s*[一二三四五六七八九十百千万零〇\d]+
      |
      \d+\s*[.、)]
    )
    \s*[:：]?                       # 可选分隔符
    \s*                              # 可选空白
    """,
    re.VERBOSE,
)


def split_by_segment_markers(text: str) -> list[str]:
    """带标记的切;无标记返回 [text](原文 trim)。空串返回 []。

    示例:
      "第一段:猫 第二段:狗"            -> ["猫 第二段:狗"]   # 同行不切
      "第一段:猫\n第二段:狗"           -> ["猫", "狗"]
      "段一:猫\n段二:狗\n第三段:鱼"    -> ["猫", "狗", "鱼"]
      "1. 猫\n2. 狗\n3) 鱼"           -> ["猫", "狗", "鱼"]
      "场景中描述 第一段 是文案"        -> ["场景中描述 第一段 是文案"]  # 行内不切
      ""                              -> []
    """
    text = (text or "").strip()
    if not text:
        return []
    if not _SEGMENT_MARKER_RE.search(text):
        return [text]
    parts = _SEGMENT_MARKER_RE.split(text)
    segments = [p.strip() for p in parts if p and p.strip()]
    return segments or [text]