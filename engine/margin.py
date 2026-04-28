from __future__ import annotations


def compute_required_collateral(
    *, peak_short_size: float, current_price: float,
    stress_pct: float = 0.275, mm_fraction: float = 0.03,
) -> float:
    """Collateral needed to survive `stress_pct` adverse move.

    Formula: collateral_needed = N * (s + MM * (1+s))
    where N = peak_short_size * current_price.
    """
    n = peak_short_size * current_price
    return n * (stress_pct + mm_fraction * (1 + stress_pct))


def compute_margin_ratio(*, collateral: float, required: float) -> float:
    """Returns collateral / required. Returns 999 if required is 0."""
    if required <= 0:
        return 999.0
    return collateral / required


def classify_margin(ratio: float) -> str:
    """Maps ratio to status level."""
    if ratio >= 1.0:
        return "healthy"
    if ratio >= 0.8:
        return "info"
    if ratio >= 0.6:
        return "warning"
    if ratio >= 0.4:
        return "urgent"
    if ratio >= 0.2:
        return "critical"
    return "emergency"
