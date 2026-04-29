import pytest
from db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


async def test_initialize_creates_tables(db):
    tables = await db.list_tables()
    assert "config" in tables
    assert "deposits" in tables
    assert "fills" in tables
    assert "funding" in tables
    assert "pool_snapshots" in tables
    assert "order_log" in tables
    assert "grid_orders" in tables


async def test_insert_and_get_fill(db):
    await db.insert_fill(
        timestamp=1000.0,
        exchange="hyperliquid",
        symbol="ARB",
        side="sell",
        size=100.0,
        price=1.05,
        fee=0.015,
        fee_currency="USDC",
        liquidity="maker",
        realized_pnl=0.0,
        order_id="ord-1",
    )
    fills = await db.get_fills(exchange="hyperliquid", symbol="ARB")
    assert len(fills) == 1
    assert fills[0]["side"] == "sell"
    assert fills[0]["liquidity"] == "maker"


async def test_insert_pool_snapshot(db):
    await db.insert_pool_snapshot(
        timestamp=1000.0,
        pool_value_usd=204.0,
        token0_amount=1500.0,
        token1_amount=0.3,
        hedge_value_usd=190.0,
        hedge_pnl=-3.8,
        pool_pnl=4.0,
        net_pnl=1.75,
        funding_cumulative=0.15,
        fees_earned_cumulative=1.50,
        fees_paid_cumulative=0.30,
    )
    snaps = await db.get_pool_snapshots(limit=10)
    assert len(snaps) == 1
    assert snaps[0]["pool_value_usd"] == 204.0


async def test_insert_order_log(db):
    await db.insert_order_log(
        timestamp=1000.0,
        exchange="hyperliquid",
        action="place",
        side="sell",
        size=50.0,
        price=1.06,
        reason="exposure_rebalance",
    )
    logs = await db.get_order_logs(limit=10)
    assert len(logs) == 1
    assert logs[0]["action"] == "place"


async def test_config_set_and_get(db):
    await db.set_config("hedge_ratio", "0.90")
    val = await db.get_config("hedge_ratio")
    assert val == "0.90"

    await db.set_config("hedge_ratio", "0.85")
    val = await db.get_config("hedge_ratio")
    assert val == "0.85"


async def test_get_fill_stats(db):
    await db.insert_fill(
        timestamp=1000.0, exchange="hyperliquid", symbol="ARB",
        side="sell", size=100.0, price=1.05, fee=0.015,
        fee_currency="USDC", liquidity="maker", realized_pnl=0.0, order_id="o1",
    )
    await db.insert_fill(
        timestamp=1001.0, exchange="hyperliquid", symbol="ARB",
        side="buy", size=50.0, price=1.04, fee=0.045,
        fee_currency="USDC", liquidity="taker", realized_pnl=0.5, order_id="o2",
    )
    stats = await db.get_fill_stats()
    assert stats["maker_count"] == 1
    assert stats["taker_count"] == 1
    assert stats["maker_volume"] == 100.0
    assert stats["taker_volume"] == 50.0


async def test_insert_and_get_grid_order(db):
    await db.insert_grid_order(
        cloid="hb-r1-l5-1", side="sell", target_price=2800.0,
        size=0.001, placed_at=1000.0,
    )
    rows = await db.get_active_grid_orders()
    assert len(rows) == 1
    assert rows[0]["cloid"] == "hb-r1-l5-1"


async def test_mark_grid_order_cancelled(db):
    await db.insert_grid_order(
        cloid="hb-r1-l1-1", side="buy", target_price=3010.0,
        size=0.001, placed_at=1000.0,
    )
    await db.mark_grid_order_cancelled("hb-r1-l1-1", 1010.0)
    active = await db.get_active_grid_orders()
    assert len(active) == 0


async def test_mark_grid_order_filled(db):
    """Insert grid order, mark as filled with fill_id, assert no longer active."""
    await db.insert_fill(
        timestamp=1000.0, exchange="dydx", symbol="ETH-USD",
        side="sell", size=0.001, price=2800.0, fee=0.0, fee_currency="USDC",
        liquidity="maker", realized_pnl=0.0, order_id="hb-r1-l5-1",
    )
    fills = await db.get_fills()
    fill_id = fills[0]["id"]
    await db.insert_grid_order(
        cloid="hb-r1-l5-1", side="sell", target_price=2800.0,
        size=0.001, placed_at=1000.0,
    )
    await db.mark_grid_order_filled("hb-r1-l5-1", fill_id)
    active = await db.get_active_grid_orders()
    assert len(active) == 0


async def test_insert_and_get_active_operation(db):
    op_id = await db.insert_operation(
        started_at=1000.0, status="starting",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0,
        baseline_collateral=130.0,
    )
    active = await db.get_active_operation()
    assert active is not None
    assert active["id"] == op_id
    assert active["status"] == "starting"


async def test_close_operation(db):
    op_id = await db.insert_operation(
        started_at=1000.0, status="active",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0,
        baseline_collateral=130.0,
    )
    await db.close_operation(op_id, ended_at=2000.0, final_net_pnl=5.50, close_reason="user")
    active = await db.get_active_operation()
    assert active is None
    history = await db.get_operations(limit=10)
    assert len(history) == 1
    assert history[0]["status"] == "closed"
    assert history[0]["final_net_pnl"] == 5.50


async def test_operation_id_fk_in_fills(db):
    """Fills should accept operation_id and surface it on read."""
    op_id = await db.insert_operation(
        started_at=1000.0, status="active",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0,
        baseline_collateral=130.0,
    )
    fill_id = await db.insert_fill(
        timestamp=1500.0, exchange="dydx", symbol="ETH-USD",
        side="sell", size=0.001, price=3000.0, fee=0.0003, fee_currency="USDC",
        liquidity="maker", realized_pnl=0.0, order_id="cl-1",
        operation_id=op_id,
    )
    rows = await db.get_fills()
    assert rows[0]["operation_id"] == op_id


async def test_operation_accumulators_update(db):
    op_id = await db.insert_operation(
        started_at=1000.0, status="active",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0,
        baseline_collateral=130.0,
    )
    await db.add_to_operation_accumulator(op_id, "perp_fees_paid", 0.5)
    await db.add_to_operation_accumulator(op_id, "perp_fees_paid", 0.3)
    await db.add_to_operation_accumulator(op_id, "lp_fees_earned", 2.10)
    op = await db.get_operation(op_id)
    assert abs(op["perp_fees_paid"] - 0.8) < 1e-9
    assert abs(op["lp_fees_earned"] - 2.10) < 1e-9
