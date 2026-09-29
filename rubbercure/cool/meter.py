"""每模冷却水计量。

每个控制周期把流量对时间积分得到这一模用了多少水（梯形积分），同时把
``流量 × 进出水温差 × 水比热`` 对时间积分，得到冷却水从模具带走的热量。
计量随模次开始清零、模次结束结算，数据既用于"每一模用了多少水"的报表，
也用于判断流量是否真的在带走热量（只走水不降温说明换热异常）。
"""

from __future__ import annotations

import threading

# 1 m³ 水按 1000 kg 折算；流量单位 m³/h。
_WATER_DENSITY_KG_M3 = 1000.0
_SECONDS_PER_HOUR = 3600.0


class CoolantMeter:
    """按模次累计冷却水用量与带走热量。"""

    def __init__(self, specific_heat_kj_kg_k: float = 4.18) -> None:
        if specific_heat_kj_kg_k <= 0:
            raise ValueError("水的比热必须为正")
        self._cp = float(specific_heat_kj_kg_k)
        self._last_flow_m3_h: float | None = None
        self._water_m3 = 0.0
        self._heat_kj = 0.0
        self._samples = 0
        self._lock = threading.RLock()

    def reset(self) -> None:
        """模次开始时清零。"""

        with self._lock:
            self._last_flow_m3_h = None
            self._water_m3 = 0.0
            self._heat_kj = 0.0
            self._samples = 0

    def accumulate(self, flow_m3_h: float, delta_c: float, dt: float) -> dict:
        """累计一个控制周期的用水量与带走热量。"""

        if dt < 0:
            raise ValueError("计量周期不能为负")
        flow = max(0.0, float(flow_m3_h))
        with self._lock:
            if self._last_flow_m3_h is None:
                avg_flow = flow
            else:
                avg_flow = 0.5 * (self._last_flow_m3_h + flow)
            added_m3 = avg_flow / _SECONDS_PER_HOUR * dt
            added_heat_kj = (
                flow
                / _SECONDS_PER_HOUR
                * _WATER_DENSITY_KG_M3
                * self._cp
                * float(delta_c)
                * dt
            )
            self._last_flow_m3_h = flow
            self._water_m3 += added_m3
            self._heat_kj += max(0.0, added_heat_kj)
            self._samples += 1
            return {
                "added_water_m3": round(added_m3, 6),
                "added_heat_kj": round(max(0.0, added_heat_kj), 3),
                "total_water_m3": round(self._water_m3, 4),
                "total_heat_kj": round(self._heat_kj, 3),
            }

    @property
    def water_m3(self) -> float:
        with self._lock:
            return self._water_m3

    @property
    def heat_kj(self) -> float:
        with self._lock:
            return self._heat_kj

    @property
    def samples(self) -> int:
        with self._lock:
            return self._samples

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "water_m3": round(self._water_m3, 4),
                "heat_kj": round(self._heat_kj, 3),
                "samples": self._samples,
            }
