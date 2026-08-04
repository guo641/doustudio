"""cost 公式单测。

对齐豆包真实扣费:
- mini 1 点/秒(向上取整)
- fast 1.5 点/秒
- 每天每账号 50 点:mini 10s × 5 条 = 50(跑满),fast 10s × 3 ≈ 45(剩 5)
"""
from doupool.video.cost import quota_cost


def test_quota_cost_mini_per_second():
    assert quota_cost("seedance_v2.0_mini", 5) == 5
    assert quota_cost("seedance_v2.0_mini", 10) == 10
    assert quota_cost("seedance_v2.0_mini", 1) == 1


def test_quota_cost_fast_per_second_ceils():
    # fast = 1.5 点/秒,向上取整
    assert quota_cost("seedance_v2.0_std", 5) == 8  # ceil(7.5)
    assert quota_cost("seedance_v2.0_std", 10) == 15
    assert quota_cost("seedance_v2.0_std", 1) == 2  # ceil(1.5),最低 1 点
    assert quota_cost("seedance_v2.0_std", 2) == 3


def test_quota_cost_v2_legacy_alias():
    """seedance_v2.0 兼容期同 fast。"""
    assert quota_cost("seedance_v2.0", 5) == 8
    assert quota_cost("seedance_v2.0", 10) == 15


def test_quota_cost_unknown_model_falls_back_to_duration():
    """未知 model 兜底 1 点/秒(不让它偷偷算成 0)。"""
    assert quota_cost("some_unknown_model", 5) == 5
    assert quota_cost("some_unknown_model", 10) == 10
    # 0 秒也至少 1 点
    assert quota_cost("some_unknown_model", 0) == 1


def test_quota_cost_minimum_one():
    """任何情况都至少扣 1 点(避免被算成 0)。"""
    assert quota_cost("seedance_v2.0_mini", 0) == 1
    assert quota_cost("seedance_v2.0_std", 0) == 1
    # ceil(0.6 * 1.5) = 1,正好最低 1 点
    assert quota_cost("seedance_v2.0_std", 1) == 2


def test_quota_cost_negative_duration_treated_as_zero():
    """防御性:负数当 0 处理(返回最低 1)。"""
    assert quota_cost("seedance_v2.0_mini", -1) == 1


def test_quota_cost_matches_doubao_daily_50_points():
    """回归保护 — 默认 50 点/天桶下:
    - mini 10s × 5 条 = 50 点(刚好跑满,符合用户预期)
    - fast 10s × 3 条 = 45 点(剩 5 点)
    """
    daily = 50
    # 5 个 mini 10s 视频用满 50 点
    cost_10s_mini = quota_cost("seedance_v2.0_mini", 10)
    assert cost_10s_mini * 5 == daily
    # 3 个 fast 10s 用 45 点
    cost_10s_std = quota_cost("seedance_v2.0_std", 10)
    assert cost_10s_std * 3 == 45
    assert cost_10s_std * 3 < daily  # 还有余量