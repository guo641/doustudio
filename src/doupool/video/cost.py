"""v0.2.11:按 seedance 模型 + 视频时长计算额度消耗。

- mini:1 点/秒
- fast(原 v2 / std):1.5 点/秒
- 旧 seedance_v2.0 同 fast(兼容期,后端 allow-list 保留它,只是前端不展示)
- 向上取整;最低 1 点(避免 1 秒任务被算成 0)
- 未知 model 兜底 duration 本身(不让它偷偷算成 0)
"""
from __future__ import annotations

from math import ceil


MODEL_COST_PER_SECOND: dict[str, float] = {
    "seedance_v2.0_mini": 1.0,
    "seedance_v2.0": 1.5,
    "seedance_v2.0_std": 1.5,
}


def quota_cost(model: str, duration: int) -> int:
    """返回该任务的 quota 消耗(向上取整)。

    示例:
      quota_cost("seedance_v2.0_mini", 5)  == 5
      quota_cost("seedance_v2.0_mini", 10) == 10
      quota_cost("seedance_v2.0_std", 5)   == 8   # ceil(7.5)
      quota_cost("seedance_v2.0_std", 10)  == 15
      quota_cost("seedance_v2.0_std", 1)   == 2   # ceil(1.5)
    """
    duration = max(0, int(duration))
    rate = MODEL_COST_PER_SECOND.get(model)
    if rate is None:
        # 未知 model 兜底:1 点/秒(不让它偷偷算成 0)
        return max(1, duration)
    return max(1, ceil(duration * rate))