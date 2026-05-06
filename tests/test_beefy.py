import pytest
from unittest.mock import AsyncMock, MagicMock
from chains.beefy import BeefyClmReader, BeefyPosition


def _build_mocks(*, ticks, balances, total_supply, balance):
    """Build separate strategy + earn contract mocks.

    Strategy exposes `positionMain()` and `balances()`.
    Earn exposes `totalSupply()` and `balanceOf(addr)`.
    """
    strategy = MagicMock()
    strategy.functions.positionMain.return_value.call = AsyncMock(return_value=ticks)
    strategy.functions.balances.return_value.call = AsyncMock(return_value=balances)

    earn = MagicMock()
    earn.functions.totalSupply.return_value.call = AsyncMock(return_value=total_supply)
    earn.functions.balanceOf.return_value.call = AsyncMock(return_value=balance)

    w3 = MagicMock()
    # Two contract instances are constructed (strategy first, earn second);
    # have w3.eth.contract return them in that order.
    w3.eth.contract = MagicMock(side_effect=[strategy, earn])
    w3.to_checksum_address = lambda a: a
    return w3, strategy, earn


@pytest.mark.asyncio
async def test_read_position_returns_struct():
    """Mocked strategy + earn: returns expected position struct."""
    w3, _strategy, _earn = _build_mocks(
        ticks=(80000, 90000),
        balances=(int(0.5 * 10**18), int(1500 * 10**6)),
        total_supply=int(100 * 10**18),
        balance=int(1 * 10**18),
    )

    reader = BeefyClmReader(
        w3=w3,
        strategy_address="0xstrategy",
        earn_address="0xearn",
        wallet_address="0xwallet",
        decimals0=18,
        decimals1=6,
    )
    pos = await reader.read_position()
    assert isinstance(pos, BeefyPosition)
    assert pos.tick_lower == 80000
    assert pos.tick_upper == 90000
    assert abs(pos.amount0 - 0.5) < 1e-9
    assert abs(pos.amount1 - 1500.0) < 1e-9
    assert abs(pos.share - 0.01) < 1e-9  # 1 of 100


@pytest.mark.asyncio
async def test_read_position_zero_total_supply():
    """When totalSupply is 0, share should be 0.0 (no division-by-zero)."""
    w3, _, _ = _build_mocks(
        ticks=(80000, 90000),
        balances=(0, 0),
        total_supply=0,
        balance=0,
    )

    reader = BeefyClmReader(
        w3=w3, strategy_address="0xstrategy", earn_address="0xearn",
        wallet_address="0x0",
        decimals0=18, decimals1=6,
    )
    pos = await reader.read_position()
    assert pos.share == 0.0
    assert pos.amount0 == 0.0
    assert pos.amount1 == 0.0


@pytest.mark.asyncio
async def test_read_position_negative_ticks():
    """Beefy ranges may have negative tick bounds (e.g., for low-USD-priced pools)."""
    w3, _, _ = _build_mocks(
        ticks=(-887220, -880000),
        balances=(int(0.1 * 10**18), int(100 * 10**6)),
        total_supply=int(50 * 10**18),
        balance=int(5 * 10**18),
    )

    reader = BeefyClmReader(
        w3=w3, strategy_address="0xstrategy", earn_address="0xearn",
        wallet_address="0x0",
        decimals0=18, decimals1=6,
    )
    pos = await reader.read_position()
    assert pos.tick_lower == -887220
    assert pos.tick_upper == -880000
    assert abs(pos.share - 0.1) < 1e-9  # 5 of 50
