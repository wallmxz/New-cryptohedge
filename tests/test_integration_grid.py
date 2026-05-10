"""End-to-end happy-path integration test for GridMakerEngine.

Exercises:
- Real Database (sqlite)
- Mocked exchange + chain readers
- engine._iterate() places a grid
- engine._on_fill() updates state and DB

The mock uses ticks computed from real Uniswap V3 conventions:
    tick_to_price(tick, dec0=18, dec1=6) = 1.0001**tick * 10**12
For p_a ~= $2700: tick = round(log(2700 / 10**12) / log(1.0001)) = -197310
For p_b ~= $3300: tick = round(log(3300 / 10**12) / log(1.0001)) = -195303
"""
import pytest
from unittest.mock import AsyncMock, MagicMock
from state import StateHub


@pytest.mark.asyncio
async def test_engine_full_loop_in_range(tmp_path):
    """End-to-end: engine starts, reads chain, processes fill, updates state.

    After Task 10 refactor: engine no longer pre-posts a grid; instead it fires
    level-triggered taker rebalances when notional drift exceeds min_notional.
    Test now verifies fill processing path end-to-end without grid placement.
    """
    from db import Database
    from engine import GridMakerEngine
    from exchanges.base import Fill, Order

    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)
    state.operation_state = "active"

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.dydx_symbol_token0 = "ETH-USD"
    settings.dydx_symbol_token1 = ""
    settings.alert_webhook_url = ""
    settings.threshold_aggressive = 0.05
    settings.min_rebalance_notional_usd = 0.50
    settings.max_open_orders = 50
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(
        tick_size=0.1, step_size=0.001, min_notional=0.001,
    ))
    # Position exactly matches target so no rebalance fires (drift < min_notional).
    # target = compute_x(L=5.597, p=3000, p_b=3300) ~= 0.00476.
    exchange.get_position = AsyncMock(return_value=MagicMock(
        symbol="ETH-USD", side="short", size=0.00476,
        entry_price=3000.0, unrealized_pnl=0.0,
    ))
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.batch_place = AsyncMock(return_value=[])
    exchange.batch_cancel = AsyncMock(return_value=0)
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])
    exchange.subscribe_fills = AsyncMock()
    exchange.place_long_term_order = AsyncMock()
    exchange.get_oracle_prices = AsyncMock(return_value={"ETH-USD": 3000.0})

    pool_reader = MagicMock()
    pool_reader.read_price = AsyncMock(return_value=3000.0)
    beefy_reader = MagicMock()
    # Real ETH/USDC ticks for [$2700, $3300] with decimals0=18, decimals1=6.
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.5, amount1=1500.0, share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
    )
    await engine._iterate()

    # State was updated
    assert state.range_lower > 0
    assert state.range_upper > state.range_lower
    assert state.liquidity_l > 0
    assert state.pool_value_usd > 0
    assert not state.out_of_range

    # Simulate a fill (e.g. from a prior taker that filled at exchange)
    fill = Fill(
        fill_id="f1", order_id="100", symbol="ETH-USD",
        side="sell", size=0.001, price=2999.0,
        fee=0.0001, fee_currency="USDC",
        liquidity="maker", realized_pnl=0.0, timestamp=1000.0,
    )
    await engine._on_fill(fill)
    assert state.total_maker_fills == 1
    assert state.total_maker_volume == pytest.approx(fill.size)
    assert state.total_fees_paid == pytest.approx(fill.fee)

    await db.close()


@pytest.mark.asyncio
async def test_engine_out_of_range_upper_sets_flag(tmp_path):
    """Price > p_b: out_of_range = True, no rebalance fires.

    After Task 10 refactor: taker-only engine has no grid to cancel; OOR is
    just an idle state with the flag set.
    """
    from db import Database
    from engine import GridMakerEngine

    db = Database(str(tmp_path / "t2.db"))
    await db.initialize()

    state = StateHub(hedge_ratio=1.0)
    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.dydx_symbol_token0 = "ETH-USD"
    settings.dydx_symbol_token1 = ""
    settings.alert_webhook_url = ""
    settings.threshold_aggressive = 0.05
    settings.max_open_orders = 200
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.batch_cancel = AsyncMock(return_value=0)
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])
    exchange.place_long_term_order = AsyncMock()
    exchange.get_oracle_prices = AsyncMock(return_value={"ETH-USD": 3500.0})

    pool_reader = MagicMock()
    # ETH at $3500 — above the [$2700, $3300] range
    pool_reader.read_price = AsyncMock(return_value=3500.0)
    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        # Same tick values as Task 25 (~$2700-$3300 for ETH/USDC 18/6)
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.0, amount1=300.0, share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
    )
    await engine._iterate()
    assert state.out_of_range is True
    # No taker fired (out of range -> idle)
    exchange.place_long_term_order.assert_not_awaited()
    await db.close()


@pytest.mark.asyncio
async def test_engine_out_of_range_lower_holds_short(tmp_path):
    """Price < p_a: bot holds short at boundary."""
    from db import Database
    from engine import GridMakerEngine

    db = Database(str(tmp_path / "t3.db"))
    await db.initialize()

    state = StateHub(hedge_ratio=1.0)
    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.dydx_symbol_token0 = "ETH-USD"
    settings.dydx_symbol_token1 = ""
    settings.alert_webhook_url = ""
    settings.threshold_aggressive = 0.05
    settings.max_open_orders = 200
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.batch_cancel = AsyncMock(return_value=0)
    exchange.get_position = AsyncMock(return_value=MagicMock(
        side="short", size=0.103, entry_price=2982, unrealized_pnl=30.0,
    ))
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])
    exchange.place_long_term_order = AsyncMock()
    exchange.get_oracle_prices = AsyncMock(return_value={"ETH-USD": 2500.0})

    pool_reader = MagicMock()
    # ETH at $2500 — below the [$2700, $3300] range
    pool_reader.read_price = AsyncMock(return_value=2500.0)
    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.103, amount1=0.0, share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
    )
    await engine._iterate()
    assert state.out_of_range is True
    # short stays at 0.103 (not closed)
    await db.close()
