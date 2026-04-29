"""Pure V3 math for computing optimal token split given a CLM range."""
from __future__ import annotations
from math import sqrt


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

    # Out-of-range cases
    if p >= p_b:
        return 0.0, total_value_usdc
    if p <= p_a:
        return total_value_usdc / p, 0.0

    # In-range: use V3 amount formulas.
    # amount_weth_per_L = (1/sqrt(p) - 1/sqrt(p_b))
    # amount_usdc_per_L = (sqrt(p) - sqrt(p_a))
    # Value of position = amount_weth*p + amount_usdc = L * (sqrt(p) - p/sqrt(p_b) + sqrt(p) - sqrt(p_a))
    #                  = L * (2*sqrt(p) - sqrt(p_a) - p/sqrt(p_b))
    # Solve for L given total value V: L = V / (2*sqrt(p) - sqrt(p_a) - p/sqrt(p_b))
    sqrt_p = sqrt(p)
    sqrt_pa = sqrt(p_a)
    sqrt_pb = sqrt(p_b)

    denom = 2 * sqrt_p - sqrt_pa - p / sqrt_pb
    L = total_value_usdc / denom

    amount_weth = L * (1.0 / sqrt_p - 1.0 / sqrt_pb)
    amount_usdc = L * (sqrt_p - sqrt_pa)

    return amount_weth, amount_usdc
