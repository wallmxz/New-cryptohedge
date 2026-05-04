"""Pure V3 math for computing optimal token split given a CLM range."""
from __future__ import annotations

from engine.curve import compute_l_from_value, compute_x, compute_y


def compute_optimal_split(
    *, p: float, p_a: float, p_b: float, total_value_usdc: float,
) -> tuple[float, float]:
    """Given current price p (USDC/WETH), range [p_a, p_b], and total budget V (USDC),
    returns (amount_weth, amount_usdc) such that:
      - amount_weth * p + amount_usdc == V (value conservation)
      - The ratio matches the V3 concentrated liquidity ratio at p in [p_a, p_b]

    Edge cases:
      p >= p_b: returns (0, V) - only USDC needed
      p <= p_a: returns (V/p, 0) - only WETH needed (V converted to WETH)

    Raises ValueError if p_a >= p_b.
    """
    if p_a >= p_b:
        raise ValueError(f"Invalid range: p_a={p_a} must be < p_b={p_b}")
    if total_value_usdc <= 0:
        return 0.0, 0.0

    if p >= p_b:
        return 0.0, total_value_usdc
    if p <= p_a:
        return total_value_usdc / p, 0.0

    L = compute_l_from_value(total_value_usdc, p_a, p_b, p)
    return compute_x(L, p, p_b), compute_y(L, p, p_a)
