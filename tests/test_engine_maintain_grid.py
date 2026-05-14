import math
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_maintain_grid_caps_at_max_open_orders_centered_on_tick_now():
    """Lighter has a max_pending_orders_per_market limit (=16 for ARB-USD,
    code=21720). When the full V3-tick-by-tick grid exceeds max_open_orders,
    `_maintain_grid` must keep the N levels CLOSEST to tick_now and drop the
    rest (so the bot reacts to near-term price moves first).

    Validated live 2026-05-13 op #29 smoke: without this cap, the 300+
    generated levels filled the 16-order slot with extreme-edge sells only,
    leaving the bot blind to nearby moves.
    """
    from engine import GridMakerEngine
    from engine.hedge_model import HedgeModelCache
    import time as time_mod

    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.grid_anticipation_buffer = 0.0
    engine._settings.max_open_orders = 16
    engine._settings.min_rebalance_notional_usd = 1.0
    engine._settings.predictive_grid_v2 = True
    engine._settings.dydx_symbol_token0 = "ARB-USD"
    engine._settings.token0_decimals = 18
    engine._settings.token1_decimals = 6
    engine._settings.hedge_ratio = 1.0
    engine._settings.uniswap_v3_pool_fee = 500
    engine._settings.max_open_orders = 16
    engine._exchange = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
    engine._db = AsyncMock()
    engine._db.get_active_grid_orders = AsyncMock(return_value=[])
    engine._db.mark_grid_order_cancelled = AsyncMock()
    engine._db.insert_grid_order = AsyncMock()
    engine._hub = MagicMock()
    engine._hub.current_operation_id = 1
    engine._hub.hedge_ratio = 1.0
    engine._next_cloid_for_leg = MagicMock(side_effect=lambda sym: 1)
    engine._posted_grid_signature = None

    tick_lo, tick_hi = -297890, -294890  # 3000-tick range, spacing 10 → 300 levels
    cache = HedgeModelCache(
        L_main=int(2.74e17),
        p_a_main=math.pow(1.0001, tick_lo),
        p_b_main=math.pow(1.0001, tick_hi),
        L_alt=None, p_a_alt=None, p_b_alt=None,
        refreshed_at=time_mod.monotonic(),
        tick_lower_main=tick_lo, tick_upper_main=tick_hi,
    )
    hm = MagicMock()
    hm._cache = cache
    engine._hedge_model = hm

    beefy_pos = MagicMock()
    beefy_pos.share = 0.0079

    p_now = 0.132  # close to middle of human range $0.116-$0.156
    await engine._maintain_grid(
        beefy_pos=beefy_pos, p_now=p_now,
        oracle_prices={"ARB-USD": p_now},
    )

    # Engine should post AT MOST max_open_orders (16) levels, all close to p_now.
    calls = engine._exchange.place_stop_market.call_args_list
    assert len(calls) <= 16
    assert len(calls) > 0
    prices = sorted([c.kwargs["trigger_price"] for c in calls])
    # All posted levels must be within ~$0.01 of p_now (we expect tight density)
    for p in prices:
        assert abs(p - p_now) < 0.01, (
            f"posted level {p} too far from p_now {p_now} — cap should center"
        )
    # And both sides represented (we want mix of sells below + buys above)
    sides = {c.kwargs["side"] for c in calls}
    assert sides == {"sell", "buy"}, (
        f"expected both sides centered, got only {sides}"
    )


@pytest.mark.asyncio
async def test_maintain_grid_applies_anticipation_buffer_to_triggers():
    """Per user spec 2026-05-13: anticipation buffer shifts each level's
    trigger so the SL_MARKET fires slightly before reaching the V3 tick,
    capturing the implicit spread (~$0.00005 on Lighter ARB-USD).

    SELL trigger = tick_price + buffer  (fires earlier as price drops)
    BUY  trigger = tick_price - buffer  (fires earlier as price rises)
    """
    from engine import GridMakerEngine
    from engine.hedge_model import HedgeModelCache
    import time as time_mod

    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.grid_anticipation_buffer = 0.00005
    engine._settings.max_open_orders = 16
    engine._settings.min_rebalance_notional_usd = 1.0
    engine._settings.predictive_grid_v2 = True
    engine._settings.dydx_symbol_token0 = "ARB-USD"
    engine._settings.token0_decimals = 18
    engine._settings.token1_decimals = 6
    engine._settings.hedge_ratio = 1.0
    engine._settings.uniswap_v3_pool_fee = 500
    engine._exchange = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
    engine._db = AsyncMock()
    engine._db.get_active_grid_orders = AsyncMock(return_value=[])
    engine._db.mark_grid_order_cancelled = AsyncMock()
    engine._db.insert_grid_order = AsyncMock()
    engine._hub = MagicMock()
    engine._hub.current_operation_id = 1
    engine._hub.hedge_ratio = 1.0
    engine._next_cloid_for_leg = MagicMock(return_value=1)
    engine._posted_grid_signature = None

    tick_lo, tick_hi = -297890, -294890
    cache = HedgeModelCache(
        L_main=int(2.74e17),
        p_a_main=math.pow(1.0001, tick_lo),
        p_b_main=math.pow(1.0001, tick_hi),
        L_alt=None, p_a_alt=None, p_b_alt=None,
        refreshed_at=time_mod.monotonic(),
        tick_lower_main=tick_lo, tick_upper_main=tick_hi,
    )
    hm = MagicMock()
    hm._cache = cache
    engine._hedge_model = hm

    beefy_pos = MagicMock()
    beefy_pos.share = 0.0079

    await engine._maintain_grid(
        beefy_pos=beefy_pos, p_now=0.132,
        oracle_prices={"ARB-USD": 0.132},
    )

    # For each posted level, verify trigger reflects the buffer offset.
    # The DB insert receives both target_price (tick) and trigger (shifted).
    calls = engine._exchange.place_stop_market.call_args_list
    db_calls = engine._db.insert_grid_order.call_args_list
    assert len(calls) > 0, "no levels posted"
    assert len(db_calls) == len(calls)
    for db_call in db_calls:
        kw = db_call.kwargs
        target = kw["target_price"]
        trigger = kw["trigger_price"]
        side = kw["side"]
        if side == "sell":
            assert trigger > target, f"sell trigger {trigger} should be > tick {target}"
            assert abs(trigger - target - 0.00005) < 1e-9
        else:
            assert trigger < target, f"buy trigger {trigger} should be < tick {target}"
            assert abs(target - trigger - 0.00005) < 1e-9


@pytest.mark.asyncio
async def test_maintain_grid_scales_raw_L_by_decimal_factor_and_share():
    """Regression: cache.L_main stores RAW V3 strategy-total liquidity
    (e.g. 2.74e17 for ARB/USDC.e). Passing it raw to compute_x produces
    per-level sizes in raw token0 units (~10^15), which exceed Lighter's
    2^48 BaseAmount limit and crash place_stop_market on every level.

    Correct scaling: L_eff = L_raw / 10^((d0+d1)/2) * share. For ARB/USDC.e
    (d0=18, d1=6, share=0.79%): L_eff = 2.74e17 / 10^12 * 0.0079 ≈ 2180,
    which yields per-level sizes ~1-3 ARB (well within Lighter limits).

    Validated live 2026-05-13 op #29 smoke v2.
    """
    from engine import GridMakerEngine
    from engine.hedge_model import HedgeModelCache
    import time as time_mod

    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.grid_anticipation_buffer = 0.0
    engine._settings.max_open_orders = 16
    engine._settings.min_rebalance_notional_usd = 1.0
    engine._settings.predictive_grid_v2 = True
    engine._settings.dydx_symbol_token0 = "ARB-USD"
    engine._settings.token0_decimals = 18
    engine._settings.token1_decimals = 6
    engine._settings.hedge_ratio = 1.0
    engine._settings.uniswap_v3_pool_fee = 500
    engine._exchange = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
    engine._db = AsyncMock()
    engine._db.get_active_grid_orders = AsyncMock(return_value=[])
    engine._db.mark_grid_order_cancelled = AsyncMock()
    engine._db.insert_grid_order = AsyncMock()
    engine._hub = MagicMock()
    engine._hub.current_operation_id = 42
    engine._hub.hedge_ratio = 1.0
    engine._next_cloid_for_leg = MagicMock(return_value=1)
    engine._posted_grid_signature = None

    # Realistic ARB/USDC.e shape (from live op #29):
    tick_lo, tick_hi = -297890, -294890
    L_raw_strategy = int(2.74e17)
    user_share = 0.0079

    cache = HedgeModelCache(
        L_main=L_raw_strategy,
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

    beefy_pos = MagicMock()
    beefy_pos.share = user_share

    await engine._maintain_grid(
        beefy_pos=beefy_pos,
        p_now=0.132,
        oracle_prices={"ARB-USD": 0.132},
    )

    # Per-level size should be on the order of 1-10 ARB (NOT 10^15).
    # The Lighter SDK's BaseAmount limit is 2^48 = 2.81e14; with
    # size_decimals=1, that means human size < 2.81e13.
    calls = engine._exchange.place_stop_market.call_args_list
    assert len(calls) > 0, "no levels placed at all"
    for call in calls:
        size = call.kwargs.get("size", 0)
        assert 0 < size < 1000, (
            f"size {size} out of plausible range (per-level should be a "
            f"small ARB amount, not raw V3 units)"
        )


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
    engine._settings.grid_anticipation_buffer = 0.0
    engine._settings.max_open_orders = 16
    engine._settings.min_rebalance_notional_usd = 1.0
    engine._settings.predictive_grid_v2 = True
    engine._settings.dydx_symbol_token0 = "ARB-USD"
    engine._settings.token0_decimals = 18
    engine._settings.token1_decimals = 6
    engine._settings.hedge_ratio = 1.0
    engine._settings.uniswap_v3_pool_fee = 500
    engine._exchange = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
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
    assert engine._exchange.place_stop_market.call_count > 0, (
        "Grid rebuild produced zero levels — tick math is off"
    )

    # Every posted trigger price must fall inside the actual pool range
    # [0.10, 0.20] roughly. (Strict: between price at tick_lo and tick_hi.)
    lo_human = math.pow(1.0001, tick_lo) * (10 ** (18 - 6))
    hi_human = math.pow(1.0001, tick_hi) * (10 ** (18 - 6))
    for call in engine._exchange.place_stop_market.call_args_list:
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
    engine._settings.grid_anticipation_buffer = 0.0
    engine._settings.max_open_orders = 16
    engine._settings.min_rebalance_notional_usd = 1.0
    engine._settings.predictive_grid_v2 = False
    engine._exchange = AsyncMock()
    # _maintain_grid existe e é safe-no-op
    await engine._maintain_grid(
        beefy_pos=MagicMock(), p_now=0.14, oracle_prices={"ARB-USD": 0.14},
    )
    # Não chamou nada do exchange
    engine._exchange.place_stop_market.assert_not_called()
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
    engine._settings.grid_anticipation_buffer = 0.0
    engine._settings.max_open_orders = 16
    engine._settings.min_rebalance_notional_usd = 1.0
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
    assert engine._exchange.place_stop_market.call_count > 0
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
    engine._settings.grid_anticipation_buffer = 0.0
    engine._settings.max_open_orders = 16
    engine._settings.min_rebalance_notional_usd = 1.0
    engine._settings.predictive_grid_v2 = True
    engine._settings.token0_decimals = 18
    engine._settings.token1_decimals = 6
    engine._settings.dydx_symbol_token0 = "ARB-USD"
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
    engine._exchange.place_stop_market.assert_not_called()
    engine._exchange.cancel_all_stops.assert_not_called()


@pytest.mark.asyncio
async def test_maintain_grid_no_op_when_cache_cold():
    """Sem cache no HedgeModel, _maintain_grid skipa."""
    from engine import GridMakerEngine
    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.grid_anticipation_buffer = 0.0
    engine._settings.max_open_orders = 16
    engine._settings.min_rebalance_notional_usd = 1.0
    engine._settings.predictive_grid_v2 = True
    engine._exchange = AsyncMock()

    hm = MagicMock()
    hm._cache = None  # cold
    engine._hedge_model = hm

    await engine._maintain_grid(
        beefy_pos=MagicMock(), p_now=0.14, oracle_prices={"ARB-USD": 0.14},
    )

    engine._exchange.place_stop_market.assert_not_called()


@pytest.mark.asyncio
async def test_on_grid_fill_no_op_when_flag_disabled():
    """Flag desligada -> handler retorna sem fazer nada."""
    from engine import GridMakerEngine
    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.grid_anticipation_buffer = 0.0
    engine._settings.max_open_orders = 16
    engine._settings.min_rebalance_notional_usd = 1.0
    engine._settings.predictive_grid_v2 = False
    engine._exchange = AsyncMock()
    engine._db = AsyncMock()

    await engine._on_grid_fill(
        cloid=42, fill_price=0.135, fill_size=3.5, side="sell",
    )
    engine._exchange.place_stop_market.assert_not_called()


@pytest.mark.asyncio
def _trailing_engine(side: str, *, live_orders: list[dict],
                     sig=(int(1e15), -299500, -292500), buffer=0.0):
    """Helper: build a GridMakerEngine pre-wired for _on_grid_fill trailing tests."""
    from engine import GridMakerEngine
    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.grid_anticipation_buffer = buffer
    engine._settings.max_open_orders = 16
    engine._settings.min_rebalance_notional_usd = 1.0
    engine._settings.predictive_grid_v2 = True
    engine._settings.dydx_symbol_token0 = "ARB-USD"
    engine._settings.token0_decimals = 18
    engine._settings.token1_decimals = 6
    engine._settings.uniswap_v3_pool_fee = 500  # spacing 10
    engine._exchange = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
    engine._exchange.cancel_stop_order = AsyncMock()
    engine._exchange.get_open_orders = AsyncMock(return_value=live_orders)
    engine._db = AsyncMock()
    engine._db.mark_grid_order_filled = AsyncMock()
    engine._db.insert_grid_order = AsyncMock()
    engine._db.mark_grid_order_cancelled = AsyncMock()
    engine._db.get_grid_order = AsyncMock(return_value={"placed_at": 0})
    engine._hub = MagicMock()
    engine._hub.current_operation_id = 7
    engine._next_cloid_for_leg = MagicMock(side_effect=[44, 45, 46, 47])
    engine._posted_grid_signature = sig
    return engine


@pytest.mark.asyncio
async def test_on_grid_fill_buy_extends_buy_band_and_trails_sell_band_up():
    """User spec 2026-05-14: depois de BUY fillar, a banda inteira anda pra cima.
    - Extend buy: nova BUY 1 tick acima do highest existing buy.
    - Trail sell: cancela lowest sell, nova SELL 1 tick acima do highest sell.
    """
    # Setup: 7 buys (one already filled) + 8 sells alive on Lighter
    live_buys = [
        {"side":"buy","cloid":f"b{i}","order_index":100+i,"trigger_price":0.13200+i*0.00013,"size":3.0,"type":"stop-loss"}
        for i in range(7)
    ]
    live_sells = [
        {"side":"sell","cloid":f"s{i}","order_index":200+i,"trigger_price":0.13100-i*0.00013,"size":3.0,"type":"stop-loss"}
        for i in range(8)
    ]
    live = live_buys + live_sells
    engine = _trailing_engine("buy", live_orders=live)

    await engine._on_grid_fill(cloid=42, fill_price=0.132, fill_size=3.0, side="buy")

    # Expect 2 new place_stop_market calls (one buy, one sell)
    calls = engine._exchange.place_stop_market.call_args_list
    sides = [c.kwargs["side"] for c in calls]
    assert sides.count("buy") == 1
    assert sides.count("sell") == 1
    # Buy posted ABOVE existing highest buy (= 0.13200 + 6*0.00013 ≈ 0.13278)
    new_buy = next(c for c in calls if c.kwargs["side"] == "buy")
    highest_buy = max(b["trigger_price"] for b in live_buys)
    assert new_buy.kwargs["trigger_price"] > highest_buy
    # Sell posted ABOVE existing highest sell (= 0.13100 = closest-to-mid)
    new_sell = next(c for c in calls if c.kwargs["side"] == "sell")
    highest_sell = max(s["trigger_price"] for s in live_sells)
    assert new_sell.kwargs["trigger_price"] > highest_sell
    # Lowest sell cancelled
    lowest_sell = min(live_sells, key=lambda o: o["trigger_price"])
    engine._exchange.cancel_stop_order.assert_called_once()
    cc = engine._exchange.cancel_stop_order.call_args
    assert cc.kwargs["order_index"] == lowest_sell["order_index"]


@pytest.mark.asyncio
async def test_on_grid_fill_sell_extends_sell_band_and_trails_buy_band_down():
    """Symmetric: SELL fills → extend sell band DOWN + trail buy band DOWN."""
    live_buys = [
        {"side":"buy","cloid":f"b{i}","order_index":100+i,"trigger_price":0.13200+i*0.00013,"size":3.0,"type":"stop-loss"}
        for i in range(8)
    ]
    live_sells = [
        {"side":"sell","cloid":f"s{i}","order_index":200+i,"trigger_price":0.13100-i*0.00013,"size":3.0,"type":"stop-loss"}
        for i in range(7)
    ]
    live = live_buys + live_sells
    engine = _trailing_engine("sell", live_orders=live)

    await engine._on_grid_fill(cloid=42, fill_price=0.131, fill_size=3.0, side="sell")

    calls = engine._exchange.place_stop_market.call_args_list
    sides = [c.kwargs["side"] for c in calls]
    assert sides.count("sell") == 1
    assert sides.count("buy") == 1
    # Sell posted BELOW lowest existing sell
    new_sell = next(c for c in calls if c.kwargs["side"] == "sell")
    lowest_sell = min(s["trigger_price"] for s in live_sells)
    assert new_sell.kwargs["trigger_price"] < lowest_sell
    # Buy posted BELOW lowest existing buy
    new_buy = next(c for c in calls if c.kwargs["side"] == "buy")
    lowest_buy = min(b["trigger_price"] for b in live_buys)
    assert new_buy.kwargs["trigger_price"] < lowest_buy
    # Highest buy cancelled
    highest_buy = max(live_buys, key=lambda o: o["trigger_price"])
    engine._exchange.cancel_stop_order.assert_called_once()
    cc = engine._exchange.cancel_stop_order.call_args
    assert cc.kwargs["order_index"] == highest_buy["order_index"]


@pytest.mark.asyncio
async def test_on_grid_fill_skips_post_when_new_tick_outside_beefy_range():
    """Trailing pode tentar postar fora do range Beefy ativo. Nesse caso,
    skipa o post mas continua tentando a outra ação. _maintain_grid full
    rebuild vai recompor quando Beefy rebalançar."""
    # Tight signature range; highest buy already AT tick_upper edge
    sig = (int(1e15), -296000, -295000)  # narrow Beefy range
    live_buys = [
        {"side":"buy","cloid":"b0","order_index":100,"trigger_price":0.14185,"size":3.0,"type":"stop-loss"},  # near upper edge
    ]
    live_sells = [
        {"side":"sell","cloid":"s0","order_index":200,"trigger_price":0.13800,"size":3.0,"type":"stop-loss"},
    ]
    engine = _trailing_engine("buy", live_orders=live_buys + live_sells, sig=sig)

    await engine._on_grid_fill(cloid=42, fill_price=0.142, fill_size=3.0, side="buy")

    # Extending the buy band upward would post beyond tick_upper=-295000 → skip.
    # Trailing sell band upward also lands outside range → also skipped.
    # Net: zero new posts.
    calls = engine._exchange.place_stop_market.call_args_list
    posts_outside = [
        c for c in calls
        # Both sides would be above tick_upper ≈ -295000 (human ~$0.1505)
        if c.kwargs.get("trigger_price", 0) > 0.1505
    ]
    assert posts_outside == [], f"unexpected post outside range: {posts_outside}"


# ---------------------------------------------------------------------------
# Reconciler skipped under predictive_grid_v2 + drift_correction guards
# (regression 2026-05-14: reconciler false-cancelled live SL_MARKETs as
# "orphans" → drift_correction fired SELL against pos=0 reading from a
# WS-drop → cascade over-hedge.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_maybe_reconcile_skipped_when_predictive_grid_v2():
    """Reconciler must NOT run under v2 — its 'cancel orphan + mark lost'
    logic was designed for dYdX limit orders and destroys SL_MARKET grids.
    """
    from engine import GridMakerEngine
    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.predictive_grid_v2 = True
    engine._iter_count = 0
    GridMakerEngine.RECONCILE_EVERY_N_ITERATIONS = 1
    engine._reconciler = MagicMock()
    engine._reconciler.reconcile = AsyncMock()
    engine._exchange = MagicMock()
    await engine._maybe_reconcile()
    engine._reconciler.reconcile.assert_not_called()


@pytest.mark.asyncio
async def test_drift_correction_skips_when_pos_is_None():
    """WS drops levam pos=None transiente. Skip drift fire (não shortear
    contra reading falso)."""
    from engine import GridMakerEngine
    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.min_rebalance_notional_usd = 1.0
    engine._exchange = AsyncMock()
    engine._exchange.place_long_term_order = AsyncMock()
    engine._db = AsyncMock()
    engine._hub = MagicMock()
    engine._next_cloid_for_leg = MagicMock(return_value=1)

    await engine._maybe_correct_drift(
        beefy_pos=MagicMock(), p_now=0.13,
        positions=[None],   # ← unreliable read
        symbols=["ARB-USD"],
        targets={"ARB-USD": 500.0},
    )
    engine._exchange.place_long_term_order.assert_not_called()


@pytest.mark.asyncio
async def test_drift_correction_skips_when_pos_size_zero_and_target_nonzero():
    """pos.size=0 + target>0 = leitura suspeita. Skip pra não shortar do zero."""
    from engine import GridMakerEngine
    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.min_rebalance_notional_usd = 1.0
    engine._exchange = AsyncMock()
    engine._exchange.place_long_term_order = AsyncMock()
    engine._db = AsyncMock()
    engine._hub = MagicMock()
    engine._next_cloid_for_leg = MagicMock(return_value=1)

    pos0 = MagicMock()
    pos0.size = 0.0
    await engine._maybe_correct_drift(
        beefy_pos=MagicMock(), p_now=0.13,
        positions=[pos0],
        symbols=["ARB-USD"],
        targets={"ARB-USD": 500.0},  # we expect a position but read says 0
    )
    engine._exchange.place_long_term_order.assert_not_called()


@pytest.mark.asyncio
async def test_drift_correction_fires_when_pos_valid_and_drift_above_threshold():
    """Sanity: quando reading é confiável (pos.size > 0) e drift > $1, dispara."""
    from engine import GridMakerEngine
    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.min_rebalance_notional_usd = 1.0
    engine._exchange = AsyncMock()
    engine._exchange.place_long_term_order = AsyncMock()
    engine._exchange.name = "lighter"
    engine._db = AsyncMock()
    engine._db.insert_order_log = AsyncMock()
    engine._hub = MagicMock()
    engine._hub.current_operation_id = 7
    engine._hub.dydx_quote_prices = {"ARB-USD": 0.13}
    engine._next_cloid_for_leg = MagicMock(return_value=1)

    pos0 = MagicMock()
    pos0.size = 493.0
    await engine._maybe_correct_drift(
        beefy_pos=MagicMock(), p_now=0.13,
        positions=[pos0],
        symbols=["ARB-USD"],
        targets={"ARB-USD": 520.0},  # drift = 27 ARB * $0.13 = $3.5 > $1
    )
    engine._exchange.place_long_term_order.assert_called_once()
    call = engine._exchange.place_long_term_order.call_args
    assert call.kwargs["side"] == "sell"
    assert call.kwargs["size"] == pytest.approx(27.0)
