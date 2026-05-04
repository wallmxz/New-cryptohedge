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


@pytest.mark.asyncio
async def test_iterate_dual_leg_calls_rebalance_for_both_legs():
    from engine import GridMakerEngine
    state = StateHub(hedge_ratio=1.0)
    state.operation_state = "active"
    state.current_operation_id = 1

    settings = MagicMock()
    settings.dydx_symbol_token0 = "ARB-USD"
    settings.dydx_symbol_token1 = "ETH-USD"
    settings.threshold_aggressive = 0.01
    settings.max_open_orders = 200
    settings.pool_token0_symbol = "ARB"
    settings.pool_token1_symbol = "WETH"
    settings.alert_webhook_url = ""
    settings.dydx_symbol = "ARB-USD"  # legacy property; some callers read it

    db = MagicMock()
    db.get_active_grid_orders = AsyncMock(return_value=[])
    db.add_to_operation_accumulator = AsyncMock()
    db.insert_order_log = AsyncMock()
    db.get_operation = AsyncMock(return_value=None)

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.place_long_term_order = AsyncMock()
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=1.0))
    exchange.get_oracle_prices = AsyncMock(return_value={"ARB-USD": 1.50, "ETH-USD": 4000.0})
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])

    pool = MagicMock()
    pool.read_price = AsyncMock(return_value=0.000375)  # ARB/WETH ratio
    beefy = MagicMock()
    beefy.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-81121, tick_upper=-76012,  # ~0.0003-0.0005 ARB/WETH (decimals 18,18)
        amount0=100.0, amount1=0.0375, share=1.0, raw_balance=10**18,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=18,  # ARB and WETH both 18 decimals
    )

    # Spy on _maybe_rebalance_leg
    rebalance_calls = []
    original = engine._maybe_rebalance_leg
    async def spy(*args, **kwargs):
        rebalance_calls.append(kwargs.get("symbol"))
        return await original(*args, **kwargs)
    engine._maybe_rebalance_leg = spy

    await engine._iterate()
    assert "ARB-USD" in rebalance_calls
    assert "ETH-USD" in rebalance_calls


@pytest.mark.asyncio
async def test_iterate_single_leg_only_calls_token0():
    from engine import GridMakerEngine
    state = StateHub(hedge_ratio=1.0)
    state.operation_state = "active"
    state.current_operation_id = 1

    settings = MagicMock()
    settings.dydx_symbol_token0 = "ETH-USD"
    settings.dydx_symbol_token1 = ""  # single-leg
    settings.threshold_aggressive = 0.01
    settings.max_open_orders = 200
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"
    settings.alert_webhook_url = ""
    settings.dydx_symbol = "ETH-USD"

    db = MagicMock()
    db.get_active_grid_orders = AsyncMock(return_value=[])
    db.add_to_operation_accumulator = AsyncMock()
    db.insert_order_log = AsyncMock()
    db.get_operation = AsyncMock(return_value=None)

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.place_long_term_order = AsyncMock()
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=1.0))
    exchange.get_oracle_prices = AsyncMock(return_value={"ETH-USD": 4000.0})
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])

    pool = MagicMock()
    pool.read_price = AsyncMock(return_value=3000.0)  # in range [2700, 3300]
    beefy = MagicMock()
    beefy.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-197310, tick_upper=-195303,  # ~$2700-$3300 with decimals 18,6
        amount0=0.05, amount1=200.0, share=1.0, raw_balance=10**18,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
    )
    # Update oracle price to match in-range price
    exchange.get_oracle_prices = AsyncMock(return_value={"ETH-USD": 3000.0})

    rebalance_calls = []
    original = engine._maybe_rebalance_leg
    async def spy(*args, **kwargs):
        rebalance_calls.append(kwargs.get("symbol"))
        return await original(*args, **kwargs)
    engine._maybe_rebalance_leg = spy

    await engine._iterate()
    assert rebalance_calls == ["ETH-USD"]  # only token0 leg
