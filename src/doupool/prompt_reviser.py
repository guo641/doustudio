"""
DouStudio 失败自动改 prompt 重试

豆包文生视频会因为以下原因失败,本模块:
1. classify_failure(): 从错误信息 / 状态码分类失败原因
2. revise_prompt(): 根据分类生成改写后的 prompt(用于重试)

不是所有失败都该改 prompt — 额度类、网络类不应该改。只有输入语义类
(违规、敏感词、画面描述不清) 才改写 prompt 重试。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Optional


class FailureKind(str, Enum):
    """失败分类 — 决定是否改写 prompt + 改写策略"""

    POLICY_VIOLATION = "policy_violation"   # 内容违规 / 侵权 / 敏感词
    RATE_LIMITED = "rate_limited"           # 额度耗尽 / 当日限额
    NETWORK = "network"                     # 网络异常 / 超时 / 5xx
    INVALID_INPUT = "invalid_input"         # 输入格式错误 / 描述不清
    GENERATION_FAILED = "generation_failed" # 豆包明确说"视频生成失败"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class FailureInfo:
    kind: FailureKind
    retryable: bool            # 是否应该用同一账号重试
    revise_prompt: bool        # 是否应该改写 prompt
    detail: str = ""


# 匹配各种已知失败文案(来自上游 yaonieyo/doubao-account-pool doubao-page-state.ts + 实际跑过的经验)
_POLICY_PATTERNS = [
    re.compile(r"生成内容中疑似包含.*?(?:侵权|违规)", re.DOTALL),
    re.compile(r"疑似包含.*?(?:侵权|违规)", re.DOTALL),
    re.compile(r"(?:侵权|违规)内容.*?(?:无法返回|不能返回|换个主题)", re.DOTALL),
    re.compile(r"无法返回该内容"),
    re.compile(r"换个主题再试试"),
    re.compile(r"内容可能违反.*?(?:规定|政策|法律)"),
    re.compile(r"sensitive content", re.IGNORECASE),
    re.compile(r"content.{0,20}violat", re.IGNORECASE),
]

_RATE_LIMIT_PATTERNS = [
    re.compile(r"今日(?:免费)?额度(?:已)?(?:用完|耗尽|不足)"),
    re.compile(r"quota.{0,20}(?:exhaust|limit)", re.IGNORECASE),
    re.compile(r"rate.{0,20}limit", re.IGNORECASE),
    re.compile(r"额度未扣除"),  # 这种不算 rate limit,反而是豆包没扣成功
]

_NETWORK_PATTERNS = [
    re.compile(r"网络(?:请求|异常|超时|错误|失败)"),
    re.compile(r"(?:timeout|timed out)", re.IGNORECASE),
    re.compile(r"fetch failed", re.IGNORECASE),
    re.compile(r"connection.{0,20}(?:reset|refused|aborted)", re.IGNORECASE),
    re.compile(r"5\d\d\s*server"),
]

_INVALID_INPUT_PATTERNS = [
    re.compile(r"prompt.{0,20}(?:empty|missing|required|invalid)", re.IGNORECASE),
    re.compile(r"参数.{0,20}(?:无效|不合法|错误)"),
    re.compile(r"图片.{0,30}(?:不存在|无效|格式|超过)"),
]

_GENERATION_FAILED_PATTERNS = [
    re.compile(r"视频生成失败"),
    re.compile(r"生成视频失败"),
    re.compile(r"未能生成视频"),
    re.compile(r"video.{0,20}generation.{0,20}fail", re.IGNORECASE),
]


def classify_failure(error_message: str, status_code: Optional[int] = None) -> FailureInfo:
    """
    从错误信息 / 状态码判定失败分类。

    返回 FailureInfo,业务侧根据 retryable / revise_prompt 决定下一步动作。
    """
    msg = (error_message or "").strip()
    msg_lower = msg.lower()

    # 1. HTTP 状态码优先
    if status_code is not None:
        if status_code == 429:
            return FailureInfo(FailureKind.RATE_LIMITED, retryable=True, revise_prompt=False, detail=msg)
        if 500 <= status_code < 600:
            return FailureInfo(FailureKind.NETWORK, retryable=True, revise_prompt=False, detail=f"HTTP {status_code}: {msg}")
        if status_code == 400:
            return FailureInfo(FailureKind.INVALID_INPUT, retryable=False, revise_prompt=False, detail=f"HTTP 400: {msg}")
        if status_code == 401 or status_code == 403:
            return FailureInfo(FailureKind.POLICY_VIOLATION, retryable=False, revise_prompt=False, detail=f"HTTP {status_code}: {msg}")

    # 2. 字符串匹配
    for pat in _POLICY_PATTERNS:
        if pat.search(msg):
            return FailureInfo(FailureKind.POLICY_VIOLATION, retryable=True, revise_prompt=True, detail=pat.search(msg).group(0))

    for pat in _RATE_LIMIT_PATTERNS:
        if pat.search(msg):
            return FailureInfo(FailureKind.RATE_LIMITED, retryable=True, revise_prompt=False, detail=pat.search(msg).group(0))

    for pat in _NETWORK_PATTERNS:
        if pat.search(msg):
            return FailureInfo(FailureKind.NETWORK, retryable=True, revise_prompt=False, detail=pat.search(msg).group(0))

    for pat in _GENERATION_FAILED_PATTERNS:
        if pat.search(msg):
            return FailureInfo(FailureKind.GENERATION_FAILED, retryable=True, revise_prompt=True, detail=pat.search(msg).group(0))

    for pat in _INVALID_INPUT_PATTERNS:
        if pat.search(msg):
            return FailureInfo(FailureKind.INVALID_INPUT, retryable=False, revise_prompt=False, detail=pat.search(msg).group(0))

    return FailureInfo(FailureKind.UNKNOWN, retryable=True, revise_prompt=False, detail=msg or "(empty error)")


# ---------- Prompt 改写策略 ----------


# 高风险关键词(可能在 prompt 中触发了政策违规)
# 不依赖 \b word boundary,因为中英混排时 \b 在中文之间不触发
_RISKY_KEYWORDS = [
    r"(?:nude|naked|nsfw|sex|porn)",
    r"(?:violence|blood|gore|kill|murder)",
    r"(?:gun|weapon|explosive)",
    r"(?:裸体|裸照|色情|情色|做爱|性交|自慰)",
    r"(?:血腥|暴力|凶杀|枪支|武器|爆炸)",
    r"(?:吸毒|毒品|冰毒|大麻|海洛因)",
    r"(?:习近平|毛泽东|江泽民|胡锦涛|温家宝|李克强|国家领导人|中共中央)",
    r"(?:台湾|新疆|西藏|香港).{0,8}(?:独立|分离|建国)",
]


def _strip_risky_keywords(prompt: str) -> str:
    """去掉触发违规的关键词,留下骨架"""
    cleaned = prompt
    for pat in _RISKY_KEYWORDS:
        cleaned = re.sub(pat, " ", cleaned, flags=re.IGNORECASE)
    # 合并多余空白
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def _soften_description(prompt: str) -> str:
    """把直白的画面描述柔化,适合合规重试"""
    softening_prefixes = [
        "请生成一段温和、符合平台规范的短视频:",
        "请基于以下合规、安全的画面描述生成视频:",
        "请生成一段适合公开分享的短视频,画面内容:",
    ]
    return f"{softening_prefixes[0]} {prompt}"


def revise_prompt(prompt: str, failure: FailureInfo, attempt: int = 1) -> str:
    """
    根据失败分类改写 prompt,用于重试。

    attempt: 1 = 第一次改写,2 = 第二次(更激进的策略)

    返回改写后的 prompt。失败分类不需要改 prompt 时,返回原 prompt。
    """
    if not failure.revise_prompt:
        return prompt

    base = (prompt or "").strip()
    if not base:
        return base

    if failure.kind == FailureKind.POLICY_VIOLATION:
        # 违规: 先剥离风险关键词,再软化描述
        cleaned = _strip_risky_keywords(base)
        if not cleaned:
            cleaned = "一段温馨、阳光、积极向上的短视频"
        if attempt >= 2:
            # 第二次尝试: 直接走安全模板,不依赖原 prompt
            return "一段适合各年龄段观看的阳光、积极、温馨的短视频,色调明亮,画面干净"
        return f"请生成一段温和、符合平台规范的短视频:{cleaned}"

    if failure.kind == FailureKind.GENERATION_FAILED:
        # 生成失败但原因不明: 简化描述,缩短长度,降低复杂度
        if attempt >= 2:
            simplified = re.sub(r"[，。；、,.;:]", " ", base)
            simplified = re.sub(r"\s+", " ", simplified).strip()
            if len(simplified) > 60:
                simplified = simplified[:60] + "……"
            return simplified or base
        return f"简化版的画面描述:{base[:80]}"

    return base
