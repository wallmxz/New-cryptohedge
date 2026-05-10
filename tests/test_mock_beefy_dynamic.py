"""MockBeefyReader: dynamic rebalance via V3 curve as p moves."""
import pytest
from backtest.chain_mock import MockBeefyReader


@pytest.mark.asyncio
async def test_set_p_changes_amounts_via_curve():
    """As p moves up, x decreases and y increases (V3 invariant)."""
    reader = MockBeefyReader()
    reader.configure(
        p_a=0.0003, p_b=0.0005,
        L=10000.0, share=1.0,
        tick_lower=-201386, tick_upper=-198363,
    )
    reader.set_p(0.0004)
    pos1 = await reader.read_position()

    reader.set_p(0.00045)  # p went up
    pos2 = await reader.read_position()

    assert pos2.amount0 < pos1.amount0  # less ARB
    assert pos2.amount1 > pos1.amount1  # more WETH


@pytest.mark.asyncio
async def test_out_of_range_returns_one_token():
    reader = MockBeefyReader()
    reader.configure(
        p_a=0.0003, p_b=0.0005, L=10000.0, share=1.0,
        tick_lower=-201386, tick_upper=-198363,
    )
    reader.set_p(0.00029)  # below p_a
    pos = await reader.read_position()
    # 100% in token0 (ARB)
    assert pos.amount0 > 0
    assert pos.amount1 == 0


@pytest.mark.asyncio
async def test_legacy_set_position_still_works():
    """Backwards compat for existing single-leg backtest."""
    reader = MockBeefyReader()
    reader.set_position(
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.05, amount1=200.0,
        share=1.0, raw_balance=10**18,
    )
    pos = await reader.read_position()
    assert pos.amount0 == 0.05
    assert pos.amount1 == 200.0
