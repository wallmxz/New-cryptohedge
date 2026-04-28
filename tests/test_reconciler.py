import pytest
import time
from unittest.mock import AsyncMock, MagicMock
from engine.reconciler import Reconciler


@pytest.mark.asyncio
async def test_reconcile_cancels_db_orphans():
    """Orders in exchange but not in DB -> cancel them."""
    db = MagicMock()
    db.get_active_grid_orders = AsyncMock(return_value=[
        {"cloid": "100", "side": "sell"}
    ])
    db.mark_grid_order_cancelled = AsyncMock()

    exchange = MagicMock()
    # Exchange has cloids 100 AND 200; 200 is orphan (not in DB)
    exchange.get_open_orders_cloids = AsyncMock(return_value=["100", "200"])
    exchange.cancel_long_term_order = AsyncMock()

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"

    rec = Reconciler(db=db, exchange=exchange, settings=settings)
    cancelled = await rec.reconcile()
    assert "200" in cancelled
    exchange.cancel_long_term_order.assert_called_with(symbol="ETH-USD", cloid_int=200)


@pytest.mark.asyncio
async def test_reconcile_marks_db_orders_dead():
    """Orders in DB but not on exchange -> mark cancelled in DB."""
    db = MagicMock()
    db.get_active_grid_orders = AsyncMock(return_value=[
        {"cloid": "100", "side": "sell"},
        {"cloid": "300", "side": "buy"},  # missing on exchange
    ])
    db.mark_grid_order_cancelled = AsyncMock()

    exchange = MagicMock()
    exchange.get_open_orders_cloids = AsyncMock(return_value=["100"])
    exchange.cancel_long_term_order = AsyncMock()

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"

    rec = Reconciler(db=db, exchange=exchange, settings=settings)
    await rec.reconcile()
    # at minimum check it was called for the missing cloid
    assert db.mark_grid_order_cancelled.call_count >= 1
    # First arg should be "300" (the lost cloid)
    cancelled_calls = [c.args[0] for c in db.mark_grid_order_cancelled.call_args_list]
    assert "300" in cancelled_calls
