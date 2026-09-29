"""每模冷却判定与摘要。

"什么时候该停下来"不靠人看：冷却水带走的功率与进出水温差成正比
（``P ∝ 流量 × 温差``），即使阀门已经关小，只要温差持续低于阈值并保持
确认时长，说明模具与水温接近平衡、再冷也没有多少热量可带走，判定冷透，
自动关阀停泵。温差偶发波动（换水温漂、传感器抖动）不立即触发，必须连续
保持，避免没冷透就误停。超过最长冷却时长仍不满足，则超时告警交人工。
"""

from __future__ import annotations

from dataclasses import dataclass

from ..clock import Clock
from ..config import CoolLimits


@dataclass
class CoolShotTracker:
    """记录一模冷却的开始时刻、低温差保持时长与判定结果。"""

    clock: Clock
    limits: CoolLimits

    def __post_init__(self) -> None:
        self._start_mono: float | None = None
        self._below_for_s = 0.0
        self._done = False
        self._timed_out = False

    def begin(self) -> None:
        """模次开始：清零保持计时。"""

        self._start_mono = self.clock.monotonic()
        self._below_for_s = 0.0
        self._done = False
        self._timed_out = False

    def is_active(self) -> bool:
        return self._start_mono is not None and not self._done and not self._timed_out

    def observe(self, delta_c: float, dt: float) -> dict:
        """推进一个周期的冷透判定，返回本周期判定状态。"""

        if self._start_mono is None:
            raise ValueError("冷却模次尚未开始")
        if dt <= 0:
            raise ValueError("判定周期必须为正")
        if not self.is_active():
            return {"done": self._done, "timed_out": self._timed_out, "below_for_s": self._below_for_s}
        if delta_c <= self.limits.stop_delta_c:
            self._below_for_s += dt
        else:
            self._below_for_s = 0.0
        elapsed_s = self.clock.monotonic() - self._start_mono
        if self._below_for_s >= self.limits.stop_confirm_s:
            self._done = True
        elif elapsed_s >= self.limits.max_cool_s:
            self._timed_out = True
        return {
            "done": self._done,
            "timed_out": self._timed_out,
            "elapsed_s": round(elapsed_s, 3),
            "below_for_s": round(self._below_for_s, 3),
        }

    @property
    def elapsed_s(self) -> float:
        if self._start_mono is None:
            return 0.0
        return self.clock.monotonic() - self._start_mono

    @property
    def done(self) -> bool:
        return self._done

    @property
    def timed_out(self) -> bool:
        return self._timed_out

    def summary(self, water_m3: float, heat_kj: float, thickness_mm: float | None) -> dict:
        """组装一模冷却报表。"""

        return {
            "thickness_mm": round(thickness_mm, 3) if thickness_mm is not None else None,
            "duration_s": round(self.elapsed_s, 3),
            "water_m3": round(water_m3, 4),
            "heat_kj": round(heat_kj, 3),
            "verdict": "cooled" if self._done else ("timeout" if self._timed_out else "running"),
        }
