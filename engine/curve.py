"""Uniswap V3 concentrated liquidity math.

For position with liquidity L in range [p_a, p_b]:
    x(p) = L * (1/sqrt(p) - 1/sqrt(p_b))     # token0 amount
    y(p) = L * (sqrt(p) - sqrt(p_a))         # token1 amount
    V(p) = x(p) * p + y(p)                   # total value in token1 units

These hold for p in [p_a, p_b]. Outside the range, position is 100% one token.
"""
from __future__ import annotations
from dataclasses import dataclass
from math import sqrt
from typing import Literal


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
    """Total LP value at price p (in token1 units, e.g., USDC).

    Caller must ensure p_a <= p <= p_b. Outside the range, this function
    does NOT return the correct clamped LP value -- it returns the formula
    extrapolated. Use guards in the caller; this function will return
    extrapolated values if p is outside [p_a, p_b].
    """
    return compute_x(L, p, p_b) * p + compute_y(L, p, p_a)


def compute_l_from_value(value: float, p_a: float, p_b: float, p: float) -> float:
    """Solve for L given a target value V at price p in range [p_a, p_b].

    V = L * (2*sqrt(p) - sqrt(p_a) - p/sqrt(p_b))
    """
    denom = 2.0 * sqrt(p) - sqrt(p_a) - p / sqrt(p_b)
    if denom <= 0:
        raise ValueError(f"Invalid range or price: p={p}, p_a={p_a}, p_b={p_b}")
    return value / denom


@dataclass(frozen=True)
class GridLevel:
    price: float           # USD price of token0 at this level
    size: float            # base units of token0 (e.g., WETH amount)
    side: Literal["buy", "sell"]  # "buy" (close short) or "sell" (open short)
    target_short: float    # cumulative target short (base units). Used by engine
                           # for accounting/fills, not by GridManager.diff.


def inverse_x_to_p(L: float, x: float, p_b: float) -> float:
    """Solve x(p) = x for p, given L and p_b.

    x = L * (1/sqrt(p) - 1/sqrt(p_b))
    => 1/sqrt(p) = x/L + 1/sqrt(p_b)
    => p = 1 / (x/L + 1/sqrt(p_b))^2
    """
    if L <= 0:
        raise ValueError("L must be positive")
    inv_sqrt_p = x / L + 1.0 / sqrt(p_b)
    if inv_sqrt_p <= 0:
        raise ValueError(f"invalid x: produces non-positive 1/sqrt(p) (x={x}, L={L})")
    return 1.0 / (inv_sqrt_p * inv_sqrt_p)


def compute_target_grid(
    *,
    L: float, p_a: float, p_b: float, p_now: float,
    hedge_ratio: float, min_notional_usd: float, max_orders: int,
) -> list[GridLevel]:
    """Build a grid of orders covering [p_a, p_b] with each order ~ min_notional_usd.

    If the raw level count would exceed max_orders, recomputes step as
    total_x_range/max_orders so the result is at most ~max_orders levels
    (may be one or two short due to open-interval traversal — see I2).

    Levels above p_now are buys (close short), below are sells (add short).
    Returned levels are sorted by ascending price.
    """
    if not (p_a < p_now < p_b):
        return []  # out of range, no grid

    x_now = compute_x(L, p_now, p_b)
    x_at_a = compute_x(L, p_a, p_b)

    # Δx in base units = min_notional_usd / current price
    step_x = min_notional_usd / p_now

    # How many levels fit in the full range [p_a, p_b]?
    total_x_range = x_at_a - 0.0  # x decreases from x_at_a (at p_a) to 0 (at p_b)

    if total_x_range / step_x > max_orders:
        # Increase step to fit max_orders
        step_x = total_x_range / max_orders

    levels: list[GridLevel] = []

    # Levels ABOVE p_now (buys): x decreases from x_now toward 0
    target_x = x_now - step_x
    while target_x > 0:
        p_level = inverse_x_to_p(L, target_x, p_b)
        if p_level >= p_b:
            break
        levels.append(GridLevel(
            price=p_level,
            size=step_x * hedge_ratio,
            side="buy",
            target_short=target_x * hedge_ratio,
        ))
        target_x -= step_x

    # Levels BELOW p_now (sells): x increases from x_now toward x_at_a
    target_x = x_now + step_x
    while target_x < x_at_a:
        p_level = inverse_x_to_p(L, target_x, p_b)
        if p_level <= p_a:
            break
        levels.append(GridLevel(
            price=p_level,
            size=step_x * hedge_ratio,
            side="sell",
            target_short=target_x * hedge_ratio,
        ))
        target_x += step_x

    return sorted(levels, key=lambda lv: lv.price)
