"""冷却水按温差自动调流、每模计量与冷透自动停泵。"""

from __future__ import annotations

import pytest

from rubbercure.clock import ManualClock
from rubbercure.config import default_config
from rubbercure.cool.cool import CoolingUnit
from rubbercure.cool.flow import DeltaFlowController
from rubbercure.cool.meter import CoolantMeter
from rubbercure.cool.simulator import MoldThermalSpec, run_cooling_shot
from rubbercure.cool.state import CoolPhase
from rubbercure.cool.valve import CoolantValve
from rubbercure.eventbus import EventBus


def _unit(clock=None) -> CoolingUnit:
    return CoolingUnit(clock or ManualClock(), EventBus(), default_config().cool)


def test_valve_clamps_opening_and_maps_to_flow(platform):
    valve = CoolantValve(platform.bus, rated_flow_m3_h=20.0)
    assert valve.set_opening(150.0) == 100.0
    assert valve.flow_m3_h() == 20.0
    assert valve.set_opening(-5.0) == 0.0
    assert valve.flow_m3_h() == 0.0
    assert valve.set_opening(50.0) == 50.0
    assert valve.flow_m3_h() == pytest.approx(10.0)


def test_delta_controller_opens_for_large_delta_and_closes_for_small():
    pi = DeltaFlowController(kp=8.0, ki=1.5)
    big = pi.update(measured_delta_c=12.0, target_delta_c=5.0, dt=1.0)
    assert big > 50.0  # 大温差要求加流量，输出明显开大
    pi.reset()
    small = pi.update(measured_delta_c=0.5, target_delta_c=5.0, dt=1.0)
    assert small == 0.0  # 小温差关小，夹到零阀位


def test_meter_accumulates_water_and_heat(platform):
    meter = CoolantMeter(4.18)
    meter.reset()
    meter.accumulate(18.0, 5.0, 3600.0)
    assert meter.water_m3 == pytest.approx(18.0)
    # 18 m³ × 1000 kg/m³ × 4.18 kJ/(kg·K) × 5 K
    assert meter.heat_kj == pytest.approx(18.0 * 1000.0 * 4.18 * 5.0)


def test_regulate_closes_valve_when_delta_stays_below_threshold(platform):
    unit = CoolingUnit(platform.clock, platform.bus, default_config().cool)
    unit.start_cooling("mold-S", thickness_mm=3.0)
    # 前两个周期温差高于目标，满阀冲流，梯形积分累计到水；之后温差极低。
    platform.clock.advance(1.0)
    unit.regulate(30.0, 40.0, 1.0)
    platform.clock.advance(1.0)
    first = unit.regulate(30.0, 40.0, 1.0)
    assert first["meter"]["total_water_m3"] > 0
    result = first
    limits = default_config().cool
    for _ in range(int(limits.stop_confirm_s) + 2):
        platform.clock.advance(1.0)
        result = unit.regulate(30.0, 30.4, 1.0)
    assert result["judge"]["done"] is True
    assert unit.phase is CoolPhase.IDLE
    assert unit.circulation.flow_m3_h == 0.0
    assert unit.last_shot["verdict"] == "cooled"
    assert unit.last_shot["water_m3"] > 0
    assert unit.last_shot["thickness_mm"] == 3.0


def test_regulate_keeps_valve_open_while_mold_is_hot(platform):
    unit = CoolingUnit(platform.clock, platform.bus, default_config().cool)
    unit.start_cooling("mold-H")
    platform.clock.advance(1.0)
    result = unit.regulate(28.0, 42.0, 1.0)
    assert result["opening_pct"] == 100.0
    assert result["flow_m3_h"] == pytest.approx(18.0)
    assert result["judge"]["done"] is False


def test_timeout_alarm_when_never_cools(platform):
    limits = default_config().cool
    unit = CoolingUnit(platform.clock, platform.bus, limits)
    unit.start_cooling("mold-T")
    result = None
    steps = int(limits.max_cool_s)
    for _ in range(steps):
        platform.clock.advance(1.0)
        result = unit.regulate(28.0, 40.0, 1.0)
    assert result["judge"]["timed_out"] is True
    actions = [event.action for event in platform.bus.history("cool")]
    assert "cooling_shot_timeout" in actions
    # 超时不自动关阀，留给人工
    assert unit.phase is CoolPhase.CIRCULATING


def test_simulated_thin_and_thick_shots_self_stop_and_save_water():
    limits = default_config().cool
    cases = (
        MoldThermalSpec("mold-thin", 3.0, 200.0, 4.0),
        MoldThermalSpec("mold-thick", 12.0, 800.0, 4.0),
    )
    for spec in cases:
        controlled = run_cooling_shot(_unit(), spec, dt=1.0)
        baseline = run_cooling_shot(
            _unit(), spec, dt=1.0, fixed_opening_pct=100.0
        )
        assert controlled["shot"]["verdict"] == "cooled"
        assert baseline["shot"]["verdict"] == "cooled"
        assert controlled["shot"]["water_m3"] < baseline["shot"]["water_m3"]


def test_open_sequence_reports_per_shot_water_usage(platform):
    platform.run_cure("mold-C", cavity_count=2, thickness_mm=12.0)
    report = platform.run_open(cool_dt=2.0)
    assert report["violations"] == []
    shot = report["cooling_shot"]
    assert shot["verdict"] == "cooled"
    assert shot["thickness_mm"] == 12.0
    assert shot["water_m3"] > 0
    assert shot["heat_kj"] > 0
    assert shot["duration_s"] > 0


def test_idle_allows_drain_after_auto_stop(platform):
    unit = platform.press("press-01").cooling
    unit.start_cooling("mold-X")
    unit.transition_state(CoolPhase.IDLE)
    # 已自动停泵（idle）后允许直接进入排空
    assert unit.transition_state(CoolPhase.DRAINING) is CoolPhase.DRAINING
