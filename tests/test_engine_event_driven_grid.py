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

    await engine._apply_fills_to_grid(filled_cloids={100}, step=step)

    # Assertions
    assert engine._exchange.cancel_stop_order.call_count == 1
    cancel_call = engine._exchange.cancel_stop_order.call_args
    # lowest buy should be cancelled (cloid 201)
    assert cancel_call.kwargs.get("cloid_int") == 201

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
