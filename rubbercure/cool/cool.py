"""冷却回路单元。

把循环泵、温差调流量、每模用水计量、到位判定、告警与排放装成一台硫化机的
冷却回路，对外暴露降温、采样、排放与停运四个动作。控制周期每报一组进出口
水温，回路就按温差调一次调节阀、按流量积分一次水量、推进一步到位保持计时；
模具凉透并保持够时长后，``cooling_ready`` 事件给出"可以停"的信号，停运顺序
仍由 ``stop_cooling`` 统一校验蒸汽联锁，控制台不会绕过它。
"""

from __future__ import annotations

import threading

from ..clock import Clock
from ..config import CoolLimits
from ..eventbus import Event, EventBus
from .alarm import CoolantAlarm
from .circulation import CoolantCirculation
from .drain import drain_coolant
from .flow import DeltaTFlowController
from .meter import WaterMeter
from .ready import CoolReadyHold
from .state import CoolPhase, CoolStateMachine


class CoolingUnit:
    """一台硫化机的冷却回路。"""

    def __init__(self, clock: Clock, bus: EventBus, limits: CoolLimits) -> None:
        self.clock = clock
        self.limits = limits
        self._bus = bus
        self.phase = CoolPhase.IDLE
        self._state = CoolStateMachine()
        self.circulation = CoolantCirculation(bus, rated_flow_m3_h=limits.rated_flow_m3_h)
        self.alarm = CoolantAlarm(bus, limits)
        self.flow_control = DeltaTFlowController(limits)
        self.ready_hold = CoolReadyHold(limits)
        self.meter = WaterMeter(bus)
        self._mold_id: str | None = None
        self._lock = threading.RLock()

    @property
    def mold_id(self) -> str | None:
        with self._lock:
            return self._mold_id

    def set_mold(self, mold_id: str) -> None:
        with self._lock:
            self._mold_id = mold_id

    def transition_state(self, target: CoolPhase) -> CoolPhase:
        self.phase = self._state.transition(target)
        return self.phase

    def start_cooling(self, mold_id: str) -> dict:
        """启动冷却循环，按额定流量起步并为本模开盖计水。"""

        self.set_mold(mold_id)
        self.transition_state(CoolPhase.CIRCULATING)
        self.ready_hold.reset()
        self.flow_control.reset()
        flow = self.circulation.start()
        self.meter.begin(mold_id, self.clock.now())
        result = {"mold_id": mold_id, "flow_m3_h": round(flow, 2)}
        self._bus.publish(
            Event.of("cool", "cool", "cooling_started", subject=mold_id, severity="info", **result)
        )
        return result

    def measure(self, inlet_c: float, outlet_c: float) -> dict:
        """记录冷却温度并做越限判定（不调流量、不积水量）。"""

        sample = self.circulation.sample(inlet_c, outlet_c)
        alarm = self.alarm.check(inlet_c, outlet_c, self.circulation.flow_m3_h)
        sample["alarm"] = alarm
        return sample

    def regulate(self, inlet_c: float, outlet_c: float, dt_s: float) -> dict:
        """一个控制周期的冷却调节：采样 → 调流量 → 计水 → 判到位。"""

        sample = self.circulation.sample(inlet_c, outlet_c)
        with self._lock:
            if self.phase is not CoolPhase.CIRCULATING:
                raise RuntimeError("冷却循环未运行，不能调流量")
            regulation = self.flow_control.update(sample["delta_c"])
            self.circulation.apply_flow(regulation["flow_m3_h"])
            volume = self.meter.accumulate(regulation["flow_m3_h"], dt_s)
            ready = self.ready_hold.update(outlet_c, sample["delta_c"], dt_s)
            alarm = self.alarm.check(inlet_c, outlet_c, self.circulation.flow_m3_h)
            if ready["became_ready"]:
                self._bus.publish(
                    Event.of(
                        "cool",
                        "cool",
                        "cooling_ready",
                        subject=self._mold_id or "coolant-loop",
                        severity="notice",
                        mold_id=self._mold_id,
                        outlet_c=ready["outlet_c"],
                        held_s=ready["held_s"],
                    )
                )
        sample["alarm"] = alarm
        sample["regulation"] = regulation
        sample["ready"] = ready
        sample["volume_m3"] = round(volume, 4)
        return sample

    def is_ready(self) -> bool:
        return self.ready_hold.is_ready()

    def stop_pump(self, moment: float | None = None) -> dict:
        """停泵并封账本模用水（蒸汽联锁由调用方先校验）。"""

        self.transition_state(CoolPhase.STOPPED)
        flow = self.circulation.stop()
        report = self.meter.close(self.clock.now() if moment is None else moment)
        result = {"mold_id": self._mold_id, "flow_m3_h": round(flow, 3), "water": report}
        self._bus.publish(
            Event.of(
                "cool",
                "cool",
                "cooling_stopped",
                subject="coolant-loop",
                severity="notice",
                **result,
            )
        )
        return result

    def drain(self) -> dict:
        """排空冷却管路。"""

        return drain_coolant(self, self._bus)

    def snapshot(self) -> dict:
        with self._lock:
            mold_id = self._mold_id
        return {
            "phase": self._state.phase.value,
            "history": self._state.history(),
            "mold_id": mold_id,
            "circulation": self.circulation.snapshot(),
            "ready": self.ready_hold.snapshot(),
            "meter": self.meter.snapshot(),
            "limits": self.limits.as_dict(),
        }
