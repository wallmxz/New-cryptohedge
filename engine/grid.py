from __future__ import annotations
from dataclasses import dataclass
from engine.curve import GridLevel


@dataclass
class GridDiff:
    to_place: list[GridLevel]
    to_cancel: list[str]  # cloids


def _level_key(level: GridLevel) -> tuple:
    """Identity for matching grid levels (price + side + size, rounded)."""
    return (round(level.price, 6), level.side, round(level.size, 9))


class GridManager:
    """Computes the diff between current open orders and target grid."""

    def diff(
        self,
        current: list[tuple[str, GridLevel]],
        target: list[GridLevel],
    ) -> GridDiff:
        """Returns (place, cancel) lists.

        current: list of (cloid, level) for currently-open orders.
        target: list of desired grid levels.
        """
        target_keys = {_level_key(lv) for lv in target}
        current_keys = {_level_key(lv): cloid for cloid, lv in current}

        to_place = [lv for lv in target if _level_key(lv) not in current_keys]
        to_cancel = [
            cloid for key, cloid in current_keys.items() if key not in target_keys
        ]
        return GridDiff(to_place=to_place, to_cancel=to_cancel)
