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
