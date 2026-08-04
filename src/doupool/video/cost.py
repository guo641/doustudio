"""v0.2.18:按 seedance 模型 + 视频时长计算额度消耗。

- mini:`0.2` 点/秒(向上取整)。5s=1、10s=2 点。
- fast(原 v2 / std):`0.4` 点/秒。5s=2、10s=4 点。
- 旧 seedance_v2.0 同 fast(兼容期,后端 allow-list 保留它,只是前端不展示)
- 向上取整;最低 1 点(避免 1 秒任务被算成 0)
- 未知 model 兜底 1 点/秒(不让它偷偷算成 0)

v0.2.18:费率从 mini=1.0/s / fast=1.5/s 调到 0.2 / 0.4,
v0.2.11~v0.2.17 期间默认 `daily_quota_mini/std=5`,原费率下
**任意 5 秒视频就吃光当日 mini 桶、5 秒 fast 直接超额**(用户线上实测:
mini 10s 视频扣 10 点,显示 10/5 全用完)。
新费率:mini 5s=1 / 10s=2、fast 5s=2 / 10s=4,与默认 5 点/天的桶
匹配 —— 一个 10s mini 视频占 2/5,5 个 10s mini 用完当天。
"""
from __future__ import annotations

from math import ceil


MODEL_COST_PER_SECOND: dict[str, float] = {
    "seedance_v2.0_mini": 0.2,
    "seedance_v2.0": 0.4,
    "seedance_v2.0_std": 0.4,
}


def quota_cost(model: str, duration: int) -> int:
    """返回该任务的 quota 消耗(向上取整)。

    示例(v0.2.18 费率):
      quota_cost("seedance_v2.0_mini", 5)  == 1
      quota_cost("seedance_v2.0_mini", 10) == 2
      quota_cost("seedance_v2.0_std", 5)   == 2   # ceil(2.0)
      quota_cost("seedance_v2.0_std", 10)  == 4
      quota_cost("seedance_v2.0_std", 1)   == 1   # ceil(0.4)=1,最低 1 点
      quota_cost("seedance_v2.0_std", 3)   == 2   # ceil(1.2)=2
    """
    duration = max(0, int(duration))
    rate = MODEL_COST_PER_SECOND.get(model)
    if rate is None:
        # 未知 model 兜底:1 点/秒(不让它偷偷算成 0)
        return max(1, duration)
    return max(1, ceil(duration * rate))