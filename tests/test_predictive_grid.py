"""Pure-function unit tests for the predictive grid module.

Math reference: V3 LP formulas
  amount0(p) = L × (1/√p − 1/√p_b)
  amount1(p) = L × (√p − √p_a)
"""
import bisect
import math
import pytest

from engine.predictive_grid import (
    LevelGrid, find_level_idx, compute_deltas,
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
