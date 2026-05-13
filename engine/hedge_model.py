"""HedgeModel — predictive hedge model with cached L from V3 positions,
V3 formula evaluation, and verify-vs-actual divergence detection.

Per spec 2026-05-10-predictive-hedge-model-design.md:
- L cache TTL: 300s automatic refresh + on-demand refresh on >1% divergence
- predict() returns DISPLAY UNITS (decimals applied) — matches Beefy
  balances() semantics for direct float comparison.
- Engine uses ACTUAL (Beefy) as authoritative target; predicted is verify-only.
"""
from __future__ import annotations

import asyncio
import logging
import math
import time
from dataclasses import dataclass

from chains.v3_position import V3Position, V3PositionReader

logger = logging.getLogger(__name__)


REFRESH_TTL_S = 300.0
DIVERGENCE_THRESHOLD = 0.01  # 1%


@dataclass
class HedgeModelCache:
    L_main: int
    p_a_main: float
    p_b_main: float
    L_alt: int | None
    p_a_alt: float | None
    p_b_alt: float | None
    refreshed_at: float  # monotonic seconds
    # Raw V3 ticks (int) for the active concentrated-liquidity range.
    # `p_a_main`/`p_b_main` above store the RAW V3 ratio
    # (`1.0001^tick`), which is what `predict()` needs for the V3
    # amount formula. But `_maintain_grid` and `_on_grid_fill` work
    # with ticks, so storing them here saves a `log(...) / log(1.0001)`
    # roundtrip and — more importantly — sidesteps the
    # double-decimal-factor unit bug that produced ticks ~2x more
    # negative than reality (e.g. -574215 instead of -297890).
    # Verified live 2026-05-13 (op #29 smoke v2).
    tick_lower_main: int = 0
    tick_upper_main: int = 0


class HedgeModel:
    """Predictive hedge model. Owns the L cache and V3-formula evaluation.
    The engine calls predict() each iter and verifies against Beefy actual."""

    def __init__(self, v3_reader: V3PositionReader):
        self._reader = v3_reader
        self._cache: HedgeModelCache | None = None
        self._refresh_pending: bool = False

    def cache_stale(self) -> bool:
        if self._cache is None:
            return True
        return (time.monotonic() - self._cache.refreshed_at) > REFRESH_TTL_S

    def should_refresh(self) -> bool:
        return self.cache_stale() or self._refresh_pending

    async def refresh_cache(self) -> None:
        """Re-reads L_main + L_alt from V3 pool. Updates cache atomically.
        Failure preserves prior cache (so engine keeps using last known good)."""
        try:
            main, alt = await asyncio.gather(
                self._reader.read_position_main(),
                self._reader.read_position_alt(),
            )
            self._cache = HedgeModelCache(
                L_main=main.liquidity,
                p_a_main=math.pow(1.0001, main.tick_lower),
                p_b_main=math.pow(1.0001, main.tick_upper),
                L_alt=alt.liquidity if alt is not None else None,
                p_a_alt=math.pow(1.0001, alt.tick_lower) if alt is not None else None,
                p_b_alt=math.pow(1.0001, alt.tick_upper) if alt is not None else None,
                refreshed_at=time.monotonic(),
                tick_lower_main=main.tick_lower,
                tick_upper_main=main.tick_upper,
            )
            self._refresh_pending = False
        except Exception as e:
            logger.warning(f"HedgeModel.refresh_cache failed, keeping prior: {e}")
            # Leave _refresh_pending True (if it was True) so we retry next iter

    def predict(
        self, p_now: float, *, decimals0: int, decimals1: int,
    ) -> tuple[float, float] | None:
        """Returns (predicted_amount0_total, predicted_amount1_total) for the
        STRATEGY in DISPLAY UNITS (decimals applied), matching Beefy
        balances() semantics. Caller multiplies by user share.

        Returns None if cache empty (caller falls back to Beefy actual)."""
        if self._cache is None:
            return None
        c = self._cache
        # positionMain raw amounts
        a0_main = _v3_amount0(c.L_main, p_now, c.p_a_main, c.p_b_main)
        a1_main = _v3_amount1(c.L_main, p_now, c.p_a_main, c.p_b_main)
        # positionAlt raw amounts (if active)
        a0_alt = a1_alt = 0.0
        if c.L_alt is not None:
            a0_alt = _v3_amount0(c.L_alt, p_now, c.p_a_alt, c.p_b_alt)
            a1_alt = _v3_amount1(c.L_alt, p_now, c.p_a_alt, c.p_b_alt)
        # Scale raw → display units
        return (
            (a0_main + a0_alt) / (10 ** decimals0),
            (a1_main + a1_alt) / (10 ** decimals1),
        )

    def verify(
        self, *, predicted: tuple[float, float], actual: tuple[float, float],
    ) -> float:
        """Returns max relative divergence across both legs. If above
        DIVERGENCE_THRESHOLD, sets _refresh_pending=True so should_refresh()
        becomes True for the next iter."""
        d0 = abs(predicted[0] - actual[0]) / max(actual[0], 1e-18)
        d1 = abs(predicted[1] - actual[1]) / max(actual[1], 1e-18)
        max_div = max(d0, d1)
        if max_div > DIVERGENCE_THRESHOLD:
            self._refresh_pending = True
        return max_div


def _v3_amount0(L: int, p: float, p_a: float, p_b: float) -> float:
    """V3 token0 amount in raw units. Clamped 0 above p_b (single-asset edge)."""
    if p >= p_b:
        return 0.0
    p_use = max(p, p_a)
    return float(L) * (1.0 / math.sqrt(p_use) - 1.0 / math.sqrt(p_b))


def _v3_amount1(L: int, p: float, p_a: float, p_b: float) -> float:
    """V3 token1 amount in raw units. Clamped 0 below p_a."""
    if p <= p_a:
        return 0.0
    p_use = min(p, p_b)
    return float(L) * (math.sqrt(p_use) - math.sqrt(p_a))
