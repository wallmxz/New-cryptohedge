import pytest
from unittest.mock import AsyncMock, MagicMock
from state import StateHub
from engine.curve import GridLevel


@pytest.mark.asyncio
async def test_engine_iteration_in_range_builds_grid():
    """One iteration: reader returns position in range; engine builds + places grid."""
    from engine import GridMakerEngine

    state = StateHub()
    state.hedge_ratio = 1.0
    state.max_exposure_pct = 0.05

    settings = MagicMock()
    settings.active_exchange = "dydx"
    settings.dydx_symbol = "ETH-USD"
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"
    settings.threshold_aggressive = 0.05
    settings.threshold_recovery = 0.02
    settings.max_open_orders = 200
    settings.clm_vault_address = "0xvault"
    settings.clm_pool_address = "0xpool"
    settings.wallet_address = "0xwallet"

    db = MagicMock()
    db.insert_grid_order = AsyncMock()
    db.get_active_grid_orders = AsyncMock(return_value=[])
    db.insert_order_log = AsyncMock()

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(
        tick_size=0.1, step_size=0.001, min_notional=3.0,
    ))
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.batch_place = AsyncMock(return_value=[])
    exchange.batch_cancel = AsyncMock(return_value=0)

    pool_reader = MagicMock()
    pool_reader.read_price = AsyncMock(return_value=3000.0)

    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=78240, tick_upper=80580,  # ~$2700-$3300 range
        amount0=0.5, amount1=1500.0, share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
        decimals0=18, decimals1=6,
    )
    # Run one iteration
    await engine._iterate()

    # Range was set in state
    assert state.range_lower > 0
    assert state.range_upper > state.range_lower
    assert state.liquidity_l > 0
