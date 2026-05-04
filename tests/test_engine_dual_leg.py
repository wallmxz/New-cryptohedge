"""Engine._maybe_rebalance_leg: level-triggered taker per perp."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from engine import GridMakerEngine
from state import StateHub


@pytest.fixture
def engine_for_rebalance():
    state = StateHub(hedge_ratio=1.0)
    state.current_operation_id = 1
    state.operation_state = "active"

    settings = MagicMock()
    settings.dydx_symbol_token0 = "ARB-USD"
    settings.dydx_symbol_token1 = ""

    db = MagicMock()
    db.add_to_operation_accumulator = AsyncMock()
    db.insert_order_log = AsyncMock()

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.place_long_term_order = AsyncMock()

    pool = MagicMock(); beefy = MagicMock()
    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
    )
    return engine, exchange, db


@pytest.mark.asyncio
async def test_rebalance_leg_skips_below_min_notional(engine_for_rebalance):
    engine, exchange, _ = engine_for_rebalance
    # drift = 0.0001 ARB at $1.50 = $0.00015, below $1 min_notional
    await engine._maybe_rebalance_leg(
        symbol="ARB-USD", target=100.0, current=99.9999,
        min_notional=1.0, ref_price=1.50,
    )
    exchange.place_long_term_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_rebalance_leg_fires_sell_when_under_shorted(engine_for_rebalance):
    """target > current → drift > 0 → SELL more (add short)."""
    engine, exchange, _ = engine_for_rebalance
    await engine._maybe_rebalance_leg(
        symbol="ARB-USD", target=105.0, current=100.0,
        min_notional=1.0, ref_price=1.50,
    )
    exchange.place_long_term_order.assert_awaited_once()
    call = exchange.place_long_term_order.await_args
    assert call.kwargs["symbol"] == "ARB-USD"
    assert call.kwargs["side"] == "sell"
    assert abs(call.kwargs["size"] - 5.0) < 1e-9
    # Cross-spread for taker on a SELL = below current
    assert call.kwargs["price"] == 1.50 * 0.999


@pytest.mark.asyncio
async def test_rebalance_leg_fires_buy_when_over_shorted(engine_for_rebalance):
    engine, exchange, _ = engine_for_rebalance
    await engine._maybe_rebalance_leg(
        symbol="ARB-USD", target=95.0, current=100.0,
        min_notional=1.0, ref_price=1.50,
    )
    call = exchange.place_long_term_order.await_args
    assert call.kwargs["side"] == "buy"
    assert abs(call.kwargs["size"] - 5.0) < 1e-9
    assert call.kwargs["price"] == 1.50 * 1.001


@pytest.mark.asyncio
async def test_rebalance_leg_attributes_fee_to_correct_leg_token0(engine_for_rebalance):
    engine, exchange, db = engine_for_rebalance
    engine._hub.current_operation_id = 42
    await engine._maybe_rebalance_leg(
        symbol="ARB-USD", target=105.0, current=100.0,
        min_notional=1.0, ref_price=1.50,
    )
    db.add_to_operation_accumulator.assert_awaited_once()
    call = db.add_to_operation_accumulator.await_args
    # Positional args: (op_id, field, delta)
    assert call.args[1] == "perp_fees_paid_token0"
    assert call.args[2] > 0


@pytest.mark.asyncio
async def test_rebalance_leg_attributes_fee_to_token1_when_dual_leg():
    """In dual-leg, when symbol matches dydx_symbol_token1, fee goes to token1 accumulator."""
    state = StateHub(hedge_ratio=1.0)
    state.current_operation_id = 42
    state.operation_state = "active"

    settings = MagicMock()
    settings.dydx_symbol_token0 = "ARB-USD"
    settings.dydx_symbol_token1 = "ETH-USD"

    db = MagicMock()
    db.add_to_operation_accumulator = AsyncMock()
    db.insert_order_log = AsyncMock()

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.place_long_term_order = AsyncMock()

    pool = MagicMock(); beefy = MagicMock()
    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
    )

    # Fire on the ETH-USD leg
    await engine._maybe_rebalance_leg(
        symbol="ETH-USD", target=0.05, current=0.04,
        min_notional=1.0, ref_price=4000.0,
    )

    db.add_to_operation_accumulator.assert_awaited_once()
    call = db.add_to_operation_accumulator.await_args
    assert call.args[1] == "perp_fees_paid_token1"  # ETH-USD is token1


@pytest.mark.asyncio
async def test_rebalance_leg_does_not_attribute_fee_when_no_active_operation(engine_for_rebalance):
    """If current_operation_id is None, skip the accumulator (no op to bill)."""
    engine, exchange, db = engine_for_rebalance
    engine._hub.current_operation_id = None  # no active op
    await engine._maybe_rebalance_leg(
        symbol="ARB-USD", target=105.0, current=100.0,
        min_notional=1.0, ref_price=1.50,
    )
    exchange.place_long_term_order.assert_awaited_once()  # taker still fires
    db.add_to_operation_accumulator.assert_not_awaited()
