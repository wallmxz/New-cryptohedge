"""Tests for the engine's _refresh_grid polling logic."""
import time
from unittest.mock import AsyncMock, MagicMock
import pytest

from state import StateHub


def _engine_with_predictive():
    """Build an engine instance ready to test refresh logic."""
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
    exchange = MagicMock()
    exchange.name = "lighter"
    exchange.subscribe_funding = MagicMock()
    exchange.get_market_meta = AsyncMock()

    pool = MagicMock()
    pool.read_slot0 = AsyncMock(return_value=(int(2**96 * 1.0), -78500))
    beefy = MagicMock()
    beefy._strategy = MagicMock()
    beefy._strategy.functions = MagicMock()
    beefy._strategy.functions.positionMain = MagicMock(
        return_value=MagicMock(
            call=AsyncMock(return_value=((-81121, -76012), (0, 0), 0, 0)),
        )
    )

    eng = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=18,
    )
    return eng


@pytest.mark.asyncio
async def test_grid_stale_true_when_never_checked():
    eng = _engine_with_predictive()
    assert eng._grid_stale() is True


@pytest.mark.asyncio
async def test_grid_stale_false_within_interval():
    eng = _engine_with_predictive()
    eng._last_grid_check_at = time.monotonic()
    assert eng._grid_stale() is False


@pytest.mark.asyncio
async def test_grid_stale_true_after_interval():
    eng = _engine_with_predictive()
    eng._last_grid_check_at = time.monotonic() - 61
    assert eng._grid_stale() is True


@pytest.mark.asyncio
async def test_refresh_grid_builds_when_no_grid_exists(monkeypatch):
    eng = _engine_with_predictive()
    eng._beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-81121, tick_upper=-76012,
        amount0=0.01, amount1=100.0, share=1.0, raw_balance=10**18,
    ))
    await eng._refresh_grid()
    assert eng._grid is not None
    assert eng._grid.tick_lower == -81121
    assert eng._grid.tick_upper == -76012
    assert eng._last_level_idx is None


@pytest.mark.asyncio
async def test_refresh_grid_skips_rebuild_when_unchanged(monkeypatch):
    eng = _engine_with_predictive()
    eng._beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-81121, tick_upper=-76012,
        amount0=0.01, amount1=100.0, share=1.0, raw_balance=10**18,
    ))
    await eng._refresh_grid()
    grid_id_before = id(eng._grid)
    eng._last_level_idx = 5

    eng._last_grid_check_at -= 100
    await eng._refresh_grid()

    assert id(eng._grid) == grid_id_before
    assert eng._last_level_idx == 5


@pytest.mark.asyncio
async def test_refresh_grid_keeps_old_grid_on_rpc_failure():
    eng = _engine_with_predictive()
    eng._beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-81121, tick_upper=-76012,
        amount0=0.01, amount1=100.0, share=1.0, raw_balance=10**18,
    ))
    await eng._refresh_grid()
    grid_before = eng._grid

    eng._beefy_reader._strategy.functions.positionMain.return_value.call = AsyncMock(
        side_effect=RuntimeError("RPC timeout"),
    )
    eng._last_grid_check_at -= 100
    await eng._refresh_grid()

    assert eng._grid is grid_before
    assert (time.monotonic() - eng._last_grid_check_at) < 5
