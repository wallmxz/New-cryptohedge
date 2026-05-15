import pytest
from unittest.mock import MagicMock, AsyncMock
from engine import GridMakerEngine
from engine.grid_state import GridStop


def _make_engine():
    settings = MagicMock()
    settings.dydx_symbol_token0 = "ARB-USD"
    settings.dydx_symbol_token1 = "ETH-USD"
    return GridMakerEngine(
        settings=settings, hub=MagicMock(), db=MagicMock(), exchange=None,
    )


def test_engine_has_event_driven_state_vars_init_empty():
    engine = _make_engine()
    assert engine._last_known_position is None
    assert engine._local_grid == {}
    assert engine._last_safety_reconcile_at == 0.0


@pytest.mark.asyncio
async def test_single_sell_fill_triggers_3_writes():
    """A sell fills → cancel lowest buy + post buy at fill trigger + post sell at top+step."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.cancel_stop_order = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
    engine._next_cloid_for_leg = MagicMock(side_effect=[9001, 9002])

    # Pre-populate local_grid with 4-stop micro grid (2 sells + 2 buys)
    # sells at 0.140, 0.142  | buys at 0.130, 0.128
    engine._local_grid = {
        100: GridStop(100, "sell", 0.140, 3.0),  # this one filled
        101: GridStop(101, "sell", 0.142, 3.0),  # top sell
        200: GridStop(200, "buy", 0.130, 3.0),
        201: GridStop(201, "buy", 0.128, 3.0),   # lowest buy
    }
    step = 0.002  # arbitrary fixed step for the test
    live_by_cloid = {
        101: {"cloid": "101", "side": "sell", "trigger_price": 0.142, "size": 3.0, "order_index": 5101},
        200: {"cloid": "200", "side": "buy", "trigger_price": 0.130, "size": 3.0, "order_index": 5200},
        201: {"cloid": "201", "side": "buy", "trigger_price": 0.128, "size": 3.0, "order_index": 5201},
    }

    await engine._apply_fills_to_grid(filled_cloids={100}, step=step, live_by_cloid=live_by_cloid)

    # Assertions
    assert engine._exchange.cancel_stop_order.call_count == 1
    cancel_call = engine._exchange.cancel_stop_order.call_args
    # lowest buy should be cancelled (cloid 201 → order_index 5201)
    assert cancel_call.kwargs.get("order_index") == 5201

    assert engine._exchange.place_stop_market.call_count == 2
    posts = engine._exchange.place_stop_market.call_args_list
    # First post: new buy at filled sell's trigger price (0.140)
    assert posts[0].kwargs["side"] == "buy"
    assert posts[0].kwargs["trigger_price"] == 0.140
    # Second post: new sell at top + step = 0.142 + 0.002 = 0.144
    assert posts[1].kwargs["side"] == "sell"
    assert abs(posts[1].kwargs["trigger_price"] - 0.144) < 1e-9

    # local_grid updated: removed cloid 100 (filled) and 201 (cancelled), added 9001 (new buy) and 9002 (new sell)
    assert 100 not in engine._local_grid
    assert 201 not in engine._local_grid
    assert 9001 in engine._local_grid
    assert engine._local_grid[9001].side == "buy"
    assert engine._local_grid[9001].trigger_price == 0.140
    assert 9002 in engine._local_grid
    assert engine._local_grid[9002].side == "sell"
    assert abs(engine._local_grid[9002].trigger_price - 0.144) < 1e-9


@pytest.mark.asyncio
async def test_local_grid_not_corrupted_when_post_fails():
    """If place_stop_market raises, the new cloid must NOT be inserted into _local_grid.
    Otherwise the bot mis-tracks state and the safety net masks the bug for up to 90s."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.cancel_stop_order = AsyncMock()  # cancel succeeds
    # Buy post succeeds, sell post FAILS
    engine._exchange.place_stop_market = AsyncMock(
        side_effect=[None, Exception("rate limit")],
    )
    engine._next_cloid_for_leg = MagicMock(side_effect=[9001, 9002])

    engine._local_grid = {
        100: GridStop(100, "sell", 0.140, 3.0),
        101: GridStop(101, "sell", 0.142, 3.0),
        200: GridStop(200, "buy", 0.130, 3.0),
        201: GridStop(201, "buy", 0.128, 3.0),
    }
    step = 0.002
    live_by_cloid = {
        101: {"cloid": "101", "side": "sell", "trigger_price": 0.142, "size": 3.0, "order_index": 5101},
        200: {"cloid": "200", "side": "buy", "trigger_price": 0.130, "size": 3.0, "order_index": 5200},
        201: {"cloid": "201", "side": "buy", "trigger_price": 0.128, "size": 3.0, "order_index": 5201},
    }

    await engine._apply_fills_to_grid(filled_cloids={100}, step=step, live_by_cloid=live_by_cloid)

    # Old filled sell was removed
    assert 100 not in engine._local_grid
    # Cancelled buy was removed (cancel succeeded)
    assert 201 not in engine._local_grid
    # New buy succeeded — should be in local_grid
    assert 9001 in engine._local_grid
    assert engine._local_grid[9001].side == "buy"
    # New sell FAILED — should NOT be in local_grid (phantom cloid prevention)
    assert 9002 not in engine._local_grid


@pytest.mark.asyncio
async def test_skip_fill_when_step_is_zero():
    """When step=0 (sparse grid path), don't post replacement at colliding price."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.cancel_stop_order = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
    engine._next_cloid_for_leg = MagicMock(side_effect=[9001, 9002])

    engine._local_grid = {
        100: GridStop(100, "sell", 0.140, 3.0),
        101: GridStop(101, "sell", 0.142, 3.0),
        200: GridStop(200, "buy", 0.130, 3.0),
        201: GridStop(201, "buy", 0.128, 3.0),
    }
    live_by_cloid = {
        101: {"cloid": "101", "side": "sell", "trigger_price": 0.142, "size": 3.0, "order_index": 5101},
        200: {"cloid": "200", "side": "buy", "trigger_price": 0.130, "size": 3.0, "order_index": 5200},
        201: {"cloid": "201", "side": "buy", "trigger_price": 0.128, "size": 3.0, "order_index": 5201},
    }

    await engine._apply_fills_to_grid(filled_cloids={100}, step=0.0, live_by_cloid=live_by_cloid)

    # No writes when step is 0 — safety net will fix later
    engine._exchange.cancel_stop_order.assert_not_called()
    engine._exchange.place_stop_market.assert_not_called()
    # local_grid unchanged
    assert 100 in engine._local_grid


@pytest.mark.asyncio
async def test_single_buy_fill_triggers_3_writes():
    """A buy fills → cancel highest sell + post sell at fill trigger + post buy at bottom-step."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.cancel_stop_order = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
    engine._next_cloid_for_leg = MagicMock(side_effect=[9101, 9102])

    engine._local_grid = {
        100: GridStop(100, "sell", 0.140, 3.0),
        101: GridStop(101, "sell", 0.142, 3.0),  # highest sell — gets cancelled
        200: GridStop(200, "buy", 0.130, 3.0),   # this one filled
        201: GridStop(201, "buy", 0.128, 3.0),   # bottom buy
    }
    step = 0.002
    live_by_cloid = {
        100: {"cloid": "100", "side": "sell", "trigger_price": 0.140, "size": 3.0, "order_index": 5100},
        101: {"cloid": "101", "side": "sell", "trigger_price": 0.142, "size": 3.0, "order_index": 5101},
        201: {"cloid": "201", "side": "buy", "trigger_price": 0.128, "size": 3.0, "order_index": 5201},
    }

    await engine._apply_fills_to_grid(filled_cloids={200}, step=step, live_by_cloid=live_by_cloid)

    assert engine._exchange.cancel_stop_order.call_count == 1
    posts = engine._exchange.place_stop_market.call_args_list
    assert engine._exchange.place_stop_market.call_count == 2
    # First post: new sell at filled buy's trigger (0.130)
    assert posts[0].kwargs["side"] == "sell"
    assert posts[0].kwargs["trigger_price"] == 0.130
    # Second post: new buy at bottom - step = 0.128 - 0.002 = 0.126
    assert posts[1].kwargs["side"] == "buy"
    assert abs(posts[1].kwargs["trigger_price"] - 0.126) < 1e-9

    assert 200 not in engine._local_grid
    assert 101 not in engine._local_grid
    assert engine._local_grid[9101].side == "sell"
    assert engine._local_grid[9101].trigger_price == 0.130
    assert engine._local_grid[9102].side == "buy"
    assert abs(engine._local_grid[9102].trigger_price - 0.126) < 1e-9


@pytest.mark.asyncio
async def test_two_sells_filled_same_iter_processed_in_order():
    """Two sells filled simultaneously → 6 writes (2 cancels + 4 posts).
    Closest-to-market processed first."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.cancel_stop_order = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
    cloid_seq = iter(range(9201, 9210))
    engine._next_cloid_for_leg = MagicMock(side_effect=lambda sym: next(cloid_seq))

    # 4-stop pre-grid; 2 lowest sells fill
    engine._local_grid = {
        100: GridStop(100, "sell", 0.140, 3.0),  # filled (closest to market)
        101: GridStop(101, "sell", 0.142, 3.0),  # filled
        102: GridStop(102, "sell", 0.144, 3.0),  # top sell BEFORE fills
        200: GridStop(200, "buy", 0.130, 3.0),
        201: GridStop(201, "buy", 0.128, 3.0),  # 2nd lowest
        202: GridStop(202, "buy", 0.126, 3.0),  # lowest buy (farthest from market)
    }
    step = 0.002
    live_by_cloid = {
        102: {"cloid": "102", "side": "sell", "trigger_price": 0.144, "size": 3.0, "order_index": 5102},
        200: {"cloid": "200", "side": "buy", "trigger_price": 0.130, "size": 3.0, "order_index": 5200},
        201: {"cloid": "201", "side": "buy", "trigger_price": 0.128, "size": 3.0, "order_index": 5201},
        202: {"cloid": "202", "side": "buy", "trigger_price": 0.126, "size": 3.0, "order_index": 5202},
    }

    await engine._apply_fills_to_grid(filled_cloids={100, 101}, step=step, live_by_cloid=live_by_cloid)

    # 2 cancels + 4 posts = 6 writes
    assert engine._exchange.cancel_stop_order.call_count == 2
    assert engine._exchange.place_stop_market.call_count == 4

    # First fill processed (cloid 100, lowest sell at 0.140 = closest to market):
    #  - cancel lowest buy = 202 at 0.126 (farthest from market below)
    #  - post buy at 0.140
    #  - post sell at top (102=0.144) + 0.002 = 0.146
    # Second fill processed (cloid 101, sell at 0.142):
    #  - lowest_buy now 201 at 0.128 (since 202 was cancelled); cancel it
    #  - post buy at 0.142
    #  - post sell at top (now 9202 at 0.146) + 0.002 = 0.148

    cancels = engine._exchange.cancel_stop_order.call_args_list
    posts = engine._exchange.place_stop_market.call_args_list

    cancel_order_indexes = [c.kwargs.get("order_index") for c in cancels]
    # cloid 202 → order_index 5202; cloid 201 → order_index 5201
    assert cancel_order_indexes == [5202, 5201]

    post_prices = [(p.kwargs["side"], p.kwargs["trigger_price"]) for p in posts]
    assert post_prices[0] == ("buy", 0.140)
    assert post_prices[1] == ("sell", pytest.approx(0.146))
    assert post_prices[2] == ("buy", 0.142)
    assert post_prices[3] == ("sell", pytest.approx(0.148))


@pytest.mark.asyncio
async def test_safety_reconcile_bootstrap_populates_local_grid_from_lighter():
    """First call: local_grid empty → query Lighter open_orders + DB lookup,
    populate local_grid. NO cancellations."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    # Lighter returns 3 live orders
    engine._exchange.get_open_orders = AsyncMock(return_value=[
        {"cloid": "5001", "side": "sell", "trigger_price": 0.140, "size": 3.0, "order_index": 70001},
        {"cloid": "5002", "side": "sell", "trigger_price": 0.142, "size": 3.0, "order_index": 70002},
        {"cloid": "6001", "side": "buy", "trigger_price": 0.130, "size": 3.0, "order_index": 70003},
    ])
    engine._exchange.cancel_stop_order = AsyncMock()  # should NOT be called

    # local_grid empty (post-restart state)
    engine._local_grid = {}

    await engine._safety_reconcile()

    # local_grid now has 3 entries matching Lighter
    assert len(engine._local_grid) == 3
    assert 5001 in engine._local_grid
    assert engine._local_grid[5001].side == "sell"
    assert engine._local_grid[5001].trigger_price == 0.140
    assert 5002 in engine._local_grid
    assert 6001 in engine._local_grid
    assert engine._local_grid[6001].side == "buy"

    # No cancel calls during bootstrap
    engine._exchange.cancel_stop_order.assert_not_called()


@pytest.mark.asyncio
async def test_cancel_uses_order_index_not_cloid_int():
    """Regression: cancel_stop_order was being called with cloid_int=, but the
    real adapter signature requires order_index=. Verify we now pass order_index
    looked up from live_by_cloid map."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.cancel_stop_order = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
    engine._next_cloid_for_leg = MagicMock(side_effect=[9001, 9002])

    engine._local_grid = {
        100: GridStop(100, "sell", 0.140, 3.0),  # filled
        101: GridStop(101, "sell", 0.142, 3.0),
        200: GridStop(200, "buy", 0.130, 3.0),
        201: GridStop(201, "buy", 0.128, 3.0),  # to cancel
    }
    # live_by_cloid contains all stops EXCEPT the filled one. Each has a synthetic order_index.
    live_by_cloid = {
        101: {"cloid": "101", "side": "sell", "trigger_price": 0.142, "size": 3.0, "order_index": 5101},
        200: {"cloid": "200", "side": "buy", "trigger_price": 0.130, "size": 3.0, "order_index": 5200},
        201: {"cloid": "201", "side": "buy", "trigger_price": 0.128, "size": 3.0, "order_index": 5201},
    }

    await engine._apply_fills_to_grid(
        filled_cloids={100}, step=0.002, live_by_cloid=live_by_cloid,
    )

    # Cancel was called with order_index=5201 (NOT cloid_int=201)
    assert engine._exchange.cancel_stop_order.call_count == 1
    call = engine._exchange.cancel_stop_order.call_args
    assert call.kwargs.get("order_index") == 5201
    assert "cloid_int" not in call.kwargs  # explicit: don't use cloid_int


@pytest.mark.asyncio
async def test_cancel_skipped_when_cloid_not_in_live_but_local_grid_pops_anyway():
    """If opp.cloid isn't in live_by_cloid (cancelled by external means or race),
    skip the cancel call but still remove from local_grid."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.cancel_stop_order = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
    engine._next_cloid_for_leg = MagicMock(side_effect=[9001, 9002])

    engine._local_grid = {
        100: GridStop(100, "sell", 0.140, 3.0),
        101: GridStop(101, "sell", 0.142, 3.0),
        200: GridStop(200, "buy", 0.130, 3.0),
        201: GridStop(201, "buy", 0.128, 3.0),  # NOT in live_by_cloid
    }
    live_by_cloid = {
        101: {"cloid": "101", "side": "sell", "trigger_price": 0.142, "size": 3.0, "order_index": 5101},
        200: {"cloid": "200", "side": "buy", "trigger_price": 0.130, "size": 3.0, "order_index": 5200},
        # 201 missing
    }

    await engine._apply_fills_to_grid(
        filled_cloids={100}, step=0.002, live_by_cloid=live_by_cloid,
    )

    # No cancel call (skipped)
    engine._exchange.cancel_stop_order.assert_not_called()
    # But 201 still removed from local_grid
    assert 201 not in engine._local_grid
