import pytest
from unittest.mock import AsyncMock, MagicMock
from state import StateHub
from engine.curve import GridLevel


@pytest.mark.asyncio
async def test_engine_iteration_in_range_builds_grid():
    """One iteration: reader returns position in range; engine builds + places grid."""
    from engine import GridMakerEngine

    state = StateHub()
    state.hedge_ratio = 1.0
    state.max_exposure_pct = 0.05

    settings = MagicMock()
    settings.active_exchange = "dydx"
    settings.dydx_symbol = "ETH-USD"
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"
    settings.threshold_aggressive = 0.05
    settings.threshold_recovery = 0.02
    settings.max_open_orders = 200
    settings.clm_vault_address = "0xvault"
    settings.clm_pool_address = "0xpool"
    settings.wallet_address = "0xwallet"

    db = MagicMock()
    db.insert_grid_order = AsyncMock()
    db.get_active_grid_orders = AsyncMock(return_value=[])
    db.insert_order_log = AsyncMock()

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(
        tick_size=0.1, step_size=0.001, min_notional=3.0,
    ))
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.batch_place = AsyncMock(return_value=[])
    exchange.batch_cancel = AsyncMock(return_value=0)

    pool_reader = MagicMock()
    pool_reader.read_price = AsyncMock(return_value=3000.0)

    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=78240, tick_upper=80580,  # ~$2700-$3300 range
        amount0=0.5, amount1=1500.0, share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
        decimals0=18, decimals1=6,
    )
    # Run one iteration
    await engine._iterate()

    # Range was set in state
    assert state.range_lower > 0
    assert state.range_upper > state.range_lower
    assert state.liquidity_l > 0


@pytest.mark.asyncio
async def test_engine_fill_updates_db_and_state():
    """When a fill arrives via WS, engine inserts to fills table and marks grid_order filled."""
    from engine import GridMakerEngine
    from exchanges.base import Fill

    state = StateHub()
    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"

    db = MagicMock()
    db.insert_fill = AsyncMock(return_value=42)
    db.mark_grid_order_filled = AsyncMock()
    db.insert_order_log = AsyncMock()

    exchange = MagicMock()
    exchange.name = "dydx"

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=MagicMock(), beefy_reader=MagicMock(),
    )

    fill = Fill(
        fill_id="f1", order_id="100",  # cloid as order_id
        symbol="ETH-USD", side="sell", size=0.001, price=2999.0,
        fee=0.001, fee_currency="USDC", liquidity="maker",
        realized_pnl=0.0, timestamp=1000.0,
    )
    await engine._on_fill(fill)
    db.insert_fill.assert_called_once()
    db.mark_grid_order_filled.assert_called_once_with("100", 42)
    assert state.total_maker_fills == 1


@pytest.mark.asyncio
async def test_engine_reconcile_runs_periodically():
    """Engine calls reconciler.reconcile() every N iterations."""
    from engine import GridMakerEngine

    state = StateHub()
    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.threshold_aggressive = 0.05
    settings.threshold_recovery = 0.02
    settings.max_open_orders = 200
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    db = MagicMock()
    db.get_active_grid_orders = AsyncMock(return_value=[])

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])
    exchange.cancel_long_term_order = AsyncMock()
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=3.0))
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.batch_place = AsyncMock(return_value=[])
    exchange.batch_cancel = AsyncMock(return_value=0)

    pool_reader = MagicMock()
    pool_reader.read_price = AsyncMock(return_value=3000.0)
    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=78240, tick_upper=80580, amount0=0.5, amount1=1500.0,
        share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
    )
    engine.RECONCILE_EVERY_N_ITERATIONS = 1
    await engine._iterate()
    exchange.get_open_orders_cloids.assert_called()


@pytest.mark.asyncio
async def test_engine_recovery_reconciles_on_start():
    """On start(), reconciler runs once before main loop."""
    import time
    from engine import GridMakerEngine

    state = StateHub()
    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"

    db = MagicMock()
    db.get_active_grid_orders = AsyncMock(return_value=[
        {"cloid": "999", "side": "sell"}  # stale from previous run
    ])
    db.mark_grid_order_cancelled = AsyncMock()

    exchange = MagicMock()
    exchange.connect = AsyncMock()
    exchange.disconnect = AsyncMock()
    exchange.subscribe_fills = AsyncMock()
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])  # nothing on exchange
    exchange.name = "dydx"

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=MagicMock(), beefy_reader=MagicMock(),
    )
    engine._iterate = AsyncMock()  # don't run main loop
    await engine.start()

    # The 999 should have been marked cancelled (it was in DB but not on exchange)
    db.mark_grid_order_cancelled.assert_called()
    cancelled_args = [c.args for c in db.mark_grid_order_cancelled.call_args_list]
    assert any("999" in args for args in cancelled_args)

    await engine.stop()
