"""冷却水调节阀。

冷却回路原来只有泵的运行／停止两态，流量恒定，模具厚薄全靠人盯。调节阀把
流量变成可连续调节的现场量：温差闭环输出开度（0~100%），阀门据此给出实际
流量。阀位与蒸汽阀一样是不可逆读的现场量，控制器只通过本对象读写，不允许
绕过它直接假定流量。
"""

from __future__ import annotations

import threading

from ..eventbus import Event, EventBus

MAX_OPENING_PCT = 100.0
_MIN_OPENING_PCT = 0.0


class CoolantValve:
    """冷却水电动调节阀。"""

    def __init__(self, bus: EventBus, rated_flow_m3_h: float = 18.0) -> None:
        if rated_flow_m3_h <= 0:
            raise ValueError("冷却水调节阀额定流量必须为正")
        self._bus = bus
        self._rated = float(rated_flow_m3_h)
        self._opening_pct = 0.0
        self._lock = threading.RLock()

    @property
    def opening_pct(self) -> float:
        with self._lock:
            return self._opening_pct

    @property
    def rated_flow_m3_h(self) -> float:
        return self._rated

    def is_open(self, tolerance_pct: float = 0.5) -> bool:
        with self._lock:
            return self._opening_pct > tolerance_pct

    def flow_m3_h(self) -> float:
        """按开度折算流量（线性阀特性）。"""

        with self._lock:
            return self._rated * self._opening_pct / MAX_OPENING_PCT

    def set_opening(self, opening_pct: float) -> float:
        """把阀门开到指定开度，返回夹幅后的实际开度。"""

        with self._lock:
            self._opening_pct = max(_MIN_OPENING_PCT, min(MAX_OPENING_PCT, float(opening_pct)))
            opening = self._opening_pct
            flow = self.flow_m3_h()
        self._bus.publish(
            Event.of(
                "cool",
                "cool",
                "coolant_valve_set",
                subject="coolant-valve",
                opening_pct=round(opening, 2),
                flow_m3_h=round(flow, 3),
            )
        )
        return opening

    def close(self) -> float:
        """关阀。"""

        with self._lock:
            self._opening_pct = 0.0
        self._bus.publish(Event.of("cool", "cool", "coolant_valve_closed", subject="coolant-valve"))
        return 0.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "opening_pct": round(self._opening_pct, 2),
                "open": self.is_open(),
                "flow_m3_h": round(self.flow_m3_h(), 3),
                "rated_flow_m3_h": self._rated,
            }
