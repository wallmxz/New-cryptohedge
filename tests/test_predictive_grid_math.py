import pytest
from math import isclose
from engine.curve import tick_to_human_price


def test_tick_to_human_price_arb_usdce_at_zero_tick():
    """Tick 0 → raw price = 1.0, ajustado pelos decimais.
    Para ARB(18)/USDC.e(6) onde ARB é token0:
    human = 1.0 * 10^(18-6) = 1e12. (preço raw é absurdo, mas matemática
    é consistente com Uniswap V3.)"""
    p = tick_to_human_price(tick=0, decimals0=18, decimals1=6)
    assert isclose(p, 1e12, rel_tol=1e-9)


def test_tick_to_human_price_arb_usdce_at_realistic_tick():
    """ARB a ~$0.14 em USDC.e: tick ~ ?
    1.0001^t * 10^12 = 0.14  →  t = log(0.14 / 1e12) / log(1.0001)
    t = log(1.4e-13) / log(1.0001) ≈ -296160
    """
    target_price = 0.14
    from math import log
    expected_tick = int(log(target_price / 1e12) / log(1.0001))
    p = tick_to_human_price(
        tick=expected_tick, decimals0=18, decimals1=6,
    )
    # Tolerância: tick é int, então perde precisão (~0.01%)
    assert isclose(p, target_price, rel_tol=1e-3)


def test_tick_to_human_price_monotonic():
    """Tick maior → preço maior."""
    p_low = tick_to_human_price(tick=-296200, decimals0=18, decimals1=6)
    p_high = tick_to_human_price(tick=-296100, decimals0=18, decimals1=6)
    assert p_high > p_low


from engine.curve import compute_grid_from_pool_ticks


def test_compute_grid_minimal_range():
    """Range muito estreito com 1 nível acima + 1 abaixo do tick_now.
    Verifica que gera exatamente 2 levels (um buy, um sell), spacing respeitado.

    Setup: tick_lower=-296200, tick_now=-296100, tick_upper=-296000, spacing=100
    Ticks alinhados < tick_now: -296200 → 1 sell
    Ticks alinhados > tick_now: -296000 → 1 buy
    tick_now skipped (mid)
    """
    L = 1e15
    grid = compute_grid_from_pool_ticks(
        L=L,
        tick_lower=-296200,
        tick_upper=-296000,
        tick_spacing=100,
        tick_now=-296100,
        decimals0=18,
        decimals1=6,
        hedge_ratio=1.0,
        lighter_price_decimals=5,
        lighter_size_decimals=1,
    )
    assert len(grid) == 2
    sells = [lv for lv in grid if lv.side == "sell"]
    buys = [lv for lv in grid if lv.side == "buy"]
    assert len(sells) == 1
    assert len(buys) == 1
    # Sell tem preço menor que buy
    assert sells[0].price < buys[0].price


def test_compute_grid_returns_empty_when_L_zero():
    grid = compute_grid_from_pool_ticks(
        L=0.0, tick_lower=-296200, tick_upper=-296000,
        tick_spacing=100, tick_now=-296100,
        decimals0=18, decimals1=6, hedge_ratio=1.0,
        lighter_price_decimals=5, lighter_size_decimals=1,
    )
    assert grid == []


def test_compute_grid_returns_empty_when_inverted_range():
    grid = compute_grid_from_pool_ticks(
        L=1e15, tick_lower=-296000, tick_upper=-296200,  # inverted
        tick_spacing=100, tick_now=-296100,
        decimals0=18, decimals1=6, hedge_ratio=1.0,
        lighter_price_decimals=5, lighter_size_decimals=1,
    )
    assert grid == []


def test_compute_grid_with_tick_now_above_range():
    """Se tick_now > tick_upper, todos os ticks ficam abaixo → all sells."""
    grid = compute_grid_from_pool_ticks(
        L=1e15, tick_lower=-296200, tick_upper=-296000,
        tick_spacing=100, tick_now=-295900,  # acima do range
        decimals0=18, decimals1=6, hedge_ratio=1.0,
        lighter_price_decimals=5, lighter_size_decimals=1,
    )
    # Todos abaixo de tick_now → all sells. Garantir não-vazio
    # pra prevenir regressão silenciosa do loop DOWN.
    assert len(grid) > 0
    assert all(lv.side == "sell" for lv in grid)


def test_compute_grid_with_tick_now_below_range():
    """tick_now < tick_lower, todos os ticks ficam acima → all buys."""
    grid = compute_grid_from_pool_ticks(
        L=1e15, tick_lower=-296200, tick_upper=-296000,
        tick_spacing=100, tick_now=-296300,  # abaixo
        decimals0=18, decimals1=6, hedge_ratio=1.0,
        lighter_price_decimals=5, lighter_size_decimals=1,
    )
    # Garantir não-vazio pra prevenir regressão silenciosa do loop UP.
    assert len(grid) > 0
    assert all(lv.side == "buy" for lv in grid)


def test_compute_grid_returns_empty_when_hedge_ratio_zero():
    """hedge_ratio=0 → size=0 em todos os níveis → grade vazia.
    Comportamento defensivo: usuário desligou hedge, bot não posta nada."""
    grid = compute_grid_from_pool_ticks(
        L=1e15, tick_lower=-296200, tick_upper=-296000,
        tick_spacing=100, tick_now=-296100,
        decimals0=18, decimals1=6, hedge_ratio=0.0,
        lighter_price_decimals=5, lighter_size_decimals=1,
    )
    assert grid == []


def test_compute_grid_sizes_conserve_delta_x():
    """Soma dos sizes (sem hedge_ratio) deve igualar x_at_tick_lower
    (porque ticks vão de tick_lower a tick_upper cobrindo toda a curva).
    """
    from engine.curve import compute_x
    L = 1e15
    tick_lower, tick_upper = -296200, -296000
    decimals0, decimals1 = 18, 6
    grid = compute_grid_from_pool_ticks(
        L=L, tick_lower=tick_lower, tick_upper=tick_upper,
        tick_spacing=10, tick_now=-296100,
        decimals0=decimals0, decimals1=decimals1, hedge_ratio=1.0,
        lighter_price_decimals=5, lighter_size_decimals=1,
    )
    price_lower = tick_to_human_price(
        tick=tick_lower, decimals0=decimals0, decimals1=decimals1,
    )
    price_upper = tick_to_human_price(
        tick=tick_upper, decimals0=decimals0, decimals1=decimals1,
    )
    expected_total_x = compute_x(L, price_lower, price_upper)
    actual_total_x = sum(lv.size for lv in grid)
    # Tolerância por rounding step da Lighter:
    assert abs(actual_total_x - expected_total_x) / expected_total_x < 0.05


def test_compute_grid_uniform_level_sizes_when_tick_now_misaligned():
    """Regression 2026-05-14: usuário viu 1 ordem com 0.9 ARB e as outras
    com 3.0 — o "nível parcial" da fórmula anterior usava x_at_tick_now
    como referência, fazendo o primeiro level cobrir menos que 1 spacing
    (delta proporcional ao gap entre tick_now e o primeiro aligned tick).

    Fix: snap o reference de prev_x ao tick aligned vizinho. Todos os
    levels cobrem exatamente 1 tick_spacing de V3 amount delta → sizes
    uniformes. Drift correction compensa qualquer over/under-shoot
    transitório no primeiro fill."""
    L = 1e15
    # Pick a tick_now MISALIGNED (entre tick boundaries) pra exercitar o caso problemático
    # Spacing 100. tick_now = -296157 (não múltiplo de 100).
    grid = compute_grid_from_pool_ticks(
        L=L,
        tick_lower=-297000,
        tick_upper=-295000,
        tick_spacing=100,
        tick_now=-296157,  # misaligned: 43 ticks acima de -296200 (sell side)
        decimals0=18,
        decimals1=6,
        hedge_ratio=1.0,
        lighter_price_decimals=5,
        lighter_size_decimals=1,
    )
    assert len(grid) > 0
    sizes = [lv.size for lv in grid if lv.size > 0]
    # Sizes devem ser todos próximos do mesmo valor (não 30% / 70% / etc.)
    # Permite variação até 20% por rounding (steps de 0.1 ARB pra ARB)
    smin, smax = min(sizes), max(sizes)
    ratio = smax / smin if smin > 0 else float("inf")
    assert ratio < 1.2, (
        f"size variation too high: min={smin}, max={smax}, ratio={ratio:.2f}"
    )
