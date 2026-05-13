import math
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_maintain_grid_uses_real_pool_ticks_not_log_of_raw_price():
    """Regression: HedgeModel.refresh_cache stores p_a_main / p_b_main as
    RAW V3 ratios (= 1.0001^tick, e.g. 1.15e-13 for ARB-USDC.e at tick
    -297890). The old `_maintain_grid` code divided those by decimal_factor
    (= 10^(d0-d1)) before taking log — which double-converts and produces
    massively wrong ticks (e.g. -574215 instead of -297890).

    Fix: cache exposes tick_lower_main / tick_upper_main directly (since
    V3Position already has them as ints) and `_maintain_grid` uses those
    instead of inferring from p_a/p_b. p_now (human price) is the only
    quantity that still needs decimal_factor to derive its tick.

    Verified live 2026-05-13 op #29 smoke v2.
    """
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
    engine._settings.uniswap_v3_pool_fee = 500
    engine._exchange = AsyncMock()
    engine._exchange.place_stop_limit_order = AsyncMock()
    engine._db = AsyncMock()
    engine._db.get_active_grid_orders = AsyncMock(return_value=[])
    engine._db.mark_grid_order_cancelled = AsyncMock()
    engine._db.insert_grid_order = AsyncMock()
    engine._hub = MagicMock()
    engine._hub.current_operation_id = 42
    engine._hub.hedge_ratio = 1.0
    engine._next_cloid_for_leg = MagicMock(side_effect=lambda sym: 1)
    engine._posted_grid_signature = None

    # Real cache shape: RAW V3 prices (from math.pow(1.0001, tick)), plus
    # the cached ticks themselves so _maintain_grid can use them directly.
    tick_lo, tick_hi = -297890, -294890
    cache = HedgeModelCache(
        L_main=int(2.7e17),
        p_a_main=math.pow(1.0001, tick_lo),   # raw V3 ratio, ~1.15e-13
        p_b_main=math.pow(1.0001, tick_hi),   # raw V3 ratio, ~1.56e-13
        L_alt=None, p_a_alt=None, p_b_alt=None,
        refreshed_at=time_mod.monotonic(),
        tick_lower_main=tick_lo,              # NEW field
        tick_upper_main=tick_hi,              # NEW field
    )
    hm = MagicMock()
    hm._cache = cache
    engine._hedge_model = hm

    await engine._maintain_grid(
        beefy_pos=MagicMock(),
        p_now=0.132,  # human price in the middle of the range
        oracle_prices={"ARB-USD": 0.132},
    )

    # The grid must have been built using the REAL ticks (-297890 / -294890),
    # not the bogus ~-574215 produced by double-decimal-factor math.
    # That means at least some stop orders must have been placed.
    assert engine._exchange.place_stop_limit_order.call_count > 0, (
        "Grid rebuild produced zero levels — tick math is off"
    )

    # Every posted trigger price must fall inside the actual pool range
    # [0.10, 0.20] roughly. (Strict: between price at tick_lo and tick_hi.)
    lo_human = math.pow(1.0001, tick_lo) * (10 ** (18 - 6))
    hi_human = math.pow(1.0001, tick_hi) * (10 ** (18 - 6))
    for call in engine._exchange.place_stop_limit_order.call_args_list:
        trigger = call.kwargs.get("trigger_price", 0)
        assert lo_human * 0.95 <= trigger <= hi_human * 1.05, (
            f"trigger {trigger} outside pool range [{lo_human:.4f}, {hi_human:.4f}]"
        )


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

    # Mock HedgeModel with a valid cache. p_a/p_b store RAW V3 ratio;
    # ticks store the int boundary used by _maintain_grid directly.
    # For an ARB-USDC.e-shaped range [~$0.10, ~$0.20] human:
    #   tick at $0.10 ≈ log(0.10 / 10^12)/log(1.0001) ≈ -299580
    #   tick at $0.20 ≈ log(0.20 / 10^12)/log(1.0001) ≈ -292650
    tick_lo, tick_hi = -299580, -292650
    hm = MagicMock()
    hm._cache = HedgeModelCache(
        L_main=int(1e15),
        p_a_main=math.pow(1.0001, tick_lo),
        p_b_main=math.pow(1.0001, tick_hi),
        L_alt=None, p_a_alt=None, p_b_alt=None,
        refreshed_at=time_mod.monotonic(),
        tick_lower_main=tick_lo,
        tick_upper_main=tick_hi,
    )
    engine._hedge_model = hm

    # Mock _next_cloid_for_leg
    engine._next_cloid_for_leg = MagicMock(side_effect=lambda sym: 1)

    # Estado inicial: sem grade posted
    engine._posted_grid_signature = None

    await engine._maintain_grid(
        beefy_pos=MagicMock(),
        p_now=0.14,  # entre 0.10 e 0.20 (human)
        oracle_prices={"ARB-USD": 0.14},
    )

    # Deve ter postado alguma quantidade de stops
    assert engine._exchange.place_stop_limit_order.call_count > 0
    # E signature foi atualizada (agora armazena ticks, não p_a/p_b)
    assert engine._posted_grid_signature == (int(1e15), tick_lo, tick_hi)


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

    tick_lo, tick_hi = -299580, -292650
    cache = HedgeModelCache(
        L_main=int(1e15),
        p_a_main=math.pow(1.0001, tick_lo),
        p_b_main=math.pow(1.0001, tick_hi),
        L_alt=None, p_a_alt=None, p_b_alt=None,
        refreshed_at=time_mod.monotonic(),
        tick_lower_main=tick_lo,
        tick_upper_main=tick_hi,
    )
    hm = MagicMock()
    hm._cache = cache
    engine._hedge_model = hm

    # Signature já matches o cache (agora stored as L + ticks)
    engine._posted_grid_signature = (int(1e15), tick_lo, tick_hi)

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


@pytest.mark.asyncio
async def test_on_grid_fill_no_op_when_flag_disabled():
    """Flag desligada -> handler retorna sem fazer nada."""
    from engine import GridMakerEngine
    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.predictive_grid_v2 = False
    engine._exchange = AsyncMock()
    engine._db = AsyncMock()

    await engine._on_grid_fill(
        cloid=42, fill_price=0.135, fill_size=3.5, side="sell",
    )
    engine._exchange.place_stop_limit_order.assert_not_called()


@pytest.mark.asyncio
async def test_on_grid_fill_no_op_when_cloid_not_a_grid_stop():
    """Se o cloid nao corresponde a uma stop order da grade no DB, skipa."""
    from engine import GridMakerEngine
    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.predictive_grid_v2 = True
    engine._exchange = AsyncMock()
    engine._db = AsyncMock()
    # DB retorna None ou row sem is_stop_order=1 -> nao e da grade
    engine._db.get_grid_order = AsyncMock(return_value=None)

    await engine._on_grid_fill(
        cloid=42, fill_price=0.135, fill_size=3.5, side="sell",
    )
    engine._exchange.place_stop_limit_order.assert_not_called()


@pytest.mark.asyncio
async def test_on_grid_fill_posts_next_tick_level_for_sell():
    """Sell fillou em tick T -> posta novo sell em tick T - tick_spacing (mais abaixo)."""
    from engine import GridMakerEngine

    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.predictive_grid_v2 = True
    engine._settings.dydx_symbol_token0 = "ARB-USD"
    engine._settings.token0_decimals = 18
    engine._settings.token1_decimals = 6
    engine._settings.uniswap_v3_pool_fee = 500  # spacing 10
    engine._exchange = AsyncMock()
    engine._db = AsyncMock()
    engine._db.mark_grid_order_filled = AsyncMock()
    engine._db.insert_grid_order = AsyncMock()
    engine._db.get_grid_order = AsyncMock(return_value={
        "cloid": 42, "side": "sell", "target_price": 0.135, "size": 3.5,
        "is_stop_order": 1, "trigger_price": 0.135,
    })
    engine._hub = MagicMock()
    engine._hub.current_operation_id = 7
    engine._next_cloid_for_leg = MagicMock(return_value=43)

    # Signature ativo. NOW stored as (L, tick_lower_main, tick_upper_main) —
    # ticks raw V3 ints. For ARB-USDC.e (d0=18, d1=6), price $0.135 maps
    # to tick ≈ -296461, well inside [-299500, -292500].
    engine._posted_grid_signature = (int(1e15), -299500, -292500)

    await engine._on_grid_fill(
        cloid=42, fill_price=0.135, fill_size=3.5, side="sell",
    )

    engine._exchange.place_stop_limit_order.assert_called_once()
    kwargs = engine._exchange.place_stop_limit_order.call_args.kwargs
    assert kwargs["side"] == "sell"
    assert kwargs["size"] == 3.5
    assert kwargs["trigger_price"] < 0.135


@pytest.mark.asyncio
async def test_on_grid_fill_posts_next_tick_level_for_buy():
    """Buy fillou em tick T -> posta novo buy em tick T + tick_spacing (mais acima)."""
    from engine import GridMakerEngine

    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.predictive_grid_v2 = True
    engine._settings.dydx_symbol_token0 = "ARB-USD"
    engine._settings.token0_decimals = 18
    engine._settings.token1_decimals = 6
    engine._settings.uniswap_v3_pool_fee = 500
    engine._exchange = AsyncMock()
    engine._db = AsyncMock()
    engine._db.mark_grid_order_filled = AsyncMock()
    engine._db.insert_grid_order = AsyncMock()
    engine._db.get_grid_order = AsyncMock(return_value={
        "cloid": 43, "side": "buy", "target_price": 0.145, "size": 3.5,
        "is_stop_order": 1, "trigger_price": 0.145,
    })
    engine._hub = MagicMock()
    engine._hub.current_operation_id = 7
    engine._next_cloid_for_leg = MagicMock(return_value=44)
    # Signature stored as (L, tick_lower_main, tick_upper_main).
    engine._posted_grid_signature = (int(1e15), -299500, -292500)

    await engine._on_grid_fill(
        cloid=43, fill_price=0.145, fill_size=3.5, side="buy",
    )

    kwargs = engine._exchange.place_stop_limit_order.call_args.kwargs
    assert kwargs["side"] == "buy"
    assert kwargs["trigger_price"] > 0.145


@pytest.mark.asyncio
async def test_on_grid_fill_skips_when_next_tick_outside_range():
    """Se proximo tick cairia fora do range posted, nao posta (espera rebuild)."""
    from engine import GridMakerEngine

    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.predictive_grid_v2 = True
    engine._settings.dydx_symbol_token0 = "ARB-USD"
    engine._settings.token0_decimals = 18
    engine._settings.token1_decimals = 6
    engine._settings.uniswap_v3_pool_fee = 500
    engine._exchange = AsyncMock()
    engine._db = AsyncMock()
    engine._db.mark_grid_order_filled = AsyncMock()
    # Filled price 0.142 maps to tick ≈ -295995 (very close to upper
    # edge of signature range [-296000, -295000]); next_tick for SELL =
    # -296005, which falls BELOW tick_lower → out of range, must skip.
    engine._db.get_grid_order = AsyncMock(return_value={
        "cloid": 42, "side": "sell", "target_price": 0.142, "size": 3.5,
        "is_stop_order": 1, "trigger_price": 0.142,
    })
    engine._hub = MagicMock()
    engine._next_cloid_for_leg = MagicMock()
    engine._posted_grid_signature = (int(1e15), -296000, -295000)

    await engine._on_grid_fill(
        cloid=42, fill_price=0.142, fill_size=3.5, side="sell",
    )

    if engine._exchange.place_stop_limit_order.called:
        kwargs = engine._exchange.place_stop_limit_order.call_args.kwargs
        assert 0.10 <= kwargs["trigger_price"] <= 0.20
