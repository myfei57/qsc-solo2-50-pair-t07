"""冷却水循环。

冷却水把硫化完的模具从工艺温度降到取件温度。循环泵负责开停，调节阀
（:class:`~rubbercure.cool.valve.CoolantValve`）负责连续调流量：泵启动时
阀门开到初始开度，之后由温差闭环逐周期调节。流量以阀位折算值为准，进出
口水温由现场仪表上报。冷却回路要和蒸汽回路配合：蒸汽未停之前禁止停冷却，
避免骤冷。
"""

from __future__ import annotations

import threading

from ..eventbus import Event, EventBus
from .valve import MAX_OPENING_PCT, CoolantValve


class CoolantCirculation:
    """冷却水循环泵、调节阀与进出口温度。"""

    def __init__(
        self,
        bus: EventBus,
        rated_flow_m3_h: float = 18.0,
        valve: CoolantValve | None = None,
    ) -> None:
        if rated_flow_m3_h <= 0:
            raise ValueError("冷却水额定流量必须为正")
        self._bus = bus
        self._rated = float(rated_flow_m3_h)
        self.valve = valve or CoolantValve(bus, rated_flow_m3_h=rated_flow_m3_h)
        self._running = False
        self._inlet_c = 0.0
        self._outlet_c = 0.0
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        with self._lock:
            return self._running

    @property
    def flow_m3_h(self) -> float:
        """当前实际流量：停泵时为零，运行时取调节阀折算流量。"""

        with self._lock:
            if not self._running:
                return 0.0
            return self.valve.flow_m3_h()

    def circulation_delta_c(self) -> float:
        with self._lock:
            return self._outlet_c - self._inlet_c

    def start(self, flow_m3_h: float | None = None, opening_pct: float = MAX_OPENING_PCT) -> float:
        """启动循环泵并把调节阀开到初始开度。

        兼容旧调用：传入 ``flow_m3_h`` 时按额定流量折算成开度。
        """

        with self._lock:
            self._running = True
            if flow_m3_h is not None:
                opening_pct = max(0.0, min(MAX_OPENING_PCT, float(flow_m3_h) / self._rated * MAX_OPENING_PCT))
            self.valve.set_opening(opening_pct)
            flow = self.flow_m3_h
        self._bus.publish(
            Event.of(
                "cool",
                "cool",
                "coolant_started",
                subject="coolant-pump",
                flow_m3_h=round(flow, 2),
                opening_pct=round(opening_pct, 2),
            )
        )
        return flow

    def regulate(self, opening_pct: float) -> float:
        """把调节阀设到闭环给出的开度，返回折算流量。"""

        with self._lock:
            self.valve.set_opening(float(opening_pct))
            return self.flow_m3_h

    def sample(self, inlet_c: float, outlet_c: float) -> dict:
        """记录一组进出口温度。"""

        with self._lock:
            self._inlet_c = float(inlet_c)
            self._outlet_c = float(outlet_c)
            return {
                "inlet_c": round(self._inlet_c, 3),
                "outlet_c": round(self._outlet_c, 3),
                "delta_c": round(self.circulation_delta_c(), 3),
            }

    def stop(self) -> float:
        """停循环泵并关阀。"""

        with self._lock:
            self._running = False
            self.valve.close()
        self._bus.publish(Event.of("cool", "cool", "coolant_stopped", subject="coolant-pump"))
        return 0.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "running": self._running,
                "flow_m3_h": round(self.flow_m3_h, 2),
                "inlet_c": round(self._inlet_c, 3),
                "outlet_c": round(self._outlet_c, 3),
                "delta_c": round(self.circulation_delta_c(), 3),
                "rated_flow_m3_h": self._rated,
                "valve": self.valve.snapshot(),
            }
