# tests/test_integration_operation.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from state import StateHub


@pytest.mark.asyncio
async def test_full_operation_lifecycle(tmp_path):
    """End-to-end: start -> fill -> stop -> history."""
    from db import Database
    from engine import GridMakerEngine
    from exchanges.base import Order, Fill

    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.alert_webhook_url = ""
    settings.threshold_aggressive = 0.05
    settings.threshold_recovery = 0.02
    settings.max_open_orders = 50
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    placed = []

    async def fake_place(**kw):
        placed.append(kw)
        return Order(order_id=str(kw["cloid_int"]), symbol=kw["symbol"],
                     side=kw["side"], size=kw["size"], price=kw["price"], status="open")

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=0.001))
    exchange.get_position = AsyncMock(return_value=None)  # initially no position
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.place_long_term_order = AsyncMock(side_effect=fake_place)
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

    # Phase 1: start
    op_id = await engine.start_operation()
    assert state.operation_state == "active"
    assert len(placed) == 1  # bootstrap order

    # Phase 2: simulate a maker fill during active op
    fill = Fill(
        fill_id="f1", order_id="100", symbol="ETH-USD", side="sell", size=0.001,
        price=2999.0, fee=0.0003, fee_currency="USDC", liquidity="maker",
        realized_pnl=0.0, timestamp=1500.0,
    )
    await engine._on_fill(fill)
    fills = await db.get_fills()
    assert any(f["operation_id"] == op_id for f in fills)

    # Phase 3: stop
    # Need a position to close - patch get_position to return one
    exchange.get_position = AsyncMock(return_value=MagicMock(
        side="short", size=0.005, entry_price=3000.0, unrealized_pnl=0.0,
    ))
    result = await engine.stop_operation()
    assert state.operation_state == "none"
    assert "final_net_pnl" in result

    # Phase 4: history
    history = await db.get_operations(limit=10)
    assert len(history) == 1
    assert history[0]["id"] == op_id
    assert history[0]["status"] == "closed"

    # Phase 5: can start again - should succeed
    op_id_2 = await engine.start_operation()
    assert op_id_2 != op_id
    assert state.operation_state == "active"

    await db.close()
