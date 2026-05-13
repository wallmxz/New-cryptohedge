import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_maintain_grid_no_op_when_flag_disabled():
    """Sem PREDICTIVE_GRID_V2 ativado, _maintain_grid retorna sem fazer nada."""
    from engine import GridMakerEngine
    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.predictive_grid_v2 = False
    engine._exchange = AsyncMock()
    # _maintain_grid existe e é safe-no-op
    await engine._maintain_grid(
        beefy_pos=MagicMock(), p_now=0.14, oracle_prices={"ARB-USD": 0.14},
    )
    # Não chamou nada do exchange
    engine._exchange.place_stop_limit_order.assert_not_called()
    if hasattr(engine._exchange, 'cancel_stop_order'):
        engine._exchange.cancel_stop_order.assert_not_called()


@pytest.mark.asyncio
async def test_maintain_grid_rebuilds_when_no_grid_posted():
    """Quando não tem grade posted (signature=None) e há cache válido,
    _maintain_grid posta todos os níveis novos."""
    from engine import GridMakerEngine
    from engine.hedge_model import HedgeModelCache
    import time as time_mod

    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.predictive_grid_v2 = True
    engine._settings.dydx_symbol_token0 = "ARB-USD"
    engine._settings.token0_decimals = 18
    engine._settings.token1_decimals = 6
    engine._settings.hedge_ratio = 1.0
    engine._settings.uniswap_v3_pool_fee = 500  # tick_spacing 10
    engine._exchange = AsyncMock()
    engine._db = AsyncMock()
    engine._db.get_active_grid_orders = AsyncMock(return_value=[])
    engine._db.mark_grid_order_cancelled = AsyncMock()
    engine._db.insert_grid_order = AsyncMock()
    engine._hub = MagicMock()
    engine._hub.current_operation_id = 42
    engine._hub.hedge_ratio = 1.0

    # Mock HedgeModel with a valid cache
    hm = MagicMock()
    hm._cache = HedgeModelCache(
        L_main=int(1e15),
        p_a_main=0.10,  # human price USDC.e per ARB
        p_b_main=0.20,
        L_alt=None, p_a_alt=None, p_b_alt=None,
        refreshed_at=time_mod.monotonic(),
    )
    engine._hedge_model = hm

    # Mock _next_cloid_for_leg
    engine._next_cloid_for_leg = MagicMock(side_effect=lambda sym: 1)

    # Estado inicial: sem grade posted
    engine._posted_grid_signature = None

    await engine._maintain_grid(
        beefy_pos=MagicMock(),
        p_now=0.14,  # entre 0.10 e 0.20
        oracle_prices={"ARB-USD": 0.14},
    )

    # Deve ter postado alguma quantidade de stops
    assert engine._exchange.place_stop_limit_order.call_count > 0
    # E signature foi atualizada
    assert engine._posted_grid_signature is not None


@pytest.mark.asyncio
async def test_maintain_grid_no_op_when_signature_unchanged():
    """Se HedgeModel cache não mudou, _maintain_grid não recompila grade."""
    from engine import GridMakerEngine
    from engine.hedge_model import HedgeModelCache
    import time as time_mod

    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.predictive_grid_v2 = True
    engine._exchange = AsyncMock()
    engine._db = AsyncMock()
    engine._hub = MagicMock()

    cache = HedgeModelCache(
        L_main=int(1e15), p_a_main=0.10, p_b_main=0.20,
        L_alt=None, p_a_alt=None, p_b_alt=None,
        refreshed_at=time_mod.monotonic(),
    )
    hm = MagicMock()
    hm._cache = cache
    engine._hedge_model = hm

    # Signature já matches o cache
    engine._posted_grid_signature = (int(1e15), 0.10, 0.20)

    await engine._maintain_grid(
        beefy_pos=MagicMock(),
        p_now=0.14,
        oracle_prices={"ARB-USD": 0.14},
    )

    # Sem chamadas pro exchange
    engine._exchange.place_stop_limit_order.assert_not_called()
    engine._exchange.cancel_all_stops.assert_not_called()


@pytest.mark.asyncio
async def test_maintain_grid_no_op_when_cache_cold():
    """Sem cache no HedgeModel, _maintain_grid skipa."""
    from engine import GridMakerEngine
    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.predictive_grid_v2 = True
    engine._exchange = AsyncMock()

    hm = MagicMock()
    hm._cache = None  # cold
    engine._hedge_model = hm

    await engine._maintain_grid(
        beefy_pos=MagicMock(), p_now=0.14, oracle_prices={"ARB-USD": 0.14},
    )

    engine._exchange.place_stop_limit_order.assert_not_called()
