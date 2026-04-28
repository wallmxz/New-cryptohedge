from math import isclose
from engine.curve import (
    compute_x,
    compute_y,
    compute_v,
    compute_l_from_value,
    inverse_x_to_p,
    compute_target_grid,
    GridLevel,
)


def test_x_at_lower_bound_max():
    """At p = p_a, x is at maximum."""
    L = 56.0
    p_a, p_b = 2700, 3300
    x_at_a = compute_x(L, p_a, p_b)
    assert x_at_a > 0
    # x should equal L * (1/sqrt(p_a) - 1/sqrt(p_b))
    expected = 56.0 * (1/2700**0.5 - 1/3300**0.5)
    assert isclose(x_at_a, expected, rel_tol=1e-6)


def test_x_at_upper_bound_zero():
    """At p = p_b, x is zero."""
    assert isclose(compute_x(56.0, 3300, 3300), 0.0, abs_tol=1e-9)


def test_y_at_lower_bound_zero():
    """At p = p_a, y is zero."""
    assert isclose(compute_y(56.0, 2700, 2700), 0.0, abs_tol=1e-9)


def test_y_at_upper_bound_max():
    """At p = p_b, y is at maximum."""
    L = 56.0
    expected = 56.0 * (3300**0.5 - 2700**0.5)
    assert isclose(compute_y(L, 3300, 2700), expected, rel_tol=1e-6)


def test_v_returns_300_at_center():
    """For L=56 and range [2700, 3300], V at p=3000 should equal ~300."""
    assert isclose(compute_v(56.0, 2700, 3300, 3000), 300.16, rel_tol=1e-3)


def test_l_from_value_inverse_of_v():
    """L computed from V should reproduce V."""
    L = compute_l_from_value(300.0, 2700, 3300, 3000)
    v_back = compute_v(L, 2700, 3300, 3000)
    assert isclose(v_back, 300.0, rel_tol=1e-6)


def test_x_above_upper_bound_zero():
    """For p > p_b, x should be zero (position fully in token1)."""
    assert compute_x(56.0, 3500, 3300) == 0.0


def test_y_below_lower_bound_zero():
    """For p < p_a, y should be zero (position fully in token0)."""
    assert compute_y(56.0, 2500, 2700) == 0.0


def test_inverse_x_round_trip():
    """inverse_x_to_p should be the inverse of compute_x."""
    L, p_b = 56.0, 3300
    p_original = 2950.0
    x = compute_x(L, p_original, p_b)
    p_recovered = inverse_x_to_p(L, x, p_b)
    assert isclose(p_recovered, p_original, rel_tol=1e-6)


def test_inverse_x_at_zero_returns_p_b():
    """When x = 0, p should equal p_b."""
    assert isclose(inverse_x_to_p(56.0, 0.0, 3300), 3300, rel_tol=1e-6)


def test_target_grid_density():
    """Grid should have ~ (x_at_p_a - 0) / step_size levels."""
    L, p_a, p_b, p_now = 56.0, 2700, 3300, 3000
    levels = compute_target_grid(
        L=L, p_a=p_a, p_b=p_b, p_now=p_now,
        hedge_ratio=1.0, min_notional_usd=3.0, max_orders=200,
    )
    # Expected ~100 levels covering full range x: 0 to 0.103
    assert 100 <= len(levels) <= 102


def test_target_grid_bounded_by_max_orders():
    """When max_orders is small, grid should be sparser (larger step)."""
    levels = compute_target_grid(
        L=56.0, p_a=2700, p_b=3300, p_now=3000,
        hedge_ratio=1.0, min_notional_usd=3.0, max_orders=20,
    )
    assert len(levels) <= 20


def test_target_grid_sides():
    """Levels above p_now are buys (close short), below are sells (open short)."""
    levels = compute_target_grid(
        L=56.0, p_a=2700, p_b=3300, p_now=3000,
        hedge_ratio=1.0, min_notional_usd=3.0, max_orders=200,
    )
    for level in levels:
        if level.price > 3000:
            assert level.side == "buy", f"price {level.price} should be buy"
        elif level.price < 3000:
            assert level.side == "sell", f"price {level.price} should be sell"


def test_grid_target_short_monotonic():
    """Cumulative target_short is monotonic in price within each side."""
    levels = compute_target_grid(
        L=56.0, p_a=2700, p_b=3300, p_now=3000,
        hedge_ratio=1.0, min_notional_usd=3.0, max_orders=200,
    )
    buys = sorted([lv for lv in levels if lv.side == "buy"], key=lambda lv: lv.price)
    sells = sorted([lv for lv in levels if lv.side == "sell"], key=lambda lv: lv.price)
    # Buys: as price increases, target_short decreases
    for i in range(len(buys) - 1):
        assert buys[i].target_short > buys[i+1].target_short
    # Sells: as price decreases, target_short increases (so reverse-sorted by price, target_short increases)
    for i in range(len(sells) - 1):
        assert sells[i].target_short > sells[i+1].target_short


def test_grid_with_hedge_ratio_half():
    levels_full = compute_target_grid(
        L=56.0, p_a=2700, p_b=3300, p_now=3000,
        hedge_ratio=1.0, min_notional_usd=3.0, max_orders=200,
    )
    levels_half = compute_target_grid(
        L=56.0, p_a=2700, p_b=3300, p_now=3000,
        hedge_ratio=0.5, min_notional_usd=3.0, max_orders=200,
    )
    assert len(levels_full) == len(levels_half)
    for full, half in zip(levels_full, levels_half):
        assert isclose(half.size, full.size * 0.5, rel_tol=1e-9)
        assert isclose(half.target_short, full.target_short * 0.5, rel_tol=1e-9)


def test_grid_levels_sorted_by_price():
    levels = compute_target_grid(
        L=56.0, p_a=2700, p_b=3300, p_now=3000,
        hedge_ratio=1.0, min_notional_usd=3.0, max_orders=200,
    )
    prices = [lv.price for lv in levels]
    assert prices == sorted(prices)


def test_inverse_x_negative_raises():
    """Negative x produces non-positive 1/sqrt(p), should raise."""
    import pytest
    with pytest.raises(ValueError, match="non-positive"):
        # large negative x makes inv_sqrt_p negative
        inverse_x_to_p(56.0, -10.0, 3300)
