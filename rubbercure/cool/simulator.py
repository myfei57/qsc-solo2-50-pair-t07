"""冷却工艺仿真。

现场没有真机联调时，用集总热容模型代替模具与水路，驱动真实的
:class:`~rubbercure.cool.cool.CoolingUnit` 温差闭环跑完整一模：

* 模具一个温度、一个热容 ``C``（厚薄模具热容差好几倍）；
* 换热功率 ``U·(模温-出水温)``，稳态下出水温满足
  ``U·(模温-出水) = 质量流量·比热·(出水-进水)``，直接解出，无需迭代；
* 模温按 ``C·dT = -Q·dt`` 显式推进。

仿真同时跑一路"恒流全开"基线，回答两个问题：按温差调流量后每一模省多少
水，冷却时间变长还是变短。仿真只用于验证与演示，不进入真机控制链。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..clock import ManualClock
from ..config import CoolLimits
from ..eventbus import EventBus
from .cool import CoolingUnit

_SECONDS_PER_HOUR = 3600.0
_WATER_DENSITY_KG_M3 = 1000.0


@dataclass(frozen=True)
class MoldThermalSpec:
    """模具集总热参数：热容越大降温越慢（厚模）。"""

    mold_id: str
    thickness_mm: float
    heat_capacity_kj_k: float
    conductance_kw_k: float
    start_c: float = 120.0
    inlet_c: float = 28.0


@dataclass
class ThermalModel:
    """单集总点模具 + 水路的显式热模型。"""

    spec: MoldThermalSpec
    cp_kj_kg_k: float
    mold_c: float = 0.0

    def __post_init__(self) -> None:
        if self.mold_c == 0.0:
            self.mold_c = self.spec.start_c

    def step(self, flow_m3_h: float, dt: float) -> tuple[float, float, float]:
        """推进 ``dt`` 秒，返回 (出水温, 温差, 换热功率 kW)。"""

        mass_kg_s = max(flow_m3_h, 0.0) / _SECONDS_PER_HOUR * _WATER_DENSITY_KG_M3
        w = mass_kg_s * self.cp_kj_kg_k  # 水路热容流率，kW/K
        if w <= 0.0:
            return self.spec.inlet_c, 0.0, 0.0
        ua = self.spec.conductance_kw_k
        outlet = (ua * self.mold_c + w * self.spec.inlet_c) / (ua + w)
        outlet = max(self.spec.inlet_c, outlet)
        power_kw = w * (outlet - self.spec.inlet_c)
        self.mold_c -= power_kw * dt / self.spec.heat_capacity_kj_k
        self.mold_c = max(self.spec.inlet_c, self.mold_c)
        return outlet, outlet - self.spec.inlet_c, power_kw


def run_cooling_shot(
    unit: CoolingUnit,
    spec: MoldThermalSpec,
    dt: float = 1.0,
    *,
    fixed_opening_pct: float | None = None,
    trace_every: int = 30,
) -> dict:
    """用热模型驱动冷却回路跑一模，返回模次报表与轨迹。

    ``fixed_opening_pct`` 给定时旁路温差闭环，阀门恒开该开度（恒流基线），
    但计量与冷透判停走同一套组件，保证两种方式可比。
    """

    if dt <= 0:
        raise ValueError("仿真步长必须为正")
    limits = unit.limits
    model = ThermalModel(spec, limits.water_specific_heat_kj_kg_k)
    unit.start_cooling(spec.mold_id, spec.thickness_mm)
    trace: list[dict] = []
    ticks = 0
    while True:
        ticks += 1
        unit.clock.advance(dt)
        if fixed_opening_pct is not None:
            unit.circulation.valve.set_opening(float(fixed_opening_pct))
        flow = unit.circulation.flow_m3_h
        outlet, _delta, _power = model.step(flow, dt)
        if fixed_opening_pct is not None:
            result = _fixed_step(unit, spec.inlet_c, outlet, dt)
        else:
            result = unit.regulate(spec.inlet_c, outlet, dt)
        if ticks % trace_every == 0 or result["judge"]["done"] or result["judge"]["timed_out"]:
            trace.append(
                {
                    "t_s": round(ticks * dt, 1),
                    "mold_c": round(model.mold_c, 2),
                    "outlet_c": round(outlet, 2),
                    "delta_c": result["delta_c"],
                    "flow_m3_h": result["flow_m3_h"],
                    "opening_pct": result["opening_pct"],
                    "water_m3": result["meter"]["total_water_m3"],
                }
            )
        if result["judge"]["done"] or result["judge"]["timed_out"]:
            break
        if ticks * dt > limits.max_cool_s + dt:
            break
    return {"shot": unit.last_shot or {}, "trace": trace, "final_mold_c": round(model.mold_c, 3)}


def _fixed_step(unit: CoolingUnit, inlet_c: float, outlet_c: float, dt: float) -> dict:
    """恒流基线一步：计量与判停走真实组件，阀位不交给 PI。"""

    sample = unit.circulation.sample(inlet_c, outlet_c)
    flow = unit.circulation.flow_m3_h
    metered = unit.meter.accumulate(flow, sample["delta_c"], dt)
    judge = unit.tracker.observe(sample["delta_c"], dt)
    if judge["done"]:
        # 与闭环一样走统一的模次结算（关阀停泵、出报表），只是标注为基线。
        unit.complete_cooling(reason="baseline")
    return {
        **sample,
        "flow_m3_h": round(flow, 3),
        "opening_pct": round(unit.circulation.valve.opening_pct, 2),
        "meter": metered,
        "judge": judge,
    }


def compare_control_modes(
    limits: CoolLimits,
    specs: tuple[MoldThermalSpec, ...],
    dt: float = 1.0,
) -> dict:
    """对每副模具分别跑温差闭环与恒流全开，输出对比。"""

    shots: list[dict] = []
    for spec in specs:
        controlled = run_cooling_shot(
            CoolingUnit(ManualClock(), EventBus(), limits), spec, dt=dt
        )
        baseline = run_cooling_shot(
            CoolingUnit(ManualClock(), EventBus(), limits),
            spec,
            dt=dt,
            fixed_opening_pct=100.0,
        )
        c_shot, b_shot = controlled["shot"], baseline["shot"]
        saved_m3 = max(0.0, b_shot["water_m3"] - c_shot["water_m3"])
        saved_ratio = saved_m3 / b_shot["water_m3"] if b_shot["water_m3"] else 0.0
        shots.append(
            {
                "mold_id": spec.mold_id,
                "thickness_mm": spec.thickness_mm,
                "controlled": c_shot,
                "constant_full_flow": b_shot,
                "saved_water_m3": round(saved_m3, 3),
                "saved_ratio": round(saved_ratio, 4),
                "extra_time_s": round(c_shot["duration_s"] - b_shot["duration_s"], 3),
                "trace": controlled["trace"],
            }
        )
    return {"shots": shots}
