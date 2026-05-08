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
