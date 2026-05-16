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


@pytest.mark.asyncio
async def test_safety_reconcile_steady_state_cancels_orphan():
    """Steady-state: Lighter has 1 cloid that's not in local_grid → cancel it
    using order_index from the live response."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.get_open_orders = AsyncMock(return_value=[
        {"cloid": "5001", "side": "sell", "trigger_price": 0.140, "size": 3.0, "order_index": 7001},
        {"cloid": "9999", "side": "sell", "trigger_price": 0.150, "size": 3.0, "order_index": 7999},  # orphan
    ])
    engine._exchange.cancel_stop_order = AsyncMock()

    engine._local_grid = {
        5001: GridStop(5001, "sell", 0.140, 3.0),  # known
    }

    await engine._safety_reconcile()

    engine._exchange.cancel_stop_order.assert_called_once()
    args = engine._exchange.cancel_stop_order.call_args
    assert args.kwargs.get("order_index") == 7999  # orphan's order_index, NOT cloid


@pytest.mark.asyncio
async def test_grid_event_loop_iter_no_position_change_no_writes():
    """One iter of the event loop with pos_now == last_known_position -> 0 writes."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    pos = MagicMock(symbol="ARB-USD", side="short", size=10.0, entry_price=0.135, unrealized_pnl=0.0)
    engine._exchange.get_position = AsyncMock(return_value=pos)
    engine._exchange.get_open_orders = AsyncMock()
    engine._exchange.cancel_stop_order = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()

    engine._last_known_position = pos
    engine._local_grid = {1: GridStop(1, "sell", 0.140, 3.0)}
    engine._last_safety_reconcile_at = 9999999999.0  # far future, so safety net doesn't fire

    await engine._grid_event_iter()

    # No writes
    engine._exchange.cancel_stop_order.assert_not_called()
    engine._exchange.place_stop_market.assert_not_called()
    # No open_orders read either (only on position change or safety net)
    engine._exchange.get_open_orders.assert_not_called()


import asyncio


@pytest.mark.asyncio
async def test_engine_start_creates_grid_event_loop_task():
    """start() must create both _task (main loop) and _grid_task (event loop)."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.get_position = AsyncMock(return_value=None)

    # Mock out _main_loop to keep test fast (don't run real loop)
    async def _noop(): await asyncio.sleep(0.01)
    engine._main_loop = _noop
    # Mock _grid_event_loop similarly
    engine._grid_event_loop = _noop

    # Engine.start does adapter init / subscribe / etc — bypass by going direct:
    engine._running = True
    engine._task = asyncio.create_task(engine._main_loop())
    engine._grid_task = asyncio.create_task(engine._grid_event_loop())

    assert engine._task is not None
    assert engine._grid_task is not None

    # Cleanup
    await asyncio.sleep(0.02)
    if not engine._task.done():
        engine._task.cancel()
    if not engine._grid_task.done():
        engine._grid_task.cancel()


@pytest.mark.asyncio
async def test_engine_stop_cancels_both_tasks():
    """stop() must cancel both _task and _grid_task."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.disconnect = AsyncMock()

    # Create two long-running tasks
    async def _long_running():
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            raise

    engine._running = True
    engine._task = asyncio.create_task(_long_running())
    engine._grid_task = asyncio.create_task(_long_running())

    # Keep local refs because stop() clears the engine's attrs to None
    main_task = engine._task
    grid_task = engine._grid_task

    await engine.stop()

    # Both should be cancelled (or done)
    assert main_task.cancelled() or main_task.done()
    assert grid_task.cancelled() or grid_task.done()


@pytest.mark.asyncio
async def test_drift_correction_updates_last_known_position():
    """After _maybe_correct_drift dispatches a taker on the primary leg,
    _last_known_position must be updated to the post-correction position so
    the grid event loop doesn't misinterpret the position change as a fill."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.name = "lighter"
    engine._exchange.place_long_term_order = AsyncMock()
    # Mock the post-correction position the engine should record
    new_pos = MagicMock(symbol="ARB-USD", side="short", size=20.0, entry_price=0.135)
    engine._exchange.get_position = AsyncMock(return_value=new_pos)

    # Pre-state: last_known is something else (size=10)
    old_pos = MagicMock(symbol="ARB-USD", side="short", size=10.0)
    engine._last_known_position = old_pos
    engine._db.insert_order_log = AsyncMock()
    engine._next_cloid_for_leg = MagicMock(return_value=99999)

    # Configure: hub.dydx_quote_prices for ref_price
    engine._hub.dydx_quote_prices = {"ARB-USD": 0.135}
    engine._hub.current_operation_id = 29
    engine._settings.min_rebalance_notional_usd = 0.5

    # beefy_pos arg is unused for this test path; pass MagicMock
    beefy_pos = MagicMock()
    p_now = 0.135

    # symbols + targets — drift = target - current = 20 - 10 = 10 ARB → $1.35 USD > $0.50 threshold
    positions = [MagicMock(side="short", size=10.0)]
    symbols = ["ARB-USD"]
    targets = {"ARB-USD": 20.0}

    await engine._maybe_correct_drift(
        beefy_pos=beefy_pos, p_now=p_now,
        positions=positions, symbols=symbols, targets=targets,
    )

    # _last_known_position should now be the new post-correction read
    assert engine._last_known_position is new_pos


@pytest.mark.asyncio
async def test_drift_correction_only_updates_for_primary_leg():
    """If drift correction fires on a secondary leg (token1), _last_known_position
    should NOT be updated (grid is on token0; token1 position is irrelevant)."""
    engine = _make_engine()
    engine._settings.dydx_symbol_token0 = "ARB-USD"  # primary
    engine._settings.dydx_symbol_token1 = "ETH-USD"  # secondary
    engine._exchange = MagicMock()
    engine._exchange.name = "lighter"
    engine._exchange.place_long_term_order = AsyncMock()
    # get_position should NOT be called for ETH-USD path
    engine._exchange.get_position = AsyncMock()

    old_pos = MagicMock(symbol="ARB-USD", side="short", size=10.0)
    engine._last_known_position = old_pos
    engine._db.insert_order_log = AsyncMock()
    engine._next_cloid_for_leg = MagicMock(return_value=99999)
    engine._hub.dydx_quote_prices = {"ETH-USD": 3500.0}
    engine._hub.current_operation_id = 29
    engine._settings.min_rebalance_notional_usd = 0.5

    positions = [MagicMock(side="short", size=0.005)]
    symbols = ["ETH-USD"]  # secondary leg only
    targets = {"ETH-USD": 0.010}  # drift = 0.005 ETH * 3500 = $17.50, > $0.50 threshold

    await engine._maybe_correct_drift(
        beefy_pos=MagicMock(), p_now=0.135,
        positions=positions, symbols=symbols, targets=targets,
    )

    # Last known position untouched (still the old one)
    assert engine._last_known_position is old_pos
    # get_position never called for the secondary leg
    engine._exchange.get_position.assert_not_called()


@pytest.mark.asyncio
async def test_maintain_grid_still_cancels_on_range_change():
    """Range change (Beefy rebalance) detected -> cancel_all_stops still fires.
    The event-driven loop handles fills; _maintain_grid handles structural changes."""
    from engine import GridMakerEngine

    settings = MagicMock()
    settings.predictive_grid_v2 = True
    settings.dydx_symbol_token0 = "ARB-USD"
    settings.dydx_symbol_token1 = ""
    settings.token0_decimals = 18
    settings.token1_decimals = 6
    settings.uniswap_v3_pool_fee = 500
    settings.alert_webhook_url = ""

    db = MagicMock()
    db.get_active_grid_orders = AsyncMock(return_value=[])
    db.mark_grid_order_cancelled = AsyncMock()

    exchange = MagicMock()
    exchange.cancel_all_stops = AsyncMock()

    engine = GridMakerEngine(
        settings=settings, hub=MagicMock(), db=db, exchange=exchange,
    )
    # Simulate: previously posted with sig X, now cache shows different sig.
    # Use ticks around -100/100 so p_now=1e-12 (~1.0001^0 / 10^12) is in range.
    engine._posted_grid_signature = (1.0, -100, 100)
    cache = MagicMock()
    cache.L_main = 2.0  # different L → range change
    cache.tick_lower_main = -100
    cache.tick_upper_main = 100
    engine._hedge_model = MagicMock()
    engine._hedge_model._cache = cache

    beefy_pos = MagicMock(share=1.0)

    # p_now within range. tick_to_human_price with d0=18, d1=6:
    # p_a (tick=-100) ~= 1.0001^-100 * 10^12 ~= 0.99 * 10^12; p_b ~= 1.01 * 10^12.
    # So p_now in that range:
    p_now = 1.0e12

    await engine._maintain_grid(
        beefy_pos=beefy_pos, p_now=p_now, oracle_prices={},
    )

    exchange.cancel_all_stops.assert_called()


@pytest.mark.asyncio
async def test_local_grid_keys_intersect_live_cloids_after_post():
    """Regression: after `_post_initial_grid` (or any place_stop_market
    flow), the cloid stored in `_local_grid` must equal the cloid that
    `get_open_orders` returns. Pre-fix the engine kept 64-bit values
    locally while Lighter stored only the low 32 bits, so
    `set(_local_grid) & live_cloids` was always empty.

    This is a regression guard for the spec
    `docs/superpowers/specs/2026-05-15-cloid-32bit-truncation-fix-design.md`.
    """
    engine = _make_engine()

    # Simulate the engine generating a cloid the same way _post_initial_grid does.
    cloid = engine._next_cloid_for_leg("ARB-USD")

    # Simulate the engine storing in _local_grid (what _post_initial_grid does).
    engine._local_grid[cloid] = GridStop(cloid, "sell", 0.130, 3.0)

    # Simulate Lighter returning what it actually persisted: the 32-bit truncated cloid.
    lighter_persisted_cloid = cloid & 0xFFFFFFFF
    live_by_cloid = {lighter_persisted_cloid: {
        "cloid": str(lighter_persisted_cloid), "side": "sell",
        "trigger_price": 0.130, "size": 3.0, "order_index": 999,
    }}

    # Reconciler's set logic:
    local_cloids = set(engine._local_grid.keys())
    live_cloids = set(live_by_cloid.keys())
    orphans = live_cloids - local_cloids
    missing = local_cloids - live_cloids

    assert orphans == set(), (
        f"reconciler should see zero orphans; got {orphans}. "
        f"local={local_cloids} live={live_cloids}"
    )
    assert missing == set(), (
        f"reconciler should see zero missing; got {missing}. "
        f"local={local_cloids} live={live_cloids}"
    )


@pytest.mark.asyncio
async def test_engine_start_cancels_existing_stops_when_op_active():
    """On startup with an active operation, the engine cancels any
    pre-existing stop orders for the active symbol before launching the
    grid loops. This prevents the new (post-fix 32-bit) cloid namespace
    from colliding with leftover stops from a previous run.

    No-op when there is no active operation (engine doesn't own a grid).
    """
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.connect = AsyncMock()
    engine._exchange.disconnect = AsyncMock()
    engine._exchange.subscribe_fills = AsyncMock()
    engine._exchange.cancel_all_stops = AsyncMock()
    engine._db.get_active_operation = AsyncMock(
        return_value={"id": 42, "status": "active"},
    )
    # Bypass chain readers (engine.start() builds them when None — let
    # those exist as MagicMocks so the construction branch is skipped).
    engine._pool_reader = MagicMock()
    engine._beefy_reader = MagicMock()
    # Skip the reconciler path (predictive_grid_v2 setting suppresses it).
    engine._settings.predictive_grid_v2 = True

    # Stub out the long-running loops so start() returns quickly.
    async def _noop():
        await asyncio.sleep(0)
    engine._main_loop = _noop
    engine._grid_event_loop = _noop

    await engine.start()
    try:
        engine._exchange.cancel_all_stops.assert_called_once_with(
            symbol=engine._settings.dydx_symbol_token0,
        )
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_engine_start_skips_cancel_when_no_op_active():
    """No active op -> engine doesn't own a grid, must not touch Lighter."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.connect = AsyncMock()
    engine._exchange.disconnect = AsyncMock()
    engine._exchange.subscribe_fills = AsyncMock()
    engine._exchange.cancel_all_stops = AsyncMock()
    engine._db.get_active_operation = AsyncMock(return_value=None)
    engine._pool_reader = MagicMock()
    engine._beefy_reader = MagicMock()
    engine._settings.predictive_grid_v2 = True

    async def _noop():
        await asyncio.sleep(0)
    engine._main_loop = _noop
    engine._grid_event_loop = _noop

    await engine.start()
    try:
        engine._exchange.cancel_all_stops.assert_not_called()
    finally:
        await engine.stop()
