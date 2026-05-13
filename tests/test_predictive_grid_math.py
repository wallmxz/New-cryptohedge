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
