"""Tests for engine's predictive iter logic + per-leg fire."""
from unittest.mock import AsyncMock, MagicMock
import pytest

from state import StateHub


def _engine_with_book(eth_bid=2300.0, eth_ask=2301.0, arb_bid=0.130, arb_ask=0.131):
    """Build an engine with the lighter adapter's _ws_book_top pre-populated
    for ETH (mid 0) and ARB (mid 50)."""
    from engine import GridMakerEngine

    state = StateHub(hedge_ratio=0.98)
    state.operation_state = "active"
    state.current_operation_id = 1

    settings = MagicMock()
    settings.dydx_symbol_token0 = "ETH-USD"
    settings.dydx_symbol_token1 = "ARB-USD"
    settings.threshold_aggressive = 0.01
    settings.max_open_orders = 200
    settings.pool_token0_symbol = "ETH"
    settings.pool_token1_symbol = "ARB"
    settings.alert_webhook_url = ""
    settings.dydx_symbol = "ETH-USD"
    settings.min_rebalance_notional_usd = 0.50

    db = MagicMock()
    db.insert_order_log = AsyncMock()
    exchange = MagicMock()
    exchange.name = "lighter"
    exchange.subscribe_funding = MagicMock()
    exchange.place_long_term_order = AsyncMock()
    exchange.get_market_meta = AsyncMock()
    exchange._ws_book_top = {
        0: {"best_bid": eth_bid, "best_ask": eth_ask, "ts": 0},
        50: {"best_bid": arb_bid, "best_ask": arb_ask, "ts": 0},
    }

    pool = MagicMock(); beefy = MagicMock()
    eng = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=18,
    )
    eng._token0_mid = 0
    eng._token1_mid = 50
    return eng, exchange


@pytest.mark.asyncio
async def test_fire_predictive_leg_sells_at_bid():
    """delta > 0 (need to short more) → SELL at the best bid."""
    eng, exchange = _engine_with_book(eth_bid=2300.0, eth_ask=2301.0)
    await eng._fire_predictive_leg("ETH-USD", delta=0.001)
    exchange.place_long_term_order.assert_awaited_once()
    call = exchange.place_long_term_order.await_args
    assert call.kwargs["side"] == "sell"
    assert call.kwargs["price"] == 2300.0


@pytest.mark.asyncio
async def test_fire_predictive_leg_buys_at_ask():
    """delta < 0 (close some short) → BUY at the best ask."""
    eng, exchange = _engine_with_book(eth_bid=2300.0, eth_ask=2301.0)
    await eng._fire_predictive_leg("ETH-USD", delta=-0.001)
    exchange.place_long_term_order.assert_awaited_once()
    call = exchange.place_long_term_order.await_args
    assert call.kwargs["side"] == "buy"
    assert call.kwargs["price"] == 2301.0


@pytest.mark.asyncio
async def test_fire_predictive_leg_skips_below_dollar_floor():
    """Sub-$0.50 leg notional → no fire, no exception."""
    eng, exchange = _engine_with_book(eth_bid=2300.0, eth_ask=2301.0)
    await eng._fire_predictive_leg("ETH-USD", delta=0.0001)
    exchange.place_long_term_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_fire_predictive_leg_empty_book_raises_predictive_unavailable():
    """No book entry for symbol's market_id → raises PredictiveUnavailable."""
    from engine import PredictiveUnavailable
    eng, exchange = _engine_with_book()
    exchange._ws_book_top = {}
    with pytest.raises(PredictiveUnavailable):
        await eng._fire_predictive_leg("ETH-USD", delta=0.001)


@pytest.mark.asyncio
async def test_fire_predictive_leg_zero_delta_no_fire():
    eng, exchange = _engine_with_book()
    await eng._fire_predictive_leg("ETH-USD", delta=0.0)
    exchange.place_long_term_order.assert_not_awaited()
