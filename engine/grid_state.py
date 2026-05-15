"""GridStop dataclass + lookup helpers for the event-driven grid reconciler.

A GridStop represents one stop order the bot has posted on Lighter and is
tracking locally in `_local_grid`. The lookup helpers (lowest_buy, etc.)
support the algorithm in `_apply_fills_to_grid`.
"""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class GridStop:
    cloid: int
    side: Literal["sell", "buy"]
    trigger_price: float
    size: float


def lowest_buy(grid: dict[int, GridStop]) -> GridStop | None:
    """Return the buy GridStop with the LOWEST trigger_price (farthest from market below).

    None if no buys in grid.
    """
    buys = [s for s in grid.values() if s.side == "buy"]
    return min(buys, key=lambda s: s.trigger_price) if buys else None


def highest_sell(grid: dict[int, GridStop]) -> GridStop | None:
    """Return the sell GridStop with the HIGHEST trigger_price (farthest from market above)."""
    sells = [s for s in grid.values() if s.side == "sell"]
    return max(sells, key=lambda s: s.trigger_price) if sells else None


# Aliases for clarity in the event-driven algorithm.
top_sell = highest_sell
bottom_buy = lowest_buy
