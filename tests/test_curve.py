from math import isclose
from engine.curve import compute_x, compute_y, compute_v, compute_l_from_value


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
