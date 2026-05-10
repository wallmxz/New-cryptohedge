"""Pure-function unit tests for the predictive grid module.

Math reference: V3 LP formulas
  amount0(p) = L × (1/√p − 1/√p_b)
  amount1(p) = L × (√p − √p_a)
"""
import bisect
import math
import pytest

from engine.predictive_grid import (
    LevelGrid, find_level_idx, compute_deltas, build_grid,
)


def _grid_fixture():
    """Fixture grid: 5 levels in [p_a=1.0, p_b=4.0], L=1.0."""
    p_a, p_b, L = 1.0, 4.0, 1.0
    p_levels = [1.0, 1.5, 2.0, 3.0, 4.0]
    amount0_at = [1.0 - 0.5, 1/math.sqrt(1.5) - 0.5, 1/math.sqrt(2) - 0.5, 1/math.sqrt(3) - 0.5, 0.0]
    amount1_at = [0.0, math.sqrt(1.5) - 1, math.sqrt(2) - 1, math.sqrt(3) - 1, 1.0]
    return LevelGrid(
        p_a=p_a, p_b=p_b, L=L,
        p_levels=p_levels,
        amount0_at=amount0_at,
        amount1_at=amount1_at,
        tick_lower=0, tick_upper=13863,
    )


def test_find_level_idx_below_p_a_returns_zero():
    grid = _grid_fixture()
    assert find_level_idx(grid, p_now=0.5) == 0
    assert find_level_idx(grid, p_now=1.0) == 0


def test_find_level_idx_above_p_b_returns_last():
    grid = _grid_fixture()
    assert find_level_idx(grid, p_now=10.0) == 4
    assert find_level_idx(grid, p_now=4.0) == 4


def test_find_level_idx_in_range_uses_bisect():
    grid = _grid_fixture()
    assert find_level_idx(grid, p_now=1.7) == 1
    assert find_level_idx(grid, p_now=2.0) == 2
    assert find_level_idx(grid, p_now=2.5) == 2
    assert find_level_idx(grid, p_now=3.5) == 3


def test_compute_deltas_handles_multi_level_jump():
    grid = _grid_fixture()
    d0, d1 = compute_deltas(grid, old_idx=0, new_idx=4)
    assert d0 == pytest.approx(0.0 - 0.5)
    assert d1 == pytest.approx(1.0 - 0.0)


def test_compute_deltas_same_idx_returns_zero():
    grid = _grid_fixture()
    d0, d1 = compute_deltas(grid, old_idx=2, new_idx=2)
    assert d0 == 0.0
    assert d1 == 0.0


def test_compute_deltas_negative_direction():
    grid = _grid_fixture()
    d0, d1 = compute_deltas(grid, old_idx=3, new_idx=1)
    expected_d0 = (1/math.sqrt(1.5) - 0.5) - (1/math.sqrt(3) - 0.5)
    expected_d1 = (math.sqrt(1.5) - 1) - (math.sqrt(3) - 1)
    assert d0 == pytest.approx(expected_d0)
    assert d1 == pytest.approx(expected_d1)


def test_build_grid_endpoints_are_p_a_and_p_b():
    """First level == p_a, last level == p_b."""
    grid = build_grid(
        tick_lower=0, tick_upper=13863,
        L=1.0, p0_usd=1.0, p1_usd=1.0,
        min_leg_notional_usd=0.50,
    )
    assert grid.p_levels[0] == pytest.approx(1.0)
    assert grid.p_levels[-1] == pytest.approx(math.exp(13863 * math.log(1.0001)))


def test_build_grid_amounts_match_v3_formula_at_endpoints():
    L = 1.0
    grid = build_grid(
        tick_lower=0, tick_upper=13863,
        L=L, p0_usd=1.0, p1_usd=1.0,
        min_leg_notional_usd=0.50,
    )
    p_a, p_b = grid.p_a, grid.p_b
    expected_a0 = L * (1/math.sqrt(p_a) - 1/math.sqrt(p_b))
    assert grid.amount0_at[0] == pytest.approx(expected_a0)
    expected_b1 = L * (math.sqrt(p_b) - math.sqrt(p_a))
    assert grid.amount1_at[-1] == pytest.approx(expected_b1)
    assert grid.amount0_at[-1] == pytest.approx(0.0)
    assert grid.amount1_at[0] == pytest.approx(0.0)


def test_build_grid_levels_spaced_by_dollar_floor():
    """Each adjacent level pair must produce ≥$0.50 notional in at least one leg."""
    L = 100.0
    p0_usd, p1_usd = 2300.0, 0.13
    grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=L, p0_usd=p0_usd, p1_usd=p1_usd,
        min_leg_notional_usd=0.50,
    )
    for k in range(len(grid.p_levels) - 1):
        d0 = grid.amount0_at[k+1] - grid.amount0_at[k]
        d1 = grid.amount1_at[k+1] - grid.amount1_at[k]
        notional = max(abs(d0) * p0_usd, abs(d1) * p1_usd)
        assert notional >= 0.50, (
            f"level {k}→{k+1}: notional={notional:.4f} below floor 0.50"
        )


def test_build_grid_tick_range_stored():
    grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=1.0, p0_usd=2300.0, p1_usd=0.13,
        min_leg_notional_usd=0.50,
    )
    assert grid.tick_lower == -81121
    assert grid.tick_upper == -76012


def test_predictive_unavailable_is_exception():
    """Engine raises PredictiveUnavailable when fallback should run."""
    from engine import PredictiveUnavailable
    exc = PredictiveUnavailable("book empty")
    assert isinstance(exc, Exception)
    assert str(exc) == "book empty"


import pytest


@pytest.mark.skip(
    reason="T6 removed `predictive_status` from StateHub (renamed to "
    "`hedge_model_status`). This file is deleted in T7."
)
def test_state_hub_has_predictive_status_field():
    from state import StateHub
    hub = StateHub(hedge_ratio=0.98)
    assert hub.predictive_status == "idle"
