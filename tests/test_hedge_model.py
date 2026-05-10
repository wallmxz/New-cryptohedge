"""Tests for engine/hedge_model.py — predictive hedge model with
L cache, V3 formula prediction, and verify-vs-actual divergence detection.

Spec: docs/superpowers/specs/2026-05-10-predictive-hedge-model-design.md
"""
from __future__ import annotations

import math
import time
import pytest
from unittest.mock import AsyncMock, MagicMock
from chains.v3_position import V3Position
from engine.hedge_model import HedgeModel, HedgeModelCache, REFRESH_TTL_S


def _make_model(main_pos=None, alt_pos=None, refresh_raises=False):
    """Build a HedgeModel with a mocked V3PositionReader."""
    reader = MagicMock()
    if refresh_raises:
        reader.read_position_main = AsyncMock(side_effect=Exception("RPC down"))
        reader.read_position_alt = AsyncMock(return_value=alt_pos)
    else:
        reader.read_position_main = AsyncMock(
            return_value=main_pos or V3Position(
                liquidity=1000_000_000_000_000_000_000_000,  # raw L
                tick_lower=96040,
                tick_upper=97540,
            ),
        )
        reader.read_position_alt = AsyncMock(return_value=alt_pos)
    return HedgeModel(reader), reader


@pytest.mark.asyncio
async def test_predict_returns_none_when_cache_empty():
    """Cold model returns None — caller falls back to Beefy actual."""
    model, _ = _make_model()
    p_now = math.pow(1.0001, 96800)  # arbitrary in-range
    assert model.predict(p_now, decimals0=18, decimals1=18) is None


@pytest.mark.asyncio
async def test_predict_main_only_when_alt_inactive():
    """Cache with L_alt=None → predicted = positionMain contribution only."""
    model, _ = _make_model(alt_pos=None)
    await model.refresh_cache()
    p_now = math.pow(1.0001, 96800)
    predicted = model.predict(p_now, decimals0=18, decimals1=18)
    assert predicted is not None
    a0, a1 = predicted
    assert a0 > 0  # in-range, both legs > 0
    assert a1 > 0
    # Sanity: with alt=None, predicted from a single L should be deterministic
    # (we don't assert exact value here — that's covered by a separate formula test)


@pytest.mark.asyncio
async def test_predict_includes_alt_when_active():
    """Both ranges active → predicted = sum of main + alt contributions."""
    model, _ = _make_model(
        alt_pos=V3Position(
            liquidity=500_000_000_000_000_000_000_000,
            tick_lower=96100,
            tick_upper=97400,
        ),
    )
    await model.refresh_cache()
    p_now = math.pow(1.0001, 96800)
    predicted_with_alt = model.predict(p_now, decimals0=18, decimals1=18)

    # Build a second model with alt=None for comparison
    model_no_alt, _ = _make_model(alt_pos=None)
    await model_no_alt.refresh_cache()
    predicted_main_only = model_no_alt.predict(p_now, decimals0=18, decimals1=18)

    # Alt contribution should make predicted strictly larger in both legs
    # (alt range straddles current p, so it contributes to both)
    assert predicted_with_alt[0] > predicted_main_only[0]
    assert predicted_with_alt[1] > predicted_main_only[1]


@pytest.mark.asyncio
async def test_verify_returns_max_relative_divergence():
    """verify(predicted, actual) returns max(|d0|/a0, |d1|/a1)."""
    model, _ = _make_model()
    # No cache needed — verify is a pure function
    div = model.verify(predicted=(0.95, 99.0), actual=(1.0, 100.0))
    # d0 = 0.05/1.0 = 5%, d1 = 1/100 = 1% → max = 5%
    assert abs(div - 0.05) < 1e-9


@pytest.mark.asyncio
async def test_verify_schedules_refresh_when_divergence_above_threshold():
    """Divergence > 1% sets _refresh_pending=True (caller checks via should_refresh())."""
    model, _ = _make_model()
    # Build a fresh cache so should_refresh isn't True from cold start
    await model.refresh_cache()
    assert model.should_refresh() is False  # fresh cache, no pending
    model.verify(predicted=(0.90, 100.0), actual=(1.0, 100.0))  # 10% on leg 0
    assert model.should_refresh() is True


@pytest.mark.asyncio
async def test_refresh_cache_keeps_prior_on_rpc_failure():
    """If reader raises, prior cache is preserved (not nulled). _refresh_pending
    is NOT cleared (so caller will retry next iter)."""
    model, _ = _make_model()
    await model.refresh_cache()  # populate cache
    cache_before = model._cache
    assert cache_before is not None

    # Now switch reader to raise
    model._reader.read_position_main = AsyncMock(side_effect=Exception("RPC down"))
    await model.refresh_cache()  # should not raise; cache unchanged
    assert model._cache is cache_before


@pytest.mark.asyncio
async def test_cache_stale_after_ttl(monkeypatch):
    """cache_stale() returns True when (monotonic - refreshed_at) > REFRESH_TTL_S."""
    model, _ = _make_model()
    await model.refresh_cache()
    assert model.cache_stale() is False

    # Fast-forward monotonic by REFRESH_TTL_S + 1
    fake_now = time.monotonic() + REFRESH_TTL_S + 1.0
    monkeypatch.setattr(time, "monotonic", lambda: fake_now)
    assert model.cache_stale() is True


@pytest.mark.asyncio
async def test_predict_handles_asymmetric_decimals():
    """Asymmetric pair (WETH 18 / USDC 6) — predicted_amount1 should be
    scaled by 10^6 not 10^18. Catches regressions where someone uses a
    single decimals factor or swaps the two."""
    model, _ = _make_model(alt_pos=None)
    await model.refresh_cache()
    p_now = math.pow(1.0001, 96800)

    # Symmetric (control): decimals0=decimals1=18
    sym = model.predict(p_now, decimals0=18, decimals1=18)

    # Asymmetric: decimals0=18 (WETH), decimals1=6 (USDC) — same raw L
    asym = model.predict(p_now, decimals0=18, decimals1=6)

    assert sym is not None and asym is not None
    # token0 (decimals0=18) is identical in both calls
    assert math.isclose(sym[0], asym[0])
    # token1 (decimals1=18 vs 6): asym should be 10^12 LARGER (less divide)
    assert math.isclose(asym[1] / sym[1], 10**12, rel_tol=1e-6)


def test_v3_amount_formulas_match_canonical_uniswap_math():
    """Lock the V3 formulas to known values. Regressions in sign or
    operand order will fail this test (current tests only check sign/
    monotonicity, not magnitude)."""
    from engine.hedge_model import _v3_amount0, _v3_amount1

    # In-range case: L=10^21, p=1.0, p_a=0.5, p_b=2.0
    L = 10**21
    p = 1.0
    p_a = 0.5
    p_b = 2.0

    # amount0 = L * (1/sqrt(p) - 1/sqrt(p_b))
    #         = 10^21 * (1/1 - 1/sqrt(2))
    #         = 10^21 * (1 - 0.7071067811865475)
    #         = 10^21 * 0.2928932188134525
    expected_a0 = 10**21 * (1.0 - 1.0 / math.sqrt(2.0))
    actual_a0 = _v3_amount0(L, p, p_a, p_b)
    assert math.isclose(actual_a0, expected_a0, rel_tol=1e-12), (
        f"_v3_amount0 returned {actual_a0}, expected {expected_a0}"
    )

    # amount1 = L * (sqrt(p) - sqrt(p_a))
    #         = 10^21 * (1 - sqrt(0.5))
    #         = 10^21 * (1 - 0.7071067811865476)
    #         = 10^21 * 0.2928932188134524
    expected_a1 = 10**21 * (1.0 - math.sqrt(0.5))
    actual_a1 = _v3_amount1(L, p, p_a, p_b)
    assert math.isclose(actual_a1, expected_a1, rel_tol=1e-12), (
        f"_v3_amount1 returned {actual_a1}, expected {expected_a1}"
    )

    # Edge clamping
    assert _v3_amount0(L, p_b, p_a, p_b) == 0.0  # at p_b → 0
    assert _v3_amount0(L, p_b + 0.5, p_a, p_b) == 0.0  # above p_b → 0
    assert _v3_amount1(L, p_a, p_a, p_b) == 0.0  # at p_a → 0
    assert _v3_amount1(L, p_a - 0.1, p_a, p_b) == 0.0  # below p_a → 0


@pytest.mark.asyncio
async def test_refresh_pending_survives_failed_refresh():
    """If verify scheduled a refresh (_refresh_pending=True) and the
    next refresh attempt RPC-fails, _refresh_pending must REMAIN True
    so the engine retries on the next iter (not silently swallowed)."""
    model, _ = _make_model()
    await model.refresh_cache()  # populate cache, _refresh_pending=False

    # Trigger pending via verify divergence
    model.verify(predicted=(0.5, 100.0), actual=(1.0, 100.0))  # 50% on leg 0
    assert model._refresh_pending is True

    # Now fail the next refresh
    model._reader.read_position_main = AsyncMock(side_effect=Exception("RPC down"))
    await model.refresh_cache()  # raises internally; logs warning

    # Pending must still be True (retry next iter)
    assert model._refresh_pending is True
