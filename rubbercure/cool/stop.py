"""冷却停运。

停冷却前必须先确认主蒸汽阀已关闭、母管已泄压：带着蒸汽停冷却会让模具
局部骤冷、产生内应力甚至开裂。这条约束把蒸汽回路与冷却回路绑在一起，
是停运顺序里最容易做错的一步。联锁通过后由冷却单元统一下发停泵并封账
本模用水，蒸汽回路侧无法绕过这里直接关泵。
"""

from __future__ import annotations

from ..errors import InterlockError
from ..eventbus import EventBus
from ..steam.steam import SteamUnit
from .cool import CoolingUnit


def stop_cooling(unit: CoolingUnit, steam: SteamUnit, bus: EventBus) -> dict:
    """确认蒸汽已停后停冷却循环，并结算本模用水量。"""

    if steam.valve.is_open():
        raise InterlockError("主蒸汽阀尚未关闭，禁止停冷却循环", component="cool")
    if steam.header.pressure_kpa > steam.limits.safe_residual_kpa:
        raise InterlockError(
            f"蒸汽母管残余 {steam.header.pressure_kpa:.3f} kPa 未泄净，禁止停冷却",
            component="cool",
        )
    return unit.stop_pump()
