"""MockExchangeAdapter multi-symbol: positions per symbol, get_oracle_prices."""
import pytest
from backtest.exchange_mock import MockExchangeAdapter


@pytest.mark.asyncio
async def test_multi_symbol_positions_independent():
    ex = MockExchangeAdapter(symbols=["ARB-USD", "ETH-USD"])
    await ex.connect()
    ex._collateral = 200.0

    # Place ARB short
    await ex.place_long_term_order(
        symbol="ARB-USD", side="sell", size=10.0, price=1.50, cloid_int=1,
    )
    await ex.advance_to_prices({"ARB-USD": 1.50, "ETH-USD": 4000.0}, ts=0)
    pos_arb = await ex.get_position("ARB-USD")
    pos_eth = await ex.get_position("ETH-USD")
    assert pos_arb is not None
    assert pos_arb.size == 10.0
    assert pos_eth is None  # no ETH order yet

    # Place ETH short
    await ex.place_long_term_order(
        symbol="ETH-USD", side="sell", size=0.05, price=4000.0, cloid_int=2,
    )
    await ex.advance_to_prices({"ARB-USD": 1.50, "ETH-USD": 4000.0}, ts=1)
    pos_eth = await ex.get_position("ETH-USD")
    assert pos_eth is not None
    assert pos_eth.size == 0.05


@pytest.mark.asyncio
async def test_get_oracle_prices_returns_last_prices():
    ex = MockExchangeAdapter(symbols=["ARB-USD", "ETH-USD"])
    await ex.connect()
    await ex.advance_to_prices({"ARB-USD": 1.55, "ETH-USD": 4200.0}, ts=10)
    prices = await ex.get_oracle_prices(["ARB-USD", "ETH-USD"])
    assert prices == {"ARB-USD": 1.55, "ETH-USD": 4200.0}


@pytest.mark.asyncio
async def test_margin_gate_aggregates_both_legs():
    """Combined notional across both legs is checked vs collateral × 5x."""
    ex = MockExchangeAdapter(symbols=["ARB-USD", "ETH-USD"])
    await ex.connect()
    ex._collateral = 100.0  # 5x = $500 max combined notional

    await ex.advance_to_prices({"ARB-USD": 1.50, "ETH-USD": 4000.0}, ts=0)
    # ARB short of 100 ARB = $150 notional. Then ETH short of 0.1 ETH = $400 → total $550 > $500 cap
    await ex.place_long_term_order(
        symbol="ARB-USD", side="sell", size=100.0, price=1.50, cloid_int=10,
    )
    await ex.advance_to_prices({"ARB-USD": 1.50, "ETH-USD": 4000.0}, ts=1)
    # First short fills, position = 100 ARB. notional = $150.

    with pytest.raises(ValueError, match="Margin insufficient"):
        await ex.place_long_term_order(
            symbol="ETH-USD", side="sell", size=0.1, price=4000.0, cloid_int=11,
        )


@pytest.mark.asyncio
async def test_single_symbol_backwards_compat():
    """Default constructor still accepts single `symbol=` kwarg."""
    ex = MockExchangeAdapter(symbol="ETH-USD")
    await ex.connect()
    await ex.place_long_term_order(
        symbol="ETH-USD", side="sell", size=0.05, price=4000.0, cloid_int=1,
    )
    await ex.advance_to_price(4000.0, ts=0)
    pos = await ex.get_position("ETH-USD")
    assert pos is not None and pos.size == 0.05


@pytest.mark.asyncio
async def test_apply_funding_requires_symbol_in_multi_symbol_mode():
    """Silent fallback in dual-leg would lose the second leg's funding."""
    ex = MockExchangeAdapter(symbols=["ARB-USD", "ETH-USD"])
    await ex.connect()
    with pytest.raises(ValueError, match="explicit `symbol`"):
        ex.apply_funding(0.0001, ts=0.0)  # no symbol arg

    # Single-symbol still works without symbol arg
    ex_single = MockExchangeAdapter(symbol="ETH-USD")
    await ex_single.connect()
    ex_single.apply_funding(0.0001, ts=0.0)  # OK
