"""Engine funding handler — wires LighterAdapter funding callback into
the active operation's funding_paid_token0/1 accumulator."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from state import StateHub


@pytest.mark.asyncio
async def test_engine_resolves_market_ids_at_init_for_dual_leg():
    """For a cross-pair (dual-leg) op, engine resolves token0_mid and
    token1_mid via exchange.get_market_meta during __init__. These are
    needed by _on_funding_payment to route per-leg."""
    from engine import GridMakerEngine

    state = StateHub(hedge_ratio=1.0)
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
    exchange = MagicMock()
    exchange.name = "lighter"
    # Two market metas — tests the per-leg resolution.
    def market_meta_for(symbol):
        m = MagicMock()
        m.market_index = 0 if symbol == "ETH-USD" else 50
        return m
    exchange.get_market_meta = AsyncMock(side_effect=lambda s: market_meta_for(s))
    exchange.subscribe_funding = MagicMock()

    pool = MagicMock(); beefy = MagicMock()
    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=18,
    )
    # __init__ doesn't await; the async resolve runs in a startup hook
    # the engine exposes as resolve_market_ids_for_funding.
    await engine.resolve_market_ids_for_funding()
    assert engine._token0_mid == 0
    assert engine._token1_mid == 50
    # And the callback was registered.
    exchange.subscribe_funding.assert_called_once()


import time
from engine.operation import Operation, OperationState


def _make_engine_with_funding_state(token0_mid=0, token1_mid=50):
    """Build an engine ready to test _on_funding_payment in isolation."""
    from engine import GridMakerEngine
    state = StateHub(hedge_ratio=1.0)
    state.operation_state = "active"
    state.current_operation_id = 42

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
    db.add_to_operation_accumulator = AsyncMock()
    db.get_operation = AsyncMock(return_value={
        "id": 42, "started_at": 1700000000.0, "status": "active",
        "baseline_eth_price": 2000.0, "baseline_pool_value_usd": 50.0,
        "baseline_amount0": 0.01, "baseline_amount1": 100.0,
        "baseline_collateral": 100.0,
    })

    exchange = MagicMock()
    exchange.name = "lighter"
    exchange.subscribe_funding = MagicMock()
    exchange.get_market_meta = AsyncMock()

    pool = MagicMock(); beefy = MagicMock()
    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=18,
    )
    engine._token0_mid = token0_mid
    engine._token1_mid = token1_mid
    return engine, db


@pytest.mark.asyncio
async def test_on_funding_payment_writes_token0_when_market_id_matches():
    """Funding entry for token0_mid -> writes funding_paid_token0."""
    engine, db = _make_engine_with_funding_state(token0_mid=0, token1_mid=50)
    entry = MagicMock(
        funding_id=1, market_id=0, timestamp=1700001000,
        change="0.10",  # user received +$0.10
    )
    await engine._on_funding_payment(entry)
    db.add_to_operation_accumulator.assert_awaited_once_with(
        42, "funding_paid_token0", -0.10,
    )


@pytest.mark.asyncio
async def test_on_funding_payment_writes_token1_when_market_id_matches():
    """Funding entry for token1_mid -> writes funding_paid_token1."""
    engine, db = _make_engine_with_funding_state(token0_mid=0, token1_mid=50)
    entry = MagicMock(
        funding_id=2, market_id=50, timestamp=1700001000,
        change="-0.25",  # user paid $0.25
    )
    await engine._on_funding_payment(entry)
    db.add_to_operation_accumulator.assert_awaited_once_with(
        42, "funding_paid_token1", 0.25,
    )


@pytest.mark.asyncio
async def test_on_funding_payment_skips_when_no_active_op():
    """No active op -> no DB write, no error."""
    engine, db = _make_engine_with_funding_state()
    engine._hub.current_operation_id = None
    entry = MagicMock(funding_id=3, market_id=0, timestamp=1700001000, change="0.10")
    await engine._on_funding_payment(entry)
    db.add_to_operation_accumulator.assert_not_called()


@pytest.mark.asyncio
async def test_on_funding_payment_skips_when_market_id_unmatched():
    """Funding for a market we're not hedging is ignored."""
    engine, db = _make_engine_with_funding_state(token0_mid=0, token1_mid=50)
    entry = MagicMock(funding_id=4, market_id=999, timestamp=1700001000, change="0.10")
    await engine._on_funding_payment(entry)
    db.add_to_operation_accumulator.assert_not_called()


@pytest.mark.asyncio
async def test_on_funding_payment_skips_entries_before_op_started():
    """Entries with timestamp < op.started_at are ignored (backfill bound)."""
    engine, db = _make_engine_with_funding_state(token0_mid=0, token1_mid=50)
    entry = MagicMock(funding_id=5, market_id=0, timestamp=1699999999, change="0.10")
    await engine._on_funding_payment(entry)
    db.add_to_operation_accumulator.assert_not_called()


@pytest.mark.asyncio
async def test_on_funding_payment_dedupes_by_funding_id():
    """Same funding_id seen twice -> writes only once."""
    engine, db = _make_engine_with_funding_state(token0_mid=0, token1_mid=50)
    entry = MagicMock(funding_id=6, market_id=0, timestamp=1700001000, change="0.10")
    await engine._on_funding_payment(entry)
    await engine._on_funding_payment(entry)  # second call same funding_id
    assert db.add_to_operation_accumulator.await_count == 1


@pytest.mark.asyncio
async def test_on_funding_payment_skips_when_market_ids_unresolved():
    """If _token0_mid/_token1_mid haven't loaded yet, skip without
    marking funding_id seen — next call (after metadata loads) retries."""
    engine, db = _make_engine_with_funding_state(token0_mid=None, token1_mid=None)
    entry = MagicMock(funding_id=7, market_id=0, timestamp=1700001000, change="0.10")
    await engine._on_funding_payment(entry)
    db.add_to_operation_accumulator.assert_not_called()
    # And not in the seen set:
    assert 7 not in engine._seen_funding_ids


@pytest.mark.asyncio
async def test_iterate_uses_trade_pnl_override_when_supported():
    """When the exchange exposes get_trade_pnl_since, _iterate fetches
    it and passes hedge_pnl_aggregate_override into compute_operation_pnl."""
    from engine import GridMakerEngine
    state = StateHub(hedge_ratio=1.0)
    state.operation_state = "active"
    state.current_operation_id = 42

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
    db.add_to_operation_accumulator = AsyncMock()
    db.insert_order_log = AsyncMock()
    db.get_active_grid_orders = AsyncMock(return_value=[])
    db.get_operation = AsyncMock(return_value={
        "id": 42, "started_at": 1700000000.0, "status": "active",
        "baseline_eth_price": 2000.0, "baseline_pool_value_usd": 50.0,
        "baseline_amount0": 0.01, "baseline_amount1": 100.0,
        "baseline_collateral": 100.0,
        "baseline_token0_usd_price": 2000.0,
        "baseline_token1_usd_price": 0.10,
    })

    exchange = MagicMock()
    exchange.name = "lighter"
    exchange.subscribe_funding = MagicMock()
    exchange.place_long_term_order = AsyncMock()
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=10.0))
    exchange.get_oracle_prices = AsyncMock(return_value={
        "ETH-USD": 2000.0, "ARB-USD": 0.10,
    })
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])
    # Authoritative cumulative pnl: baseline=-1.0 at op start, latest=-3.5 now.
    # Override = -2.5.
    exchange.get_trade_pnl_since = AsyncMock(return_value=(-1.0, -3.5))

    pool = MagicMock()
    pool.read_price = AsyncMock(return_value=20000.0)  # arbitrary in-range value
    beefy = MagicMock()
    beefy.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-100000, tick_upper=100000,  # full-range to avoid out-of-range branch
        amount0=0.01, amount1=100.0, share=1.0, raw_balance=10**18,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=18,
    )

    await engine._iterate()

    breakdown = state.operation_pnl_breakdown
    assert breakdown.get("hedge_pnl") == -2.5, (
        f"expected hedge_pnl=-2.5 (latest -3.5 minus baseline -1.0), "
        f"got {breakdown.get('hedge_pnl')}"
    )
    assert breakdown.get("hedge_pnl_token0") == -2.5
    assert breakdown.get("hedge_pnl_token1") == 0.0


@pytest.mark.asyncio
async def test_iterate_falls_back_when_get_trade_pnl_since_returns_none():
    """When get_trade_pnl_since returns None (transient error or
    unsupported adapter), _iterate falls back to the in-memory hedge
    accumulator without crashing."""
    from engine import GridMakerEngine
    state = StateHub(hedge_ratio=1.0)
    state.operation_state = "active"
    state.current_operation_id = 42
    state.hedge_realized_pnls = {"ETH-USD": 1.0}
    state.hedge_unrealized_pnls = {"ETH-USD": 0.5}

    settings = MagicMock()
    settings.dydx_symbol_token0 = "ETH-USD"
    settings.dydx_symbol_token1 = ""
    settings.threshold_aggressive = 0.01
    settings.max_open_orders = 200
    settings.pool_token0_symbol = "ETH"
    settings.pool_token1_symbol = "USDC"
    settings.alert_webhook_url = ""
    settings.dydx_symbol = "ETH-USD"
    settings.min_rebalance_notional_usd = 0.50

    db = MagicMock()
    db.add_to_operation_accumulator = AsyncMock()
    db.insert_order_log = AsyncMock()
    db.get_active_grid_orders = AsyncMock(return_value=[])
    db.get_operation = AsyncMock(return_value={
        "id": 42, "started_at": 1700000000.0, "status": "active",
        "baseline_eth_price": 2000.0, "baseline_pool_value_usd": 50.0,
        "baseline_amount0": 0.01, "baseline_amount1": 0.0,
        "baseline_collateral": 100.0,
    })

    exchange = MagicMock()
    exchange.name = "lighter"
    exchange.subscribe_funding = MagicMock()
    exchange.place_long_term_order = AsyncMock()
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=10.0))
    exchange.get_oracle_prices = AsyncMock(return_value={"ETH-USD": 2000.0})
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])
    exchange.get_trade_pnl_since = AsyncMock(return_value=None)  # error path

    pool = MagicMock()
    pool.read_price = AsyncMock(return_value=2000.0)
    beefy = MagicMock()
    # For decimals(18,6) at p_now=2000 USDC/ETH, tick range must bracket ~tick -200311.
    # tick_lower=-205311, tick_upper=-195311 give p_a~1213 and p_b~3297 USDC/ETH.
    beefy.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-205311, tick_upper=-195311,
        amount0=0.01, amount1=0.0, share=1.0, raw_balance=10**18,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=6,
    )

    await engine._iterate()

    breakdown = state.operation_pnl_breakdown
    # Fallback: legacy single-leg.  get_position=None causes _iterate to
    # overwrite hedge_unrealized_pnls["ETH-USD"] = 0.0 before the PnL calc,
    # so only hedge_realized_pnl (1.0) contributes.  The key point: no crash,
    # override is None, and the in-memory accumulator path is used.
    assert breakdown.get("hedge_pnl") == 1.0
    assert "hedge_pnl" in breakdown  # breakdown was produced (not empty)


@pytest.mark.asyncio
async def test_maybe_rebuild_vault_readers_re_resolves_funding_mids(monkeypatch):
    """REGRESSION 2026-05-08 (post-deploy): app.py startup calls
    resolve_market_ids_for_funding with the GLOBAL settings (from .env,
    where DYDX_SYMBOL_TOKEN1 is empty for cross-pair). Then engine's
    `_maybe_rebuild_vault_readers` swaps in pair_settings (which has
    the right token1 symbol). Without re-resolving, _token1_mid stays
    None and ARB funding entries silently skip the handler — confirmed
    in DB: funding_paid_token1 = 0 while funding_paid_token0 was
    receiving entries.

    Fix: rebuild path now calls resolve_market_ids_for_funding after
    swapping settings. This test verifies that wiring."""
    from engine import GridMakerEngine
    from config import Settings

    state = StateHub(hedge_ratio=1.0)
    initial_settings = MagicMock()
    initial_settings.dydx_symbol_token0 = "ETH-USD"
    initial_settings.dydx_symbol_token1 = ""  # empty (USD-pair fallback in .env)
    initial_settings.threshold_aggressive = 0.01
    initial_settings.max_open_orders = 200
    initial_settings.pool_token0_symbol = "ETH"
    initial_settings.pool_token1_symbol = "USDC"
    initial_settings.alert_webhook_url = ""
    initial_settings.dydx_symbol = "ETH-USD"
    initial_settings.min_rebalance_notional_usd = 0.50

    db = MagicMock()
    db.get_pair_from_cache = AsyncMock(return_value=None)
    exchange = MagicMock()
    exchange.name = "lighter"
    exchange.subscribe_funding = MagicMock()
    # Mids per symbol
    def market_meta_for(sym):
        m = MagicMock()
        m.market_index = {"ETH-USD": 0, "ARB-USD": 50}.get(sym, 999)
        return m
    exchange.get_market_meta = AsyncMock(side_effect=lambda s: market_meta_for(s))
    exchange.register_active_symbols = MagicMock()

    pool = MagicMock(); beefy = MagicMock()
    eng = GridMakerEngine(
        settings=initial_settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=6,
    )
    # Initial resolve happened in __init__'s subscribe_funding wiring is
    # synchronous; the explicit resolve is async and runs from app
    # startup. Simulate that now with the empty-token1 settings.
    await eng.resolve_market_ids_for_funding()
    assert eng._token0_mid == 0
    assert eng._token1_mid is None  # nothing to resolve since "" is falsy

    # Now simulate pair selection: build_readers_for_vault returns new
    # pair_settings with dydx_symbol_token1="ARB-USD".
    pair_settings = MagicMock()
    pair_settings.dydx_symbol_token0 = "ETH-USD"
    pair_settings.dydx_symbol_token1 = "ARB-USD"
    pair_settings.pool_token0_symbol = "WETH"
    pair_settings.pool_token1_symbol = "ARB"
    pair_settings.alert_webhook_url = ""
    pair_settings.dydx_symbol = "ETH-USD"
    pair_settings.min_rebalance_notional_usd = 0.50
    pair_settings.threshold_aggressive = 0.01
    pair_settings.max_open_orders = 200

    async def fake_build(*, settings, db, w3, selected_vault_id):
        return (pair_settings, MagicMock(), MagicMock(), 18, 18)

    monkeypatch.setattr("engine.pair_factory.build_readers_for_vault", fake_build)
    # also patch the import inside the engine module
    import engine.pair_factory
    monkeypatch.setattr(engine.pair_factory, "build_readers_for_vault", fake_build)

    # `_refresh_vault_readers` reads selected_vault_id from db, so stub it.
    db.get_selected_vault_id = AsyncMock(return_value="0xVAULT")
    # The method also requires `_pair_factory_w3` non-None to proceed.
    eng._pair_factory_w3 = MagicMock()

    await eng._refresh_vault_readers()

    # Now both mids should be resolved.
    assert eng._token0_mid == 0
    assert eng._token1_mid == 50, (
        "post-rebuild resolve must populate token1_mid from new pair_settings"
    )
