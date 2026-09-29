"""每模冷却用水计量。

一台模具的冷却用了多少水要按模统计，而不是看总管上的瞬时流量。计量表在冷却
启动时按模次开盖，控制周期里用「流量 × 时长」积分累计（流量单位 m³/h），
停泵时封账，产出一条带模具号、起止时间、累计水量与峰值流量的用水账单，供
审计与排产复盘。封账后表只读：下一摸重新 ``begin``，不能向已封账的模次补数。
"""

from __future__ import annotations

import threading

from ..eventbus import Event, EventBus

_SECONDS_PER_HOUR = 3600.0


class WaterMeter:
    """按模次累计冷却水量。"""

    def __init__(self, bus: EventBus) -> None:
        self._bus = bus
        self._lock = threading.RLock()
        self._mold_id: str | None = None
        self._started_at: float = 0.0
        self._ended_at: float = 0.0
        self._volume_m3: float = 0.0
        self._peak_flow_m3_h: float = 0.0
        self._samples: int = 0
        self._last_report: dict | None = None

    @property
    def mold_id(self) -> str | None:
        with self._lock:
            return self._mold_id

    def is_open(self) -> bool:
        with self._lock:
            return self._mold_id is not None

    def begin(self, mold_id: str, started_at: float) -> None:
        """为一个模次开盖。"""

        with self._lock:
            self._mold_id = mold_id
            self._started_at = float(started_at)
            self._ended_at = 0.0
            self._volume_m3 = 0.0
            self._peak_flow_m3_h = 0.0
            self._samples = 0
        self._bus.publish(
            Event.of(
                "cool",
                "cool",
                "coolant_meter_opened",
                subject=mold_id,
                mold_id=mold_id,
                started_at=round(float(started_at), 3),
            )
        )

    def accumulate(self, flow_m3_h: float, dt_s: float) -> float:
        """按本周期流量与时长积分，返回累计水量（m³）。"""

        if dt_s < 0:
            raise ValueError("计量时长不能为负")
        with self._lock:
            if self._mold_id is None:
                raise RuntimeError("用水表未开盖，不能累计水量")
            self._volume_m3 += float(flow_m3_h) * float(dt_s) / _SECONDS_PER_HOUR
            self._peak_flow_m3_h = max(self._peak_flow_m3_h, float(flow_m3_h))
            self._samples += 1
            return self._volume_locked()

    def close(self, ended_at: float) -> dict:
        """封账并发布本模用水账单。"""

        with self._lock:
            mold_id = self._mold_id
            if mold_id is None:
                raise RuntimeError("用水表未开盖，不能封账")
            self._ended_at = float(ended_at)
            report = {
                "mold_id": mold_id,
                "started_at": round(self._started_at, 3),
                "ended_at": round(self._ended_at, 3),
                "duration_s": round(max(0.0, self._ended_at - self._started_at), 3),
                "volume_m3": round(self._volume_m3, 4),
                "peak_flow_m3_h": round(self._peak_flow_m3_h, 3),
                "samples": self._samples,
            }
            self._mold_id = None
            self._last_report = report
        self._bus.publish(
            Event.of("cool", "cool", "coolant_meter_closed", subject=report["mold_id"], **report)
        )
        return dict(report)

    def last_report(self) -> dict | None:
        with self._lock:
            return dict(self._last_report) if self._last_report else None

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "open": self._mold_id is not None,
                "mold_id": self._mold_id,
                "started_at": round(self._started_at, 3) if self._mold_id else None,
                "volume_m3": round(self._volume_locked(), 4),
                "peak_flow_m3_h": round(self._peak_flow_m3_h, 3),
                "samples": self._samples,
                "last_report": dict(self._last_report) if self._last_report else None,
            }

    def _volume_locked(self) -> float:
        return self._volume_m3
