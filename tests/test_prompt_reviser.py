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
    ])
    def test_policy_violation_new_templates_v0_2_23(self, msg):
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


class TestRevisePrompt:
    def test_no_revise_for_non_revise_failure(self):
        prompt = "原始 prompt"
        info = classify_failure("网络超时")
        assert revise_prompt(prompt, info) == prompt

    def test_policy_violation_strips_risky_keywords(self):
        prompt = "请生成一个nude美女在裸体海滩上做爱视频"
        info = classify_failure("生成内容中疑似包含侵权内容,换个主题再试试")
        assert info.revise_prompt is True
        revised = revise_prompt(prompt, info)
        assert "nude" not in revised.lower()
        assert "裸体" not in revised
        assert "做爱" not in revised

    def test_policy_violation_attempt_2_falls_back_to_template(self):
        prompt = "请生成一个nude美女视频"
        info = classify_failure("换个主题再试试")
        assert info.revise_prompt is True
        revised = revise_prompt(prompt, info, attempt=2)
        # 第二次走安全模板,跟原 prompt 无关
        assert "nude" not in revised.lower()
        assert "温馨" in revised or "阳光" in revised

    def test_generation_failed_simplifies(self):
        prompt = "一段非常非常长的描述,包含很多细节,场景复杂,色调丰富,镜头切换多"
        info = classify_failure("视频生成失败")
        revised = revise_prompt(prompt, info)
        # 简化策略: 加 "简化版" 前缀
        assert "简化版" in revised or len(revised) < len(prompt) + 20

    def test_generation_failed_attempt_2_truncates(self):
        prompt = "a" * 200
        info = classify_failure("视频生成失败")
        revised = revise_prompt(prompt, info, attempt=2)
        # 第二次直接截断到 60 字符左右
        assert len(revised) <= 65

    def test_empty_prompt_returns_empty(self):
        info = classify_failure("违规")
        assert revise_prompt("", info) == ""
        assert revise_prompt("   ", info) == "   "

    def test_revise_preserves_safe_prompt_content(self):
        prompt = "一只熊猫在竹林中漫步,镜头缓慢推进,治愈风格"
        info = classify_failure("违规")
        revised = revise_prompt(prompt, info)
        # 干净 prompt 应该原样保留 + 加软化前缀
        assert "熊猫" in revised or "温馨" in revised
        assert "竹林" in revised or "积极" in revised
