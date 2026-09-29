"""进出水温差闭环控制器。

被控量是冷却水进出口温差，输出是调节阀开度（0~100%）。模具厚、热负荷大时
温差变大，控制器开大阀门增加流量；模具薄或接近冷透时温差变小，阀门关小，
避免"恒流常开"把水白白排走。

控制方向与模温回路相反（温差越大越要加流量），因此误差取
``实测温差 - 目标温差``。控制器带积分限幅，输出饱和时回退积分，防止冷却
初期在满开度上累积一大段积分、后期冷透了又迟迟关不小。
"""

from __future__ import annotations

from dataclasses import dataclass

OUTPUT_MIN_PCT = 0.0
OUTPUT_MAX_PCT = 100.0


@dataclass
class DeltaFlowController:
    """带积分限幅的温差 → 阀位 PI 控制器。"""

    kp: float
    ki: float
    output_min: float = OUTPUT_MIN_PCT
    output_max: float = OUTPUT_MAX_PCT
    _integral: float = 0.0

    def update(self, measured_delta_c: float, target_delta_c: float, dt: float) -> float:
        """按温差推进一个控制周期，返回阀位开度百分比。"""

        if dt <= 0:
            raise ValueError("控制周期必须为正")
        error = float(measured_delta_c) - float(target_delta_c)
        self._integral += error * dt
        raw = self.kp * error + self.ki * self._integral
        clamped = max(self.output_min, min(self.output_max, raw))
        if clamped != raw:
            # 输出已经饱和，回退本周期积分，避免积分项继续堆积。
            self._integral -= error * dt
        return clamped

    def reset(self) -> None:
        self._integral = 0.0

    @property
    def integral(self) -> float:
        return self._integral

    def snapshot(self) -> dict:
        return {
            "kp": self.kp,
            "ki": self.ki,
            "integral": round(self._integral, 4),
            "output_min": self.output_min,
            "output_max": self.output_max,
        }
