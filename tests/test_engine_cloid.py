"""Tests for cloid generation — must produce unique values across many calls.

Regression for the 256-cloid wraparound bug: `_cloid_seq & 0xFF` only gave
256 unique values per (run_id, leg). After exactly 256 stops were posted on
a single leg, every subsequent post collided with an existing
`grid_orders.cloid` (UNIQUE constraint), breaking the reconciler in an
infinite loop. Observed in prod 2026-05-14: 19,415 UNIQUE failures in 18h.
"""
from unittest.mock import MagicMock

from engine import GridMakerEngine


def _make_engine() -> GridMakerEngine:
    settings = MagicMock()
    settings.dydx_symbol_token0 = "ARB-USD"
    settings.dydx_symbol_token1 = "ETH-USD"
    return GridMakerEngine(
        settings=settings, hub=MagicMock(), db=MagicMock(), exchange=None,
    )


def test_next_cloid_for_leg_unique_across_1000_calls_per_leg():
    """A single leg must produce 1000+ unique cloids without collisions.

    Pre-fix: failed at 257th call (seq wrapped 0..255 → 0).
    """
    engine = _make_engine()
    seen: set[int] = set()
    for _ in range(1000):
        c = engine._next_cloid_for_leg("ARB-USD")
        assert c not in seen, f"cloid collision after {len(seen)} unique calls"
        seen.add(c)
    assert len(seen) == 1000


def test_next_cloid_for_leg_unique_across_legs():
    """Different legs must never produce overlapping cloids."""
    engine = _make_engine()
    token0_cloids = {engine._next_cloid_for_leg("ARB-USD") for _ in range(500)}
    token1_cloids = {engine._next_cloid_for_leg("ETH-USD") for _ in range(500)}
    assert len(token0_cloids) == 500
    assert len(token1_cloids) == 500
    assert token0_cloids.isdisjoint(token1_cloids), "cross-leg cloid collision"


def test_next_cloid_unique_across_1000_calls():
    """Legacy `_next_cloid` (level-based) must also produce 1000+ unique values."""
    engine = _make_engine()
    seen: set[int] = set()
    for i in range(1000):
        c = engine._next_cloid(level_idx=i % 16)
        assert c not in seen, f"_next_cloid collision after {len(seen)} unique calls"
        seen.add(c)


def test_cloid_fits_in_int64():
    """Cloid must fit in signed int64 (Lighter/dYdX wire format)."""
    engine = _make_engine()
    engine._cloid_seq = 10_000_000  # exercise high seq values
    c = engine._next_cloid_for_leg("ARB-USD")
    assert 0 <= c < 2**63, f"cloid {c} exceeds int64 range"
