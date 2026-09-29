"""开模顺序。

开模顺序固定为：计时完成、启动冷却并按温差自动调流量直到冷透停泵、蒸汽
泄压、开模到位、取件、按模复位完成闩锁。先泄压再开模是硬约束，取件之后
才能复位闩锁——顺序错了要么顶裂制品，要么让调度误判哪台机可以接下一模。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..clock import Clock
from ..config import PlatformConfig
from ..cool.simulator import MoldThermalSpec, ThermalModel
from ..eventbus import EventBus
from ..lift.open import open_mold
from ..mold.registry import MoldRegistry
from ..press.press import CuringPress
from ..press.transitions import PressPhase
from ..timer.curve import CurveBook
from ..timer.done import complete_cure
from ..timer.latch import CureLatch
from .selfcheck import SequenceRecorder, verify_order

OPEN_ORDER: tuple[tuple[str, str, str], ...] = (
    ("timer_complete", "cooling_start", "计时完成后先启动模温冷却"),
    ("cooling_start", "steam_vent", "冷却冷透停泵后才允许泄压"),
    ("steam_vent", "lift_open", "先泄压再开模"),
    ("lift_open", "part_removed", "开模到位后才取件"),
    ("part_removed", "latch_reset", "取件后按模复位完成闩锁"),
)


@dataclass
class OpenSequence:
    """开模顺序执行器。"""

    clock: Clock
    bus: EventBus
    latch: CureLatch
    press: CuringPress
    recorder: SequenceRecorder = field(init=False)
    config: PlatformConfig | None = None
    registry: MoldRegistry | None = None
    curves: CurveBook | None = None
    cool_dt: float = 1.0

    def __post_init__(self) -> None:
        self.recorder = SequenceRecorder(self.bus, "openmold")

    def _cooling_thickness(self) -> float:
        """取硫化机记住的在机模具厚度，缺省用平台默认厚度。"""

        stored = self.press.mold_thickness_mm
        if stored is not None:
            return stored
        if self.config is not None:
            return self.config.press.default_thickness_mm
        return 0.0

    def _drive_cooling(self, mold_id: str, thickness: float) -> dict:
        """推进温差闭环直到冷透自动停泵（无现场仪表时用集总热模型供水温）。"""

        limits = self.config.cool if self.config else None
        cp = limits.water_specific_heat_kj_kg_k if limits else 4.18
        spec = MoldThermalSpec(
            mold_id=mold_id,
            thickness_mm=max(thickness, 1.0),
            heat_capacity_kj_k=60.0 + 16.0 * max(thickness, 1.0),
            conductance_kw_k=4.0,
        )
        model = ThermalModel(spec, cp)
        max_steps = int((limits.max_cool_s if limits else 1200.0) / self.cool_dt) + 2
        last: dict = {}
        for _ in range(max_steps):
            self.clock.advance(self.cool_dt)
            outlet, _delta, _power = model.step(
                self.press.cooling.circulation.flow_m3_h, self.cool_dt
            )
            last = self.press.cooling.regulate(spec.inlet_c, outlet, self.cool_dt)
            if last["judge"]["done"] or last["judge"]["timed_out"]:
                break
        return self.press.cooling.last_shot or {}

    def run(self) -> dict:
        """按开模顺序执行，返回执行报告。"""

        self.recorder.clear()
        press = self.press
        mold_id = press.require_mold()

        if press.phase in (PressPhase.CURING, PressPhase.COOLING):
            press.transition(PressPhase.VENTING)
        finished = complete_cure(press.timer, self.latch, self.bus)
        self.recorder.note(
            "timer_complete",
            "timer",
            mold_id=mold_id,
            verdict=finished["verdict"],
            latch_set=finished["latch_set"],
        )

        thickness = self._cooling_thickness()
        cooling = press.cooling.start_cooling(
            mold_id, thickness_mm=thickness if thickness > 0.0 else None
        )
        self.recorder.note(
            "cooling_start",
            "cool",
            mold_id=mold_id,
            flow_m3_h=cooling["flow_m3_h"],
            target_delta_c=cooling["target_delta_c"],
        )
        if self.config is not None:
            shot = self._drive_cooling(mold_id, max(thickness, self.config.press.default_thickness_mm))
        else:
            shot = {}

        vent = press.steam.vent_pressure(mold_id)
        self.recorder.note(
            "steam_vent", "steam", mold_id=mold_id, residual_kpa=round(vent.residual_kpa, 3)
        )

        press.transition(PressPhase.OPENING)
        opened = open_mold(press.lift, vent, self.bus, mold_id)
        self.recorder.note(
            "lift_open", "lift", mold_id=mold_id, opening_pct=opened["opening_pct"]
        )

        press.clear_mold()
        self.recorder.note("part_removed", "press", mold_id=mold_id)

        entry = self.latch.reset(mold_id, self.clock.now())
        self.recorder.note(
            "latch_reset",
            "timer",
            mold_id=mold_id,
            released=bool(entry and entry.released),
        )

        press.finalize_cycle()
        press.idle()
        return self.report(mold_id, shot)

    def report(self, mold_id: str, shot: dict | None = None) -> dict:
        return {
            "mold_id": mold_id,
            "steps": self.recorder.names(),
            "violations": verify_order(self.recorder.names(), OPEN_ORDER),
            "latch": self.latch.snapshot(),
            "press_phase": self.press.phase.value,
            "cooling_shot": shot,
        }
