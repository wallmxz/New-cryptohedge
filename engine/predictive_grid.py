"""Predictive curve-grid hedge — pure-functional grid module.

The engine pre-computes a `LevelGrid` from the Beefy CLM v2 strategy's
current tick range. Each level k corresponds to a pool ratio p_levels[k]
and the V3 amounts (amount0_at[k], amount1_at[k]) the LP would hold at
that ratio. As the Uniswap pool's currentTick moves, the engine maps p_now
to a level idx via bisect and fires hedge orders for the per-leg amount
delta between the previous idx and the new one.

Spec: docs/superpowers/specs/2026-05-08-predictive-curve-grid-hedge-design.md
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass


@dataclass
class LevelGrid:
    """Pre-computed level grid keyed on raw pool ratio p = token1/token0.

    Levels span [p_a, p_b] (the Beefy strategy's tick range). Outside the
    range, find_level_idx returns the nearest edge (0 or len-1), and the
    edge level's amounts are the OOR-clamped V3 values.
    """
    p_a: float
    p_b: float
    L: float
    p_levels: list[float]
    amount0_at: list[float]
    amount1_at: list[float]
    tick_lower: int
    tick_upper: int


def find_level_idx(grid: LevelGrid, p_now: float) -> int:
    """Returns idx k such that p_levels[k] ≤ p_now < p_levels[k+1].

    OOR clamping:
    - p_now ≤ p_a → 0 (edge level, full token0)
    - p_now ≥ p_b → len(p_levels) - 1 (edge level, full token1)

    O(log N) via bisect.
    """
    if p_now <= grid.p_levels[0]:
        return 0
    if p_now >= grid.p_levels[-1]:
        return len(grid.p_levels) - 1
    return bisect.bisect_right(grid.p_levels, p_now) - 1


def compute_deltas(
    grid: LevelGrid, old_idx: int, new_idx: int,
) -> tuple[float, float]:
    """Returns (delta_amount0, delta_amount1) for transition old_idx → new_idx.

    Positive delta = LP gained that token = need to short MORE on perp.
    Negative delta = LP lost that token = close some short (BUY on perp).

    Multi-level jumps use direct endpoint diff, NOT sum of intermediates.
    """
    return (
        grid.amount0_at[new_idx] - grid.amount0_at[old_idx],
        grid.amount1_at[new_idx] - grid.amount1_at[old_idx],
    )


# ---------------------------------------------------------------------------
# Grid builder
# ---------------------------------------------------------------------------

# Default level granularity: each adjacent level pair must move ≥$0.50
# in at least one leg's notional. Adaptive: denser where one leg moves
# slowly per Δp, sparser where it moves fast.
MIN_LEG_NOTIONAL_USD = 0.50

# Hard cap on level count — protect against pathological tick ranges
# that would generate thousands of levels (RPC + memory waste). 500
# is plenty for any reasonable Beefy CLM v2 range.
MAX_LEVELS = 500


def _amount0_at(L: float, p: float, p_b: float) -> float:
    """V3 token0 amount. Clamped 0 above p_b (single-asset edge)."""
    if p >= p_b:
        return 0.0
    return L * (1.0 / math.sqrt(p) - 1.0 / math.sqrt(p_b))


def _amount1_at(L: float, p: float, p_a: float) -> float:
    """V3 token1 amount. Clamped 0 below p_a."""
    if p <= p_a:
        return 0.0
    return L * (math.sqrt(p) - math.sqrt(p_a))


def build_grid(
    *,
    tick_lower: int,
    tick_upper: int,
    L: float,
    p0_usd: float,
    p1_usd: float,
    min_leg_notional_usd: float = MIN_LEG_NOTIONAL_USD,
) -> LevelGrid:
    """Build a fresh LevelGrid for the given Beefy tick range and current
    USD prices. Discretizes [p_a, p_b] adaptively: each adjacent level
    pair must produce ≥`min_leg_notional_usd` in at least one leg.

    Algorithm:
      1. p_a, p_b from ticks via 1.0001^tick.
      2. Walk forward from p_a in fine sub-steps; at each candidate p,
         check |Δamount0|·p0_usd OR |Δamount1|·p1_usd ≥ floor; if yes,
         emit the level and reset accumulator.
      3. Always include p_b as the last level.

    Capped at MAX_LEVELS to protect against pathological ranges.
    """
    p_a = math.pow(1.0001, tick_lower)
    p_b = math.pow(1.0001, tick_upper)

    if p_a >= p_b:
        raise ValueError(
            f"Invalid tick range: tick_lower={tick_lower} >= tick_upper={tick_upper}"
        )

    p_levels = [p_a]
    amount0_at = [_amount0_at(L, p_a, p_b)]
    amount1_at = [_amount1_at(L, p_a, p_a)]  # = 0 at p_a

    sub_step = (p_b - p_a) / 10_000
    p = p_a + sub_step
    last_a0 = amount0_at[0]
    last_a1 = amount1_at[0]

    while p < p_b and len(p_levels) < MAX_LEVELS - 1:
        a0 = _amount0_at(L, p, p_b)
        a1 = _amount1_at(L, p, p_a)
        d0_notional = abs(a0 - last_a0) * p0_usd
        d1_notional = abs(a1 - last_a1) * p1_usd
        if max(d0_notional, d1_notional) >= min_leg_notional_usd:
            p_levels.append(p)
            amount0_at.append(a0)
            amount1_at.append(a1)
            last_a0 = a0
            last_a1 = a1
        p += sub_step

    p_levels.append(p_b)
    amount0_at.append(_amount0_at(L, p_b, p_b))  # = 0
    amount1_at.append(_amount1_at(L, p_b, p_a))

    return LevelGrid(
        p_a=p_a, p_b=p_b, L=L,
        p_levels=p_levels,
        amount0_at=amount0_at,
        amount1_at=amount1_at,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
    )
