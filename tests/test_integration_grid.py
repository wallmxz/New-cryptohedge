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
    """End-to-end: engine starts, reads chain, places grid, handles fill, updates state."""
    from db import Database
    from engine import GridMakerEngine
    from exchanges.base import Fill, Order

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

    placed_orders = []

    async def fake_batch_place(specs):
        result = []
        for s in specs:
            placed_orders.append(s)
            result.append(Order(
                order_id=str(s["cloid_int"]),
                symbol=s["symbol"], side=s["side"], size=s["size"],
                price=s["price"], status="open",
            ))
        return result

    exchange = MagicMock()
    exchange.name = "dydx"
    # min_notional is in token0 display units (e.g., WETH); engine multiplies
    # by p_now to obtain USD. 0.001 WETH * $3000 = $3 min order USD.
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(
        tick_size=0.1, step_size=0.001, min_notional=0.001,
    ))
    # Existing short approximately matches the target at p_now, so exposure_pct
    # stays under threshold_aggressive and the engine takes the in-range
    # grid-placement path (rather than the taker fallback).
    # target_short_at_now = compute_x(L=5.597, p=3000, p_b=3300) ~= 0.00476.
    exchange.get_position = AsyncMock(return_value=MagicMock(
        symbol="ETH-USD", side="short", size=0.00476,
        entry_price=3000.0, unrealized_pnl=0.0,
    ))
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.batch_place = AsyncMock(side_effect=fake_batch_place)
    exchange.batch_cancel = AsyncMock(return_value=0)
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])
    exchange.subscribe_fills = AsyncMock()
    # Safety: even if aggressive path were ever taken, this is awaitable.
    exchange.place_long_term_order = AsyncMock()

    pool_reader = MagicMock()
    pool_reader.read_price = AsyncMock(return_value=3000.0)
    beefy_reader = MagicMock()
    # Real ETH/USDC ticks for [$2700, $3300] with decimals0=18, decimals1=6.
    # See module docstring for derivation.
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.5, amount1=1500.0, share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
    )
    await engine._iterate()

    # Grid was placed (in-range path executed)
    assert len(placed_orders) > 0, "expected grid orders to be placed in-range"
    # State was updated
    assert state.range_lower > 0
    assert state.range_upper > state.range_lower
    assert state.liquidity_l > 0
    assert state.pool_value_usd > 0
    assert not state.out_of_range

    # DB persisted the placed orders
    active = await db.get_active_grid_orders()
    assert len(active) == len(placed_orders)

    # Simulate a fill on one of the orders
    cloid = placed_orders[0]["cloid_int"]
    fill = Fill(
        fill_id="f1", order_id=str(cloid), symbol="ETH-USD",
        side=placed_orders[0]["side"], size=placed_orders[0]["size"],
        price=placed_orders[0]["price"], fee=0.0001, fee_currency="USDC",
        liquidity="maker", realized_pnl=0.0, timestamp=1000.0,
    )
    await engine._on_fill(fill)
    assert state.total_maker_fills == 1
    assert state.total_maker_volume == pytest.approx(fill.size)
    assert state.total_fees_paid == pytest.approx(fill.fee)

    # The corresponding grid order should now be marked as filled
    active_after = await db.get_active_grid_orders()
    assert len(active_after) == len(placed_orders) - 1

    await db.close()
