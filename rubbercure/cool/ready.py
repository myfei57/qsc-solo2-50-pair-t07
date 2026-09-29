"""冷却到位判定。

能不能停泵靠模具温度而不是靠人盯：出水温度降到取件温度以下，并且进出水温差
收敛到目标温差附近，说明模具已经凉透、水不再需要带走多少热量。两个条件同时
满足并保持一个确认时长，才判定本模冷却到位，避免模芯还没凉、表面先凉造成的
误停。条件一旦破坏，保持计时清零，重新累计。
"""

from __future__ import annotations

from ..config import CoolLimits


class CoolReadyHold:
    """按取件温度、温差与保持时长判定冷却是否到位。"""

    def __init__(self, limits: CoolLimits) -> None:
        self._demold_temp_c = limits.demold_temp_c
        self._target_delta_c = limits.target_delta_c
        self._hold_s = limits.ready_hold_s
        self._held_s = 0.0
        self._ready = False

    def conditions_met(self, outlet_c: float, delta_c: float) -> bool:
        return outlet_c <= self._demold_temp_c and delta_c <= self._target_delta_c

    def update(self, outlet_c: float, delta_c: float, dt_s: float) -> dict:
        """推进一个周期的到位保持计时。"""

        if dt_s < 0:
            raise ValueError("冷却到位保持时长不能为负")
        if self.conditions_met(outlet_c, delta_c):
            self._held_s += float(dt_s)
        else:
            self._held_s = 0.0
        was_ready = self._ready
        self._ready = self._held_s >= self._hold_s
        return {
            "outlet_c": round(float(outlet_c), 3),
            "demold_temp_c": self._demold_temp_c,
            "delta_c": round(float(delta_c), 3),
            "held_s": round(self._held_s, 3),
            "required_s": self._hold_s,
            "ready": self._ready,
            "became_ready": self._ready and not was_ready,
        }

    def is_ready(self) -> bool:
        return self._ready

    def reset(self) -> None:
        """下一摸重新计时。"""

        self._held_s = 0.0
        self._ready = False

    def snapshot(self) -> dict:
        return {
            "demold_temp_c": self._demold_temp_c,
            "target_delta_c": self._target_delta_c,
            "required_s": self._hold_s,
            "held_s": round(self._held_s, 3),
            "ready": self._ready,
        }
