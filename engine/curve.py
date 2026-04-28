"""Uniswap V3 concentrated liquidity math.

For position with liquidity L in range [p_a, p_b]:
    x(p) = L * (1/sqrt(p) - 1/sqrt(p_b))     # token0 amount
    y(p) = L * (sqrt(p) - sqrt(p_a))         # token1 amount
    V(p) = x(p) * p + y(p)                   # total value in token1 units

These hold for p in [p_a, p_b]. Outside the range, position is 100% one token.
"""
from __future__ import annotations
from math import sqrt


def compute_x(L: float, p: float, p_b: float) -> float:
    """Token0 amount in V3 LP at price p with upper bound p_b.

    For p >= p_b, returns 0 (position fully in token1).
    """
    if p >= p_b:
        return 0.0
    return L * (1.0 / sqrt(p) - 1.0 / sqrt(p_b))


def compute_y(L: float, p: float, p_a: float) -> float:
    """Token1 amount in V3 LP at price p with lower bound p_a.

    For p <= p_a, returns 0 (position fully in token0).
    """
    if p <= p_a:
        return 0.0
    return L * (sqrt(p) - sqrt(p_a))


def compute_v(L: float, p_a: float, p_b: float, p: float) -> float:
    """Total LP value at price p (in token1 units, e.g., USDC)."""
    return compute_x(L, p, p_b) * p + compute_y(L, p, p_a)


def compute_l_from_value(value: float, p_a: float, p_b: float, p: float) -> float:
    """Solve for L given a target value V at price p in range [p_a, p_b].

    V = L * (2*sqrt(p) - sqrt(p_a) - p/sqrt(p_b))
    """
    denom = 2.0 * sqrt(p) - sqrt(p_a) - p / sqrt(p_b)
    if denom <= 0:
        raise ValueError(f"Invalid range or price: p={p}, p_a={p_a}, p_b={p_b}")
    return value / denom
