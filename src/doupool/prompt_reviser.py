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
    # v0.2.27:本地等待豆包超时 —— 浏览器层 deadline 到了还没收到结果,默认
    # 退款(用户没成功拿到视频 = 豆包大概率也没扣费)。
    TIMEOUT = "timeout"
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
    # v0.2.23:豆包近期常用的「我无法生成你要求的内容」类通用拒绝模板。
    # 此前只有「侵权 / 换个主题 / 无法返回该内容」会命中,新版拒绝文案
    # 走 polling 一直返 None,要 5min 后才被 timeout 兜住,期间用户视角
    # 「永远生成中」。把这些补上 → 立即触发 DoubaoContentRejected →
    # max_reject_retries(默认 2)自动改写重试,而不是直接失败。
    # 注:第一行覆盖「我无法生成/满足/响应/提供 / 抱歉我无法... / 暂时无法...」
    # 等各种组合 —— 在豆包对话上下文里这几个动词基本只出现在拒绝场景,
    # 误判代价低(改写一次不成功就 fall back safe template),漏判代价高(用户
    # 卡 5 分钟),值得放宽。
    re.compile(r"(?:我|抱歉.{0,30})?(?:暂时)?无法[\s\S]{0,5}?(?:生成|满足|响应|提供|返回)"),
    re.compile(r"不符合.*?(?:规范|准则|要求|政策|规定)"),
    re.compile(r"涉及.*?敏感"),
    re.compile(r"重新描述.{0,8}?试试"),
    re.compile(r"换个(?:要求|话题|方向|思路)再试试"),
]

_RATE_LIMIT_PATTERNS = [
    re.compile(r"今日(?:免费)?额度(?:已)?(?:用完|耗尽|不足)"),
    re.compile(r"quota.{0,20}(?:exhaust|limit)", re.IGNORECASE),
    re.compile(r"rate.{0,20}limit", re.IGNORECASE),
    # 注:旧版把「额度未扣除」塞这里(line 72),分类成 RATE_LIMITED
    # revise_prompt=False。但豆包近期新增文案是「视频生成失败,生成额度
    # 未扣除」—— 表面像「没扣费」实际是「提示词过不了审、豆包主动拒收、
    # 没生成、所以也没扣」,应该走 revise_prompt=True 改写 prompt 重试。
    # 单独「额度未扣除」在新文案里几乎只出现在该拒绝模板里,改下面 _POLICY_PATTERNS
    # 里加新条目匹配整段;此处不再单独 match,避免误判「正常扣费失败」
    # (那种真要进 RATE_LIMITED 退额度)。
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

# v0.2.27:本地 deadline 超时 —— 区分于 NETWORK(timeout 是本地等,网络层
# 没失败)。模式必须放在 _GENERATION_FAILED_PATTERNS **之前** 因为「视频生成
# 超时」包含「视频生成失败」子串,会被它先吃掉 → 退款路径不命中。
# 精确化到「生成」语境:connection timed out 这种纯网络错继续走 NETWORK
# (NETWORK 也退款,只是分类更准确)。
_TIMEOUT_PATTERNS = [
    re.compile(r"视频生成超时"),       # browser.py:1000 RuntimeError("视频生成超时")
    re.compile(r"生成超时"),
    re.compile(r"请求超时"),
    re.compile(r"等待.{0,8}(?:超时|超时未响应)"),
    re.compile(r"deadline.{0,10}exceeded", re.IGNORECASE),
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

    # v0.2.27:必须在 _GENERATION_FAILED_PATTERNS 之前(见 _TIMEOUT_PATTERNS 注释)。
    for pat in _TIMEOUT_PATTERNS:
        if pat.search(msg):
            return FailureInfo(FailureKind.TIMEOUT, retryable=True, revise_prompt=False, detail=pat.search(msg).group(0))

    for pat in _GENERATION_FAILED_PATTERNS:
        if pat.search(msg):
            return FailureInfo(FailureKind.GENERATION_FAILED, retryable=True, revise_prompt=True, detail=pat.search(msg).group(0))

    for pat in _INVALID_INPUT_PATTERNS:
        if pat.search(msg):
            return FailureInfo(FailureKind.INVALID_INPUT, retryable=False, revise_prompt=False, detail=pat.search(msg).group(0))

    return FailureInfo(FailureKind.UNKNOWN, retryable=True, revise_prompt=False, detail=msg or "(empty error)")


# ---------- Prompt 改写策略 ----------

# v0.2.25:用户新策略 —— 拒绝类失败时,在原 prompt 末尾追加这一段指令,让豆包
# 自己改写并继续生成。每次重试都基于「上次发送的 prompt」(浏览器层 retry 循环
# 把返回的 new_prompt 写回 prompt_to_send),所以这条后缀会被累积 append:
#   attempt 1 原文 + 后缀
#   attempt 2 (原文 + 后缀) + 后缀
#   attempt 3 (原文 + 后缀 + 后缀) + 后缀
# 不再做关键词剥离 / 软化前缀 / 安全模板兜底 — 全部交给豆包自己改写。
_REVISION_INSTRUCTION = "把这段提示词修改成不违反平台规则的提示词,并生成视频"


def revise_prompt(prompt: str, failure: FailureInfo, attempt: int = 1) -> str:
    """
    根据失败分类改写 prompt,用于重试。

    v0.2.25 行为变更:策略违规和生成失败时,统一在「上次发送的 prompt」末尾追加
    一段让豆包自己改写并重生成的指令。不再启发式剥关键词或替换为安全模板 —
    把改写权完全交给豆包。

    attempt: 1 = 第一次改写,2 = 第二次(更激进的策略)。当前实现下 attempt 只
    影响日志 / 未来扩展,不再区分 prompt 内容(累积同一段后缀已够覆盖)。

    返回改写后的 prompt。失败分类不需要改 prompt 时,返回原 prompt。
    """
    if not failure.revise_prompt:
        return prompt

    base = (prompt or "").strip()
    if not base:
        return base

    # v0.2.25:POLICY_VIOLATION / GENERATION_FAILED 走同一策略 —— append 指令串。
    # 浏览器层 retry 循环已把上次的 new_prompt 写回 prompt_to_send,所以这里 base
    # 就是「上次实际发给豆包的 prompt」,后缀会自然累积。
    if failure.kind in (FailureKind.POLICY_VIOLATION, FailureKind.GENERATION_FAILED):
        return f"{base} {_REVISION_INSTRUCTION}"

    return base
