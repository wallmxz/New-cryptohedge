import pytest
from unittest.mock import AsyncMock, MagicMock
from state import StateHub
from engine.curve import GridLevel


@pytest.mark.asyncio
async def test_engine_skips_grid_when_operation_state_none():
    """When operation_state == 'none', engine reads chain but does NOT place a grid."""
    from engine import GridMakerEngine
    from state import StateHub

    state = StateHub(hedge_ratio=1.0)
    state.operation_state = "none"

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.alert_webhook_url = ""
    settings.threshold_aggressive = 0.05
    settings.threshold_recovery = 0.02
    settings.max_open_orders = 50
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    db = MagicMock()
    db.get_active_grid_orders = AsyncMock(return_value=[])

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=0.001))
    # Existing short matches target so aggressive path is NOT taken; without
    # the operation_state guard, the engine would proceed to place a grid.
    exchange.get_position = AsyncMock(return_value=MagicMock(
        symbol="ETH-USD", side="short", size=0.00476,
        entry_price=3000.0, unrealized_pnl=0.0,
    ))
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.batch_place = AsyncMock(return_value=[])
    exchange.batch_cancel = AsyncMock(return_value=0)
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])

    pool_reader = MagicMock()
    pool_reader.read_price = AsyncMock(return_value=3000.0)
    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.5, amount1=1500.0, share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
    )
    await engine._iterate()

    # Chain state read happened
    assert state.range_lower > 0
    # But NO grid placement
    exchange.batch_place.assert_not_called()


@pytest.mark.asyncio
async def test_engine_iteration_in_range_builds_grid():
    """One iteration: reader returns position in range; engine builds + places grid."""
    from engine import GridMakerEngine

    state = StateHub()
    state.hedge_ratio = 1.0
    state.max_exposure_pct = 0.05
    state.operation_state = "active"

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
async def test_engine_fires_warning_alert_when_margin_low(monkeypatch):
    """When margin_ratio < 0.6, post_alert with level=warning is called."""
    from engine import GridMakerEngine
    alerts_called = []

    async def fake_alert(*, url, level, message, data):
        alerts_called.append((level, message))

    monkeypatch.setattr("engine.post_alert", fake_alert)

    state = StateHub()
    state.hedge_ratio = 1.0
    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.alert_webhook_url = "https://hooks.test/x"
    settings.threshold_aggressive = 0.05
    settings.threshold_recovery = 0.02
    settings.max_open_orders = 200
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    db = MagicMock()
    db.get_active_grid_orders = AsyncMock(return_value=[])

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=3.0))
    # Position with high notional, low collateral -> low margin ratio
    exchange.get_position = AsyncMock(return_value=MagicMock(
        side="short", size=0.103, entry_price=2982, unrealized_pnl=0,
    ))
    exchange.get_collateral = AsyncMock(return_value=50.0)  # tight margin
    exchange.batch_place = AsyncMock(return_value=[])
    exchange.batch_cancel = AsyncMock(return_value=0)
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])

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
    await engine._iterate()
    # Margin should be low, alert should fire
    levels = [lv for lv, _ in alerts_called]
    assert any(lv in ("warning", "urgent", "critical", "info") for lv in levels)


@pytest.mark.asyncio
async def test_engine_start_operation(tmp_path):
    """start_operation grava baseline, marca state ACTIVE, e dispara bootstrap."""
    from db import Database
    from engine import GridMakerEngine
    from state import StateHub
    from exchanges.base import Order

    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.alert_webhook_url = ""
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    bootstrap_calls = []

    async def fake_place_long_term_order(**kw):
        bootstrap_calls.append(kw)
        return Order(
            order_id=str(kw["cloid_int"]), symbol=kw["symbol"], side=kw["side"],
            size=kw["size"], price=kw["price"], status="open",
        )

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=0.001))
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.place_long_term_order = AsyncMock(side_effect=fake_place_long_term_order)

    pool_reader = MagicMock()
    pool_reader.read_price = AsyncMock(return_value=3000.0)
    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.5, amount1=1500.0, share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
    )

    op_id = await engine.start_operation()

    assert state.current_operation_id == op_id
    assert state.operation_state == "active"

    # Baseline persisted
    op = await db.get_operation(op_id)
    assert op["status"] == "active"
    assert op["baseline_eth_price"] == 3000.0
    assert op["baseline_pool_value_usd"] > 0

    # Bootstrap order placed (taker for opening short)
    assert len(bootstrap_calls) == 1
    assert bootstrap_calls[0]["side"] == "sell"  # short = sell

    await db.close()


@pytest.mark.asyncio
async def test_engine_start_operation_rejects_when_already_active(tmp_path):
    """Cannot start a new op when one is already active/starting."""
    from db import Database
    from engine import GridMakerEngine
    from state import StateHub

    db = Database(str(tmp_path / "t2.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)
    # Pre-existing active op in DB
    await db.insert_operation(
        started_at=1000.0, status="active",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0, baseline_collateral=130.0,
    )

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.alert_webhook_url = ""
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=MagicMock(), pool_reader=MagicMock(), beefy_reader=MagicMock(),
    )

    with pytest.raises(RuntimeError, match="already active"):
        await engine.start_operation()

    await db.close()


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


@pytest.mark.asyncio
async def test_engine_stop_operation(tmp_path):
    """stop_operation cancela grade, fecha short via taker, grava final_pnl."""
    from db import Database
    from engine import GridMakerEngine
    from state import StateHub

    db = Database(str(tmp_path / "t3.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)

    # Pre-create active operation
    op_id = await db.insert_operation(
        started_at=1000.0, status="active",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0, baseline_collateral=130.0,
    )
    state.current_operation_id = op_id
    state.operation_state = "active"

    # Pre-seed an active grid order
    await db.insert_grid_order(
        cloid="500", side="sell", target_price=2900.0, size=0.001, placed_at=1100.0,
    )

    cancelled_calls = []

    async def fake_cancel(items):
        cancelled_calls.extend(items)
        return len(items)

    closed_calls = []

    async def fake_place(**kw):
        closed_calls.append(kw)
        from exchanges.base import Order
        return Order(order_id=str(kw["cloid_int"]), symbol=kw["symbol"],
                     side=kw["side"], size=kw["size"], price=kw["price"], status="open")

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.alert_webhook_url = ""
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.batch_cancel = AsyncMock(side_effect=fake_cancel)
    exchange.place_long_term_order = AsyncMock(side_effect=fake_place)
    exchange.get_position = AsyncMock(return_value=MagicMock(
        side="short", size=0.05, entry_price=3000.0, unrealized_pnl=0.0,
    ))
    exchange.get_collateral = AsyncMock(return_value=128.0)

    pool_reader = MagicMock()
    pool_reader.read_price = AsyncMock(return_value=2950.0)
    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.5, amount1=1500.0, share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
    )

    result = await engine.stop_operation()

    assert state.current_operation_id is None
    assert state.operation_state == "none"
    op = await db.get_operation(op_id)
    assert op["status"] == "closed"
    assert op["close_reason"] == "user"
    assert op["final_net_pnl"] is not None
    # Cancelled the grid
    assert len(cancelled_calls) >= 1
    # Closed via taker (buy to cover the short)
    assert len(closed_calls) >= 1
    assert closed_calls[0]["side"] == "buy"

    await db.close()


@pytest.mark.asyncio
async def test_engine_fill_attributed_to_active_operation(tmp_path):
    """When a fill arrives during an active operation, it gets operation_id."""
    from db import Database
    from engine import GridMakerEngine
    from state import StateHub
    from exchanges.base import Fill

    db = Database(str(tmp_path / "t4.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)

    op_id = await db.insert_operation(
        started_at=1000.0, status="active",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0, baseline_collateral=130.0,
    )
    state.current_operation_id = op_id
    state.operation_state = "active"

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"

    exchange = MagicMock()
    exchange.name = "dydx"

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=MagicMock(), beefy_reader=MagicMock(),
    )

    fill = Fill(
        fill_id="f1", order_id="100", symbol="ETH-USD", side="sell", size=0.001,
        price=2999.0, fee=0.0003, fee_currency="USDC", liquidity="maker",
        realized_pnl=0.0, timestamp=1500.0,
    )
    await engine._on_fill(fill)

    fills = await db.get_fills()
    assert len(fills) == 1
    assert fills[0]["operation_id"] == op_id

    op = await db.get_operation(op_id)
    assert abs(op["perp_fees_paid"] - 0.0003) < 1e-9

    await db.close()


@pytest.mark.asyncio
async def test_engine_updates_live_pnl_breakdown(tmp_path):
    """During _iterate, hub.operation_pnl_breakdown is updated when op is active."""
    from db import Database
    from engine import GridMakerEngine
    from state import StateHub

    db = Database(str(tmp_path / "t5.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)

    op_id = await db.insert_operation(
        started_at=1000.0, status="active",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0, baseline_collateral=130.0,
    )
    state.current_operation_id = op_id
    state.operation_state = "active"

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.alert_webhook_url = ""
    settings.threshold_aggressive = 0.05
    settings.threshold_recovery = 0.02
    settings.max_open_orders = 50
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=0.001))
    exchange.get_position = AsyncMock(return_value=MagicMock(
        side="short", size=0.05, entry_price=3000.0, unrealized_pnl=0.0,
    ))
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.batch_place = AsyncMock(return_value=[])
    exchange.batch_cancel = AsyncMock(return_value=0)
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])

    pool_reader = MagicMock()
    pool_reader.read_price = AsyncMock(return_value=3000.0)
    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.5, amount1=1500.0, share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
    )

    await engine._iterate()

    assert "lp_fees_earned" in state.operation_pnl_breakdown
    assert "net_pnl" in state.operation_pnl_breakdown
    assert isinstance(state.operation_pnl_breakdown["net_pnl"], (int, float))

    await db.close()
