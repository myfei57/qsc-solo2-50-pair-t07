"""冷却回路单元。

把循环泵、调节阀、温差闭环、每模计量与告警装成一台硫化机的冷却回路，对外
暴露降温、采样、排放与停运动作。

冷却一个模次的推进由 :meth:`CoolingUnit.regulate` 完成：读入进出口温度 →
PI 按温差调节阀位 → 累计本模用水量与带走热量 → 判定模具是否冷透。冷透后
自动关阀停泵并出一模报表；超时未冷透则告警交人工，不再恒流常开着等人盯。
停运顺序由 ``stop_cooling`` 统一校验，控制台不会绕过它。
"""

from __future__ import annotations

import threading

from ..clock import Clock
from ..config import CoolLimits
from ..eventbus import Event, EventBus
from .alarm import CoolantAlarm
from .circulation import CoolantCirculation
from .drain import drain_coolant
from .flow import DeltaFlowController
from .meter import CoolantMeter
from .shot import CoolShotTracker
from .state import CoolPhase, CoolStateMachine

DEFAULT_DELTA_KP = 8.0
DEFAULT_DELTA_KI = 1.5
_COOL_START_OPENING_PCT = 100.0


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
        self.controller = DeltaFlowController(kp=DEFAULT_DELTA_KP, ki=DEFAULT_DELTA_KI)
        self.meter = CoolantMeter(limits.water_specific_heat_kj_kg_k)
        self.tracker = CoolShotTracker(clock, limits)
        self._mold_id: str | None = None
        self._thickness_mm: float | None = None
        self._last_shot: dict | None = None
        self._lock = threading.RLock()

    @property
    def mold_id(self) -> str | None:
        with self._lock:
            return self._mold_id

    @property
    def last_shot(self) -> dict | None:
        with self._lock:
            return dict(self._last_shot) if self._last_shot else None

    def set_mold(self, mold_id: str) -> None:
        with self._lock:
            self._mold_id = mold_id

    def transition_state(self, target: CoolPhase) -> CoolPhase:
        self.phase = self._state.transition(target)
        return self.phase

    def start_cooling(
        self,
        mold_id: str,
        thickness_mm: float | None = None,
        opening_pct: float = _COOL_START_OPENING_PCT,
    ) -> dict:
        """启动一个模次的冷却循环并清零计量。"""

        with self._lock:
            self.set_mold(mold_id)
            self._thickness_mm = None if thickness_mm is None else float(thickness_mm)
            self.transition_state(CoolPhase.CIRCULATING)
            self.controller.reset()
            self.meter.reset()
            self.tracker.begin()
            flow = self.circulation.start(opening_pct=opening_pct)
            result = {
                "mold_id": mold_id,
                "thickness_mm": self._thickness_mm,
                "flow_m3_h": round(flow, 2),
                "opening_pct": round(opening_pct, 2),
                "target_delta_c": self.limits.target_delta_c,
            }
        self._bus.publish(
            Event.of(
                "cool",
                "cool",
                "cooling_started",
                subject=mold_id,
                severity="info",
                **{k: v for k, v in result.items() if v is not None},
            )
        )
        return result

    def regulate(self, inlet_c: float, outlet_c: float, dt: float) -> dict:
        """推进一个冷却控制周期：调阀、计量、冷透判定，冷透则自动停泵。"""

        if dt <= 0:
            raise ValueError("冷却控制周期必须为正")
        with self._lock:
            sample = self.circulation.sample(inlet_c, outlet_c)
            delta = sample["delta_c"]
            opening = self.controller.update(delta, self.limits.target_delta_c, dt)
            # 冷却初期模具还烫、温差高于目标时，始终满阀冲流，加快降温；
            # 只有温差落到目标以下才交给 PI 收阀，避免末段恒流白白排水。
            if delta >= self.limits.target_delta_c:
                opening = 100.0
            opening = max(self.limits.valve_min_pct, opening)
            flow = self.circulation.regulate(opening)
            metered = self.meter.accumulate(flow, delta, dt)
            judge = self.tracker.observe(delta, dt)
            alarm = self.alarm.check(inlet_c, outlet_c, flow)
            if judge["done"]:
                self._complete_shot(reason="cooled")
            elif judge["timed_out"]:
                self._bus.publish(
                    Event.of(
                        "cool",
                        "cool",
                        "cooling_shot_timeout",
                        subject=self._mold_id or "coolant-loop",
                        severity="alarm",
                        elapsed_s=judge["elapsed_s"],
                        water_m3=metered["total_water_m3"],
                        delta_c=round(delta, 3),
                    )
                )
            return {
                **sample,
                "flow_m3_h": round(flow, 3),
                "opening_pct": round(opening, 2),
                "meter": metered,
                "judge": judge,
                "alarm": alarm,
                "phase": self.phase.value,
            }

    def _complete_shot(self, reason: str = "cooled") -> dict:
        """冷透后自动关阀停泵，结算本模报表。"""

        summary = self.tracker.summary(self.meter.water_m3, self.meter.heat_kj, self._thickness_mm)
        summary["mold_id"] = self._mold_id
        summary["reason"] = reason
        self.transition_state(CoolPhase.IDLE)
        self.circulation.stop()
        self._last_shot = summary
        self._bus.publish(
            Event.of(
                "cool",
                "cool",
                "cooling_shot_completed",
                subject=self._mold_id or "coolant-loop",
                severity="notice",
                **summary,
            )
        )
        return summary

    def complete_cooling(self, reason: str = "manual") -> dict:
        """人工／顺序提前结束当前模次冷却并结算；未在冷却则返回上一模报表。"""

        with self._lock:
            if self.phase is not CoolPhase.CIRCULATING:
                return self._last_shot or {}
            return self._complete_shot(reason=reason)

    def measure(self, inlet_c: float, outlet_c: float) -> dict:
        """记录冷却温度并做越限判定。"""

        sample = self.circulation.sample(inlet_c, outlet_c)
        alarm = self.alarm.check(inlet_c, outlet_c, self.circulation.flow_m3_h)
        sample["alarm"] = alarm
        return sample

    def drain(self) -> dict:
        """排空冷却管路。"""

        return drain_coolant(self, self._bus)

    def snapshot(self) -> dict:
        with self._lock:
            mold_id = self._mold_id
            shot = dict(self._last_shot) if self._last_shot else None
        return {
            "phase": self._state.phase.value,
            "history": self._state.history(),
            "mold_id": mold_id,
            "circulation": self.circulation.snapshot(),
            "controller": self.controller.snapshot(),
            "meter": self.meter.snapshot(),
            "elapsed_s": round(self.tracker.elapsed_s, 3),
            "last_shot": shot,
            "limits": self.limits.as_dict(),
        }
