"""Tests for engine's predictive iter logic + per-leg fire."""
from unittest.mock import AsyncMock, MagicMock
import pytest

from state import StateHub


def _engine_with_book(eth_bid=2300.0, eth_ask=2301.0, arb_bid=0.130, arb_ask=0.131):
    """Build an engine with the lighter adapter's _ws_book_top pre-populated
    for ETH (mid 0) and ARB (mid 50)."""
    from engine import GridMakerEngine

    state = StateHub(hedge_ratio=0.98)
    state.operation_state = "active"
    state.current_operation_id = 1

    settings = MagicMock()
    settings.dydx_symbol_token0 = "ETH-USD"
    settings.dydx_symbol_token1 = "ARB-USD"
    settings.threshold_aggressive = 0.01
    settings.max_open_orders = 200
    settings.pool_token0_symbol = "ETH"
    settings.pool_token1_symbol = "ARB"
    settings.alert_webhook_url = ""
    settings.dydx_symbol = "ETH-USD"
    settings.min_rebalance_notional_usd = 0.50

    db = MagicMock()
    db.insert_order_log = AsyncMock()
    exchange = MagicMock()
    exchange.name = "lighter"
    exchange.subscribe_funding = MagicMock()
    exchange.place_long_term_order = AsyncMock()
    exchange.get_market_meta = AsyncMock()
    exchange._ws_book_top = {
        0: {"best_bid": eth_bid, "best_ask": eth_ask, "ts": 0},
        50: {"best_bid": arb_bid, "best_ask": arb_ask, "ts": 0},
    }

    pool = MagicMock(); beefy = MagicMock()
    eng = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=18,
    )
    eng._token0_mid = 0
    eng._token1_mid = 50
    return eng, exchange


@pytest.mark.asyncio
async def test_fire_predictive_leg_sells_at_bid():
    """delta > 0 (need to short more) → SELL at the best bid."""
    eng, exchange = _engine_with_book(eth_bid=2300.0, eth_ask=2301.0)
    await eng._fire_predictive_leg("ETH-USD", delta=0.001)
    exchange.place_long_term_order.assert_awaited_once()
    call = exchange.place_long_term_order.await_args
    assert call.kwargs["side"] == "sell"
    assert call.kwargs["price"] == 2300.0


@pytest.mark.asyncio
async def test_fire_predictive_leg_buys_at_ask():
    """delta < 0 (close some short) → BUY at the best ask."""
    eng, exchange = _engine_with_book(eth_bid=2300.0, eth_ask=2301.0)
    await eng._fire_predictive_leg("ETH-USD", delta=-0.001)
    exchange.place_long_term_order.assert_awaited_once()
    call = exchange.place_long_term_order.await_args
    assert call.kwargs["side"] == "buy"
    assert call.kwargs["price"] == 2301.0


@pytest.mark.asyncio
async def test_fire_predictive_leg_skips_below_dollar_floor():
    """Sub-$0.50 leg notional → no fire, no exception."""
    eng, exchange = _engine_with_book(eth_bid=2300.0, eth_ask=2301.0)
    await eng._fire_predictive_leg("ETH-USD", delta=0.0001)
    exchange.place_long_term_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_fire_predictive_leg_empty_book_raises_predictive_unavailable():
    """No book entry for symbol's market_id → raises PredictiveUnavailable."""
    from engine import PredictiveUnavailable
    eng, exchange = _engine_with_book()
    exchange._ws_book_top = {}
    with pytest.raises(PredictiveUnavailable):
        await eng._fire_predictive_leg("ETH-USD", delta=0.001)


@pytest.mark.asyncio
async def test_fire_predictive_leg_zero_delta_no_fire():
    eng, exchange = _engine_with_book()
    await eng._fire_predictive_leg("ETH-USD", delta=0.0)
    exchange.place_long_term_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_iterate_predictive_first_iter_snaps_no_fire():
    """First iter post-rebuild: _last_level_idx is None → snap, no fire."""
    from engine.predictive_grid import build_grid
    eng, exchange = _engine_with_book()

    grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=100.0, p0_usd=2300.0, p1_usd=0.13,
        min_leg_notional_usd=0.50,
    )
    eng._grid = grid
    eng._last_level_idx = None

    import math
    mid_p = (grid.p_a + grid.p_b) / 2
    sqrt_p_x96 = int(math.sqrt(mid_p) * 2**96)
    eng._pool_reader.read_slot0 = AsyncMock(return_value=(sqrt_p_x96, -78500))

    await eng._iterate_predictive()
    exchange.place_long_term_order.assert_not_awaited()
    assert eng._last_level_idx is not None


@pytest.mark.asyncio
async def test_iterate_predictive_level_change_fires_both_legs():
    """Level change: both legs are dispatched; floor set tiny so both fire.

    Recon (added 2026-05-09) ALSO runs every iter — to keep this test
    measuring only the per-level fire path, mock positions to match the
    NEW level's target so recon delta = 0 (silent skip).
    """
    from engine.predictive_grid import build_grid, find_level_idx
    eng, exchange = _engine_with_book(
        eth_bid=2300.0, eth_ask=2301.0, arb_bid=0.130, arb_ask=0.131,
    )
    # Lower floor so both legs (even small token1 delta) clear it.
    eng._settings.min_rebalance_notional_usd = 0.000001

    grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=100.0, p0_usd=2300.0, p1_usd=0.13,
        min_leg_notional_usd=0.50,
    )
    eng._grid = grid
    eng._last_level_idx = 0

    import math
    p_target = grid.p_b * 0.95
    sqrt_p_x96 = int(math.sqrt(p_target) * 2**96)
    eng._pool_reader.read_slot0 = AsyncMock(return_value=(sqrt_p_x96, -76100))

    # Mock positions to match NEW level's target so recon = 0.
    new_idx = find_level_idx(grid, p_target)
    target_t0 = grid.amount0_at[new_idx] * eng._hub.hedge_ratio
    target_t1 = grid.amount1_at[new_idx] * eng._hub.hedge_ratio
    pos_t0 = MagicMock(size=target_t0)
    pos_t1 = MagicMock(size=target_t1)
    async def fake_pos(symbol):
        return pos_t0 if symbol == "ETH-USD" else pos_t1
    eng._safe_get_position = fake_pos

    await eng._iterate_predictive()
    # Per-level: 2 fires. Recon: 0 (positions match target). Total = 2.
    assert exchange.place_long_term_order.await_count == 2


@pytest.mark.asyncio
async def test_iterate_predictive_no_level_change_no_fire():
    """Same level idx AND positions match grid target → no fire (incl. recon)."""
    from engine.predictive_grid import build_grid, find_level_idx
    eng, exchange = _engine_with_book()
    grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=100.0, p0_usd=2300.0, p1_usd=0.13,
        min_leg_notional_usd=0.50,
    )
    eng._grid = grid
    import math
    p_now = grid.p_levels[10] + (grid.p_levels[11] - grid.p_levels[10]) / 2
    expected_idx = find_level_idx(grid, p_now)
    eng._last_level_idx = expected_idx
    sqrt_p_x96 = int(math.sqrt(p_now) * 2**96)
    eng._pool_reader.read_slot0 = AsyncMock(return_value=(sqrt_p_x96, -78500))

    # Mock current positions to MATCH grid expected (so recon delta = 0).
    # Recon (added 2026-05-09) compares target = amount[idx] × hedge_ratio
    # vs actual; we mock _safe_get_position to return positions that match.
    target_t0 = grid.amount0_at[expected_idx] * eng._hub.hedge_ratio
    target_t1 = grid.amount1_at[expected_idx] * eng._hub.hedge_ratio
    pos_t0 = MagicMock(size=target_t0)
    pos_t1 = MagicMock(size=target_t1)
    async def fake_pos(symbol):
        return pos_t0 if symbol == "ETH-USD" else pos_t1
    eng._safe_get_position = fake_pos

    await eng._iterate_predictive()
    exchange.place_long_term_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_iterate_predictive_raises_when_slot0_fails():
    """Pool RPC failure → PredictiveUnavailable for fallback."""
    from engine import PredictiveUnavailable
    from engine.predictive_grid import build_grid
    eng, exchange = _engine_with_book()
    eng._grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=100.0, p0_usd=2300.0, p1_usd=0.13,
        min_leg_notional_usd=0.50,
    )
    eng._last_level_idx = 5
    eng._pool_reader.read_slot0 = AsyncMock(side_effect=RuntimeError("RPC down"))
    with pytest.raises(PredictiveUnavailable):
        await eng._iterate_predictive()


@pytest.mark.skip(reason=(
    "Deleted in T7 — engine no longer writes predictive_status; see "
    "hedge_model_status. T5 swapped predictive grid for HedgeModel."
))
@pytest.mark.asyncio
async def test_iterate_falls_back_to_reactive_when_predictive_unavailable():
    """When _iterate_predictive raises PredictiveUnavailable, _iterate runs
    the reactive _maybe_rebalance_leg path."""
    eng, exchange = _engine_with_book()
    eng._grid = None  # forces _iterate_predictive to raise

    # Use tick range that puts p_now=0.000375 in-range (same range as other
    # predictive tests: -81121/-76012 yields p_a~3e-4, p_b~5e-4).
    eng._pool_reader.read_price = AsyncMock(return_value=0.000375)
    eng._beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-81121, tick_upper=-76012,
        amount0=0.01, amount1=10.0, share=1.0, raw_balance=10**18,
    ))
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=10.0))
    exchange.get_oracle_prices = AsyncMock(return_value={"ETH-USD": 2300.0, "ARB-USD": 0.13})
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])
    eng._db.get_active_grid_orders = AsyncMock(return_value=[])
    eng._db.get_operation = AsyncMock(return_value=None)
    eng._db.add_to_operation_accumulator = AsyncMock()
    # _refresh_grid would normally run; stub it so no rebuild attempts
    eng._beefy_reader._strategy.functions.positionMain = MagicMock(
        return_value=MagicMock(
            call=AsyncMock(side_effect=RuntimeError("simulated")),
        )
    )

    # Spy on reactive
    rebalance_spy_called = []
    original_rebalance = eng._maybe_rebalance_leg
    async def spy(**kwargs):
        rebalance_spy_called.append(kwargs.get("symbol"))
        return await original_rebalance(**kwargs)
    eng._maybe_rebalance_leg = spy

    await eng._iterate()
    # At least one symbol got a reactive rebalance check
    assert len(rebalance_spy_called) >= 1
    assert eng._hub.predictive_status.startswith("fallback")


@pytest.mark.skip(reason=(
    "Predictive force-disabled 2026-05-09: model derives L from total "
    "strategy balances but Beefy CLM v2 has positionMain + positionAlt "
    "+ idle balances, so derived L is wrong by ~3x and grid amounts "
    "are completely off. Test asserts predictive runs cleanly + sets "
    "predictive_status='active', but engine now always falls back to "
    "reactive. Re-enable when predictive is redesigned to model the "
    "two-position structure."
))
@pytest.mark.asyncio
async def test_iterate_does_not_double_fire_predictive_and_reactive():
    """Predictive succeeds → reactive must NOT run (hard guard)."""
    from engine.predictive_grid import build_grid
    eng, exchange = _engine_with_book()
    eng._grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=100.0, p0_usd=2300.0, p1_usd=0.13,
        min_leg_notional_usd=0.50,
    )
    eng._last_level_idx = 5

    # Pool returns p in same range → no level change → predictive runs cleanly
    import math
    sqrt_p_x96 = int(math.sqrt(eng._grid.p_levels[5]) * 2**96)
    eng._pool_reader.read_slot0 = AsyncMock(return_value=(sqrt_p_x96, -78500))

    # Skip refresh
    eng._last_grid_check_at = __import__("time").monotonic()

    # Setup reactive path stubs to detect calls
    eng._beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-81121, tick_upper=-76012,
        amount0=0.01, amount1=100.0, share=1.0, raw_balance=10**18,
    ))
    eng._pool_reader.read_price = AsyncMock(return_value=0.000375)
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=10.0))
    exchange.get_oracle_prices = AsyncMock(return_value={"ETH-USD": 2300.0, "ARB-USD": 0.13})
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])
    eng._db.get_active_grid_orders = AsyncMock(return_value=[])
    eng._db.get_operation = AsyncMock(return_value=None)
    eng._db.add_to_operation_accumulator = AsyncMock()

    rebalance_spy_called = []
    original_rebalance = eng._maybe_rebalance_leg
    async def spy(**kwargs):
        rebalance_spy_called.append(kwargs.get("symbol"))
        return await original_rebalance(**kwargs)
    eng._maybe_rebalance_leg = spy

    await eng._iterate()
    # Predictive ran cleanly → reactive must NOT have been called
    assert rebalance_spy_called == []
    assert eng._hub.predictive_status == "active"


@pytest.mark.asyncio
async def test_iterate_predictive_recon_fires_when_position_diverges_from_target():
    """Recon (2026-05-09): even with no level change, if actual position
    differs from target = amount[level] × hedge_ratio by ≥ $0.50, fire
    corrective. Catches warmup baseline mismatch + reactive residuals."""
    from engine.predictive_grid import build_grid, find_level_idx
    eng, exchange = _engine_with_book(
        eth_bid=2300.0, eth_ask=2301.0, arb_bid=0.130, arb_ask=0.131,
    )
    grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=100.0, p0_usd=2300.0, p1_usd=0.13,
        min_leg_notional_usd=0.50,
    )
    eng._grid = grid
    import math
    p_now = grid.p_levels[10] + (grid.p_levels[11] - grid.p_levels[10]) / 2
    expected_idx = find_level_idx(grid, p_now)
    eng._last_level_idx = expected_idx
    sqrt_p_x96 = int(math.sqrt(p_now) * 2**96)
    eng._pool_reader.read_slot0 = AsyncMock(return_value=(sqrt_p_x96, -78500))

    # Position is OFF target. Target ARB ~ 100, actual = 200 → over-hedged
    # by 100 ARB ≈ $13. Recon must fire BUY to bring back to target.
    target_t0 = grid.amount0_at[expected_idx] * eng._hub.hedge_ratio
    target_t1 = grid.amount1_at[expected_idx] * eng._hub.hedge_ratio
    pos_t0 = MagicMock(size=target_t0)             # ETH matches
    pos_t1 = MagicMock(size=target_t1 + 100.0)     # ARB way over
    async def fake_pos(symbol):
        return pos_t0 if symbol == "ETH-USD" else pos_t1
    eng._safe_get_position = fake_pos

    await eng._iterate_predictive()
    # No level change → 0 per-level fires. ETH recon = 0 → 0 fires.
    # ARB recon = -100 → 1 fire BUY. Total = 1.
    assert exchange.place_long_term_order.await_count == 1
    call = exchange.place_long_term_order.await_args
    assert call.kwargs["symbol"] == "ARB-USD"
    assert call.kwargs["side"] == "buy"
    assert call.kwargs["price"] == 0.131  # ask (closing short via buy)


@pytest.mark.asyncio
async def test_iterate_predictive_recon_skips_when_drift_below_floor():
    """Sub-$0.50 recon drift → silent skip (within fire_predictive_leg gate)."""
    from engine.predictive_grid import build_grid, find_level_idx
    eng, exchange = _engine_with_book()
    grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=100.0, p0_usd=2300.0, p1_usd=0.13,
        min_leg_notional_usd=0.50,
    )
    eng._grid = grid
    import math
    p_now = grid.p_levels[10] + (grid.p_levels[11] - grid.p_levels[10]) / 2
    expected_idx = find_level_idx(grid, p_now)
    eng._last_level_idx = expected_idx
    sqrt_p_x96 = int(math.sqrt(p_now) * 2**96)
    eng._pool_reader.read_slot0 = AsyncMock(return_value=(sqrt_p_x96, -78500))

    # Position off by tiny amount: 0.0001 ETH × $2300 = $0.23 < $0.50 → skip
    target_t0 = grid.amount0_at[expected_idx] * eng._hub.hedge_ratio
    target_t1 = grid.amount1_at[expected_idx] * eng._hub.hedge_ratio
    pos_t0 = MagicMock(size=target_t0 + 0.0001)
    pos_t1 = MagicMock(size=target_t1 + 0.5)  # 0.5 ARB × $0.13 = $0.07 → skip
    async def fake_pos(symbol):
        return pos_t0 if symbol == "ETH-USD" else pos_t1
    eng._safe_get_position = fake_pos

    await eng._iterate_predictive()
    exchange.place_long_term_order.assert_not_awaited()

