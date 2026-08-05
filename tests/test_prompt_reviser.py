"""prompt_reviser 单元测试"""

from __future__ import annotations

import pytest

from doupool.prompt_reviser import (
    FailureKind,
    classify_failure,
    revise_prompt,
)


# ---------- classify_failure ----------


class TestClassifyFailure:
    def test_policy_violation_chinese(self):
        info = classify_failure("生成内容中疑似包含侵权内容,无法返回该内容,换个主题再试试")
        assert info.kind == FailureKind.POLICY_VIOLATION
        assert info.retryable is True
        assert info.revise_prompt is True

    def test_policy_violation_short(self):
        info = classify_failure("换个主题再试试")
        assert info.kind == FailureKind.POLICY_VIOLATION

    # v0.2.23:豆包新文案「我暂时无法生成你要求的内容,请尝试输入其他要求」
    # 此前不在 _POLICY_PATTERNS → polling 一直 None,5min 后才 timeout。
    # 这些 case 是用户真实截图里的拒绝模板,必须命中 POLICY_VIOLATION
    # 才能让 run() retry loop 触发 prompt 改写。
    @pytest.mark.parametrize("msg", [
        "我暂时无法生成你要求的内容。请尝试输入其他要求",
        "抱歉,我无法满足你的请求",
        "抱歉,暂时无法生成此类内容",
        "我无法提供这个内容",
        "抱歉,这个请求涉及敏感内容,无法生成",
        "不符合平台内容规范,无法生成",
        "内容不符合社区规范,请重新描述试试",
        "换个要求再试试",
        # v0.2.24:回归 + 新增 verb 「返回」(用户实拍:「您请求的内容无法返回」)。
        # 之前 verb 集合只覆盖 生成/满足/响应/提供,漏「返回」导致另一类新文案
        # 仍会卡 5min timeout。补上后 scan_sse_for_policy_rejection 就能识别。
        "请重新描述一下再试试",
        "很抱歉,您请求的内容无法返回",
        "抱歉,此内容无法返回,请稍后再试",
    ])
    def test_policy_violation_new_templates_v0_2_24(self, msg):
        info = classify_failure(msg)
        assert info.kind == FailureKind.POLICY_VIOLATION, f"missed: {msg!r}"
        assert info.revise_prompt is True

    def test_rate_limited(self):
        info = classify_failure("今日免费额度已用完,请明天再来")
        assert info.kind == FailureKind.RATE_LIMITED
        assert info.retryable is True
        assert info.revise_prompt is False

    def test_network_timeout(self):
        info = classify_failure("Connection timed out after 30s")
        assert info.kind == FailureKind.NETWORK
        assert info.retryable is True

    def test_network_chinese(self):
        info = classify_failure("网络请求失败,请检查网络连接")
        assert info.kind == FailureKind.NETWORK

    def test_generation_failed(self):
        info = classify_failure("视频生成失败,请稍后重试")
        assert info.kind == FailureKind.GENERATION_FAILED
        assert info.retryable is True
        assert info.revise_prompt is True

    def test_invalid_input(self):
        info = classify_failure("参数无效: prompt 长度超过限制")
        # 不一定命中 INVALID_INPUT 模式,但应该是 NOT retryable 类
        assert info.kind in (FailureKind.INVALID_INPUT, FailureKind.UNKNOWN)
        assert info.retryable is False

    def test_unknown(self):
        info = classify_failure("some weird error nobody knows")
        assert info.kind == FailureKind.UNKNOWN
        assert info.retryable is True
        assert info.revise_prompt is False

    def test_empty_message(self):
        info = classify_failure("")
        assert info.kind == FailureKind.UNKNOWN
        assert info.detail == "(empty error)"

    def test_status_code_429(self):
        info = classify_failure("Too many requests", status_code=429)
        assert info.kind == FailureKind.RATE_LIMITED
        assert info.retryable is True

    def test_status_code_500(self):
        info = classify_failure("server error", status_code=503)
        assert info.kind == FailureKind.NETWORK
        assert info.retryable is True

    def test_status_code_400(self):
        info = classify_failure("bad", status_code=400)
        assert info.kind == FailureKind.INVALID_INPUT
        assert info.retryable is False

    def test_status_code_401(self):
        info = classify_failure("unauthorized", status_code=401)
        assert info.kind == FailureKind.POLICY_VIOLATION
        assert info.retryable is False

    def test_status_code_priority_over_text(self):
        # 即使文案是网络错,status=400 应该优先识别为 INVALID_INPUT
        info = classify_failure("Connection reset", status_code=400)
        assert info.kind == FailureKind.INVALID_INPUT


# ---------- revise_prompt ----------

# v0.2.25:策略变了 —— revise_prompt 不再剥关键词/套模板,而是在原 prompt 末尾
# 追加一句固定指令,让豆包自己改写并重生成。每次重试都由浏览器层 retry 循环
# 把上次的 new_prompt 写回 prompt_to_send → 后缀自然累积(累计 N 次失败 → 末尾
# 出现 N 段指令)。
_INSTRUCTION = "把这段提示词修改成不违反平台规则的提示词,并生成视频"


class TestRevisePrompt:
    def test_no_revise_for_non_revise_failure(self):
        # quota / network / invalid_input / unknown → 不动原 prompt
        prompt = "原始 prompt"
        for msg in ("网络超时", "今日额度已用完", "参数无效"):
            info = classify_failure(msg)
            assert revise_prompt(prompt, info) == prompt, msg

    def test_policy_violation_appends_instruction(self):
        # v0.2.25:POLICY_VIOLATION → 原 prompt + 空格 + 固定指令
        prompt = "一段美丽的风景"
        info = classify_failure("我暂时无法生成你要求的内容,请尝试输入其他要求")
        assert info.revise_prompt is True
        revised = revise_prompt(prompt, info)
        assert revised.startswith(prompt)
        assert _INSTRUCTION in revised
        # 不再做任何关键词剥离 — 原内容应保留
        assert "风景" in revised

    def test_policy_violation_attempt_2_keeps_accumulating(self):
        # v0.2.25:attempt=2 也走同一策略(累积),不再 attempt>=2 切换安全模板
        prompt = "一段美丽的风景"
        info = classify_failure("换个主题再试试")
        assert info.revise_prompt is True
        revised = revise_prompt(prompt, info, attempt=2)
        assert prompt in revised
        assert _INSTRUCTION in revised

    def test_generation_failed_uses_same_strategy(self):
        # v0.2.25:GENERATION_FAILED 也走 append 指令串,不再简化/截断
        prompt = "一段非常长的描述" * 20
        info = classify_failure("视频生成失败")
        assert info.revise_prompt is True
        revised = revise_prompt(prompt, info)
        assert prompt in revised
        assert _INSTRUCTION in revised
        # 不再截断: 长度应 >= 原 prompt 长度
        assert len(revised) >= len(prompt)

    def test_retry_loop_accumulates_suffix(self):
        # 模拟浏览器层 retry 循环:每次都把上次的 new_prompt 作为下次 base
        prompt = "原始 prompt"
        info = classify_failure("我暂时无法生成你要求的内容")
        revised1 = revise_prompt(prompt, info, attempt=1)
        revised2 = revise_prompt(revised1, info, attempt=2)
        revised3 = revise_prompt(revised2, info, attempt=3)
        # 三次累积,末尾应该出现三次指令
        assert revised1.count(_INSTRUCTION) == 1
        assert revised2.count(_INSTRUCTION) == 2
        assert revised3.count(_INSTRUCTION) == 3
        # 原 prompt 内容仍在开头(累积 append,不替换)
        assert revised3.startswith(prompt)

    def test_empty_prompt_returns_empty(self):
        info = classify_failure("违规")
        assert revise_prompt("", info) == ""
        assert revise_prompt("   ", info) == "   "

    def test_instruction_is_attached_not_replacing(self):
        # 关键不变量:原 prompt 永远在结果里,后缀只 append,不替换
        prompt = "特定可识别的画面描述 xyz123"
        info = classify_failure("换个主题再试试")
        revised = revise_prompt(prompt, info)
        assert "xyz123" in revised
        assert revised != prompt
