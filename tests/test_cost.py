"""cost 公式单测。

v0.2.18:费率 mini=0.2/s、fast=0.4/s(原 v0.2.11~v0.2.17 是 1.0 / 1.5,
但 5 点的 daily_quota 下 5s 视频就爆桶)。
"""
from doupool.video.cost import quota_cost


def test_quota_cost_mini_per_second():
    """v0.2.18:mini 0.2 点/秒,向上取整。"""
    assert quota_cost("seedance_v2.0_mini", 5) == 1   # ceil(1.0)=1
    assert quota_cost("seedance_v2.0_mini", 10) == 2  # ceil(2.0)=2
    assert quota_cost("seedance_v2.0_mini", 1) == 1   # ceil(0.2)=1


def test_quota_cost_fast_per_second_ceils():
    """v0.2.18:fast 0.4 点/秒,向上取整。"""
    assert quota_cost("seedance_v2.0_std", 5) == 2   # ceil(2.0)=2
    assert quota_cost("seedance_v2.0_std", 10) == 4  # ceil(4.0)=4
    assert quota_cost("seedance_v2.0_std", 1) == 1   # ceil(0.4)=1,最低 1 点
    assert quota_cost("seedance_v2.0_std", 3) == 2   # ceil(1.2)=2


def test_quota_cost_v2_legacy_alias():
    """v0.2.18:seedance_v2.0 兼容期同 fast(0.4 点/秒)。"""
    assert quota_cost("seedance_v2.0", 5) == 2
    assert quota_cost("seedance_v2.0", 10) == 4


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
    # ceil(0.4 * 1) = 1,正好最低 1 点
    assert quota_cost("seedance_v2.0_std", 1) == 1


def test_quota_cost_negative_duration_treated_as_zero():
    """防御性:负数当 0 处理(返回最低 1)。"""
    assert quota_cost("seedance_v2.0_mini", -1) == 1


def test_quota_cost_fits_daily_quota_bucket():
    """v0.2.18:回归保护 — 默认 daily_quota_mini=5,5 个 10s mini 视频刚好不超额。

    之前 v0.2.17 费率下,1 个 10s mini 视频就扣 10 点(超额 5),整个桶爆掉。
    新费率下:5 个 10s mini = 5 * 2 = 10 点,2 个 10s mini = 4 点(剩 1 点
    可用),用户不会再看到「额度已用完」黑盒。
    """
    from doupool.video.cost import quota_cost
    daily_mini = 5
    cost_10s_mini = quota_cost("seedance_v2.0_mini", 10)
    # 2 个 10s mini 用掉 4/5
    assert cost_10s_mini * 2 == 4
    assert cost_10s_mini * 2 < daily_mini
    # 5 个 10s mini 才会触顶
    assert cost_10s_mini * 5 > daily_mini