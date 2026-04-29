import pytest
from math import isclose
from engine.lp_math import compute_optimal_split


def test_split_balanced_in_range():
    """At p=3000 in range [2500, 3500] with V=$300, ratio is roughly 46%/54%."""
    weth, usdc = compute_optimal_split(p=3000.0, p_a=2500.0, p_b=3500.0, total_value_usdc=300.0)
    weth_value = weth * 3000.0
    assert isclose(weth_value + usdc, 300.0, rel_tol=1e-6)
    assert 0.42 < weth_value / 300.0 < 0.50


def test_split_above_range():
    """When p >= p_b, only USDC is needed (range fully in USDC territory)."""
    weth, usdc = compute_optimal_split(p=3600.0, p_a=2500.0, p_b=3500.0, total_value_usdc=300.0)
    assert weth == 0.0
    assert isclose(usdc, 300.0, rel_tol=1e-9)


def test_split_below_range():
    """When p <= p_a, only WETH is needed."""
    weth, usdc = compute_optimal_split(p=2000.0, p_a=2500.0, p_b=3500.0, total_value_usdc=300.0)
    assert usdc == 0.0
    assert isclose(weth, 300.0 / 2000.0, rel_tol=1e-9)


def test_split_at_lower_boundary():
    """At p == p_a, only WETH (no USDC needed)."""
    weth, usdc = compute_optimal_split(p=2500.0, p_a=2500.0, p_b=3500.0, total_value_usdc=300.0)
    assert usdc == 0.0
    assert weth > 0


def test_split_at_upper_boundary():
    """At p == p_b, only USDC (no WETH needed)."""
    weth, usdc = compute_optimal_split(p=3500.0, p_a=2500.0, p_b=3500.0, total_value_usdc=300.0)
    assert weth == 0.0
    assert isclose(usdc, 300.0, rel_tol=1e-9)


def test_split_narrow_range_near_lower():
    """Range tight near current price favors WETH side."""
    weth, usdc = compute_optimal_split(p=3000.0, p_a=2950.0, p_b=3500.0, total_value_usdc=300.0)
    weth_value = weth * 3000.0
    assert weth_value / 300.0 > 0.55


def test_split_narrow_range_near_upper():
    """Range tight near upper end favors USDC side."""
    weth, usdc = compute_optimal_split(p=3000.0, p_a=2500.0, p_b=3050.0, total_value_usdc=300.0)
    weth_value = weth * 3000.0
    assert usdc / 300.0 > 0.55


def test_split_total_value_zero():
    weth, usdc = compute_optimal_split(p=3000.0, p_a=2500.0, p_b=3500.0, total_value_usdc=0.0)
    assert weth == 0.0
    assert usdc == 0.0


def test_split_invalid_range_raises():
    """p_a >= p_b is invalid."""
    with pytest.raises(ValueError):
        compute_optimal_split(p=3000.0, p_a=3500.0, p_b=2500.0, total_value_usdc=300.0)


def test_split_value_conservation_various():
    """For any in-range case, weth_value + usdc == total_value_usdc."""
    for p, p_a, p_b in [(3000, 2500, 3500), (2700, 2400, 3000), (3200, 3100, 3400)]:
        weth, usdc = compute_optimal_split(p=p, p_a=p_a, p_b=p_b, total_value_usdc=500.0)
        assert isclose(weth * p + usdc, 500.0, rel_tol=1e-9), f"p={p} range=[{p_a},{p_b}]"
