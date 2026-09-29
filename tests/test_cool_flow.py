"""冷却水：温差调流量、按模计水、到位判定与停运封账。"""

from __future__ import annotations

import pytest

from rubbercure.config import CoolLimits
from rubbercure.cool.flow import DeltaTFlowController
from rubbercure.cool.meter import WaterMeter
from rubbercure.cool.ready import CoolReadyHold
from rubbercure.errors import InterlockError


def _limits(**overrides) -> CoolLimits:
    values = dict(
        rated_flow_m3_h=18.0,
        min_flow_m3_h=6.0,
        alarm_inlet_c=32.0,
        alarm_outlet_c=48.0,
        drain_hold_s=30.0,
        target_delta_c=5.0,
        demold_temp_c=80.0,
        ready_hold_s=20.0,
    )
    values.update(overrides)
    return CoolLimits(**values)


def test_flow_controller_opens_for_large_delta_and_clamps():
    controller = DeltaTFlowController(_limits())
    assert controller.compute_flow(10.0) == 18.0  # 温差翻倍以上，顶到额定
    assert controller.compute_flow(0.0) == 6.0  # 没有温差，压到最小流量
    assert controller.compute_flow(2.5) == 12.0  # 一半温差，取区间中点


def test_flow_controller_deadband_keeps_valve_still():
    controller = DeltaTFlowController(_limits())
    first = controller.update(0.0)
    assert first["adjusted"] is True and first["flow_m3_h"] == 6.0
    second = controller.update(0.01)  # 变化落在死区内
    assert second["adjusted"] is False and second["flow_m3_h"] == 6.0
    third = controller.update(10.0)
    assert third["adjusted"] is True and third["flow_m3_h"] == 18.0


def test_ready_hold_requires_both_temperature_and_delta():
    hold = CoolReadyHold(_limits())
    assert not hold.conditions_met(85.0, 2.0)  # 模具还没凉
    assert not hold.conditions_met(60.0, 8.0)  # 温差还没收敛
    assert hold.conditions_met(80.0, 5.0)

    hold.update(70.0, 4.0, 10.0)
    assert not hold.is_ready()
    hold.update(70.0, 4.0, 10.0)
    assert hold.is_ready()

    broken = hold.update(95.0, 20.0, 1.0)
    assert broken["held_s"] == 0.0 and not broken["ready"]


def test_water_meter_integrates_flow_per_mold_and_closes(platform):
    meter = WaterMeter(platform.bus)
    meter.begin("mold-W", platform.clock.now())
    meter.accumulate(18.0, 1800.0)  # 半小时：9.0 m³
    meter.accumulate(6.0, 600.0)  # 再十分钟：1.0 m³
    platform.clock.advance(2400.0)
    started_at = platform.clock.now() - 2400.0
    report = meter.close(platform.clock.now())
    assert report["mold_id"] == "mold-W"
    assert report["started_at"] == started_at
    assert report["duration_s"] == 2400.0
    assert report["volume_m3"] == pytest.approx(10.0)
    assert report["peak_flow_m3_h"] == 18.0
    assert report["samples"] == 2
    assert meter.last_report()["volume_m3"] == 10.0
    assert not meter.is_open()


def test_water_meter_rejects_accumulate_or_close_without_begin(platform):
    meter = WaterMeter(platform.bus)
    with pytest.raises(RuntimeError):
        meter.accumulate(10.0, 1.0)
    with pytest.raises(RuntimeError):
        meter.close(0.0)


def test_cooling_loop_regulates_meters_and_reaches_ready(platform):
    unit = platform.press("press-01")
    unit.cooling.start_cooling("mold-C")
    hot = unit.cooling.regulate(26.0, 90.0, 5.0)
    assert hot["regulation"]["flow_m3_h"] == 18.0
    assert unit.cooling.circulation.flow_m3_h == 18.0
    assert hot["volume_m3"] == pytest.approx(0.025)

    for _ in range(4):
        result = unit.cooling.regulate(26.0, 26.0, 5.0)
    assert result["ready"]["ready"] is True
    assert result["regulation"]["flow_m3_h"] == 6.0
    assert unit.cooling.is_ready()
    assert result["volume_m3"] > 0.0


def test_regulate_rejected_when_pump_stopped(platform):
    unit = platform.press("press-01")
    with pytest.raises(RuntimeError):
        unit.cooling.regulate(26.0, 40.0, 1.0)


def test_stop_cooling_closes_meter_with_water_bill(platform):
    unit = platform.press("press-01")
    unit.steam.set_valve(0.0)
    assert unit.steam.header.pressure_kpa <= platform.config.steam.safe_residual_kpa
    unit.cooling.start_cooling("mold-S")
    unit.cooling.regulate(26.0, 90.0, 120.0)  # 大温差 -> 额定 18 m³/h
    result = platform.run_stop("press-01")
    assert result["violations"] == []
    bill = unit.cooling.snapshot()["meter"]["last_report"]
    assert bill["mold_id"] == "mold-S"
    assert bill["volume_m3"] == pytest.approx(0.6)  # 18 m³/h × 120 s
    assert bill["peak_flow_m3_h"] == 18.0
    assert unit.cooling.phase.value == "stopped"


def test_stop_cooling_still_requires_steam_off(platform):
    unit = platform.press("press-01")
    unit.cooling.start_cooling("mold-X")
    unit.steam.open_steam(platform.config.steam.target_pressure_kpa)
    with pytest.raises(InterlockError):
        platform.run_stop("press-01")


def test_apply_flow_rejected_when_pump_off(platform):
    with pytest.raises(RuntimeError):
        platform.press("press-01").cooling.circulation.apply_flow(10.0)
