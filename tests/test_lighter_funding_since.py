"""Tests for LighterAdapter.get_funding_total_since — paginates Lighter
position_funding and sums per-market since a given unix timestamp."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from exchanges.lighter import LighterAdapter


def _make_adapter(funding_entries):
    """Build a LighterAdapter with _fetch_position_funding mocked."""
    a = LighterAdapter.__new__(LighterAdapter)
    a._signer = MagicMock()  # truthy so the method doesn't early-return
    a._fetch_position_funding = AsyncMock(return_value=funding_entries)
    return a


@pytest.mark.asyncio
async def test_get_funding_total_since_filters_by_timestamp():
    """Entries with timestamp < since_ts must be excluded; >= included."""
    entries = [
        MagicMock(timestamp=1000, change=10.0, market_id=0),  # before
        MagicMock(timestamp=2000, change=5.0, market_id=0),   # at
        MagicMock(timestamp=3000, change=2.0, market_id=0),   # after
    ]
    # Set .get to mimic dict-style access (the impl uses e.get("...") pattern).
    # Actual SDK objects support both attr and getattr-style — we test the
    # filter logic, so emulate `e.get("k", default)` via __dict__-aware lambda.
    for e in entries:
        e.get = lambda k, d=None, _e=e: getattr(_e, k, d)
    a = _make_adapter(entries)
    t0, t1 = await a.get_funding_total_since(
        since_ts=2000, market_id_token0=0,
    )
    # entries at t=2000 (change=5) + t=3000 (change=2) → sum = 7
    # signs invert (Lighter change > 0 = received → paid = -7)
    assert t0 == pytest.approx(-7.0)
    assert t1 == 0.0


@pytest.mark.asyncio
async def test_get_funding_total_since_routes_per_market_id():
    """Token0 entries go to t0, token1 entries go to t1 by mid."""
    entries = [
        MagicMock(timestamp=1000, change=4.0, market_id=0),
        MagicMock(timestamp=1000, change=6.0, market_id=50),
    ]
    for e in entries:
        e.get = lambda k, d=None, _e=e: getattr(_e, k, d)
    a = _make_adapter(entries)
    t0, t1 = await a.get_funding_total_since(
        since_ts=0, market_id_token0=0, market_id_token1=50,
    )
    assert t0 == pytest.approx(-4.0)
    assert t1 == pytest.approx(-6.0)


@pytest.mark.asyncio
async def test_get_funding_total_since_inverts_sign():
    """Lighter convention: change > 0 = user received funding.
    Our return convention: positive = paid, negative = received.
    Test: change=+10 → return -10."""
    entries = [
        MagicMock(timestamp=100, change=10.0, market_id=0),
    ]
    for e in entries:
        e.get = lambda k, d=None, _e=e: getattr(_e, k, d)
    a = _make_adapter(entries)
    t0, _ = await a.get_funding_total_since(
        since_ts=0, market_id_token0=0,
    )
    assert t0 == pytest.approx(-10.0)  # received → negative paid


@pytest.mark.asyncio
async def test_get_funding_total_since_returns_zeros_when_signer_none():
    """Cold/unconnected adapter (signer=None) returns (0, 0) without
    calling _fetch_position_funding."""
    a = LighterAdapter.__new__(LighterAdapter)
    a._signer = None
    a._fetch_position_funding = AsyncMock()
    t0, t1 = await a.get_funding_total_since(
        since_ts=0, market_id_token0=0, market_id_token1=50,
    )
    assert (t0, t1) == (0.0, 0.0)
    a._fetch_position_funding.assert_not_called()
