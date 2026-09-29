"""温差闭环调流量。

模具厚薄不一样，同样的恒定流量带走的热量不一样：进出口温差偏大说明水没供够、
模温降得慢；温差偏小说明水在白流。调节器按温差给出目标流量——温差正好等于
目标时给额定流量，温差越大开得越大，温差越小关得越小——再夹到最小流量与
额定流量之间。最小流量是硬下限，防止调节阀关死造成局部断水。

调节器只算目标值，不直接动阀；阀位由冷却回路统一下发，报警与联锁仍走原链路。
"""

from __future__ import annotations

from ..config import CoolLimits

# 目标流量变化小于额定流量的该比例时保持不动，避免仪表噪声让调节阀反复抖动。
DEADBAND_RATIO = 0.01


class DeltaTFlowController:
    """按进出水温差决定冷却水目标流量。"""

    def __init__(self, limits: CoolLimits) -> None:
        self._target_delta = limits.target_delta_c
        self._min_flow = limits.min_flow_m3_h
        self._rated_flow = limits.rated_flow_m3_h
        self._last_flow = limits.rated_flow_m3_h

    @property
    def target_delta_c(self) -> float:
        return self._target_delta

    def compute_flow(self, delta_c: float) -> float:
        """按当前温差给出本周期目标流量（m³/h）。"""

        ratio = max(0.0, float(delta_c)) / self._target_delta
        target = self._min_flow + ratio * (self._rated_flow - self._min_flow)
        return round(min(self._rated_flow, target), 3)

    def update(self, delta_c: float) -> dict:
        """算出目标流量，并按死区决定本周期是否真的要动调节阀。"""

        desired = self.compute_flow(delta_c)
        deadband = self._rated_flow * DEADBAND_RATIO
        if abs(desired - self._last_flow) < deadband:
            desired = self._last_flow
        adjusted = desired != self._last_flow
        self._last_flow = desired
        return {
            "delta_c": round(float(delta_c), 3),
            "target_delta_c": self._target_delta,
            "flow_m3_h": round(desired, 3),
            "adjusted": adjusted,
        }

    def reset(self) -> None:
        """停泵后复位到额定流量，供下一模重新起步。"""

        self._last_flow = self._rated_flow
