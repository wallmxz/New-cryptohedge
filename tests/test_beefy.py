import pytest
from unittest.mock import AsyncMock, MagicMock
from chains.beefy import BeefyClmReader, BeefyPosition


@pytest.mark.asyncio
async def test_read_position_returns_struct():
    """Mocked strategy: returns expected position struct."""
    strategy = MagicMock()
    strategy.functions.range.return_value.call = AsyncMock(return_value=(80000, 90000))
    strategy.functions.balances.return_value.call = AsyncMock(
        return_value=(int(0.5 * 10**18), int(1500 * 10**6))
    )
    strategy.functions.totalSupply.return_value.call = AsyncMock(
        return_value=int(100 * 10**18)
    )
    strategy.functions.balanceOf.return_value.call = AsyncMock(
        return_value=int(1 * 10**18)
    )

    w3 = MagicMock()
    w3.eth.contract.return_value = strategy
    w3.to_checksum_address = lambda a: a

    reader = BeefyClmReader(
        w3=w3,
        strategy_address="0xstrategy",
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
    strategy = MagicMock()
    strategy.functions.range.return_value.call = AsyncMock(return_value=(80000, 90000))
    strategy.functions.balances.return_value.call = AsyncMock(return_value=(0, 0))
    strategy.functions.totalSupply.return_value.call = AsyncMock(return_value=0)
    strategy.functions.balanceOf.return_value.call = AsyncMock(return_value=0)

    w3 = MagicMock()
    w3.eth.contract.return_value = strategy
    w3.to_checksum_address = lambda a: a

    reader = BeefyClmReader(
        w3=w3, strategy_address="0x0", wallet_address="0x0",
        decimals0=18, decimals1=6,
    )
    pos = await reader.read_position()
    assert pos.share == 0.0
    assert pos.amount0 == 0.0
    assert pos.amount1 == 0.0


@pytest.mark.asyncio
async def test_read_position_negative_ticks():
    """Beefy ranges may have negative tick bounds (e.g., for low-USD-priced pools)."""
    strategy = MagicMock()
    strategy.functions.range.return_value.call = AsyncMock(return_value=(-887220, -880000))
    strategy.functions.balances.return_value.call = AsyncMock(
        return_value=(int(0.1 * 10**18), int(100 * 10**6))
    )
    strategy.functions.totalSupply.return_value.call = AsyncMock(return_value=int(50 * 10**18))
    strategy.functions.balanceOf.return_value.call = AsyncMock(return_value=int(5 * 10**18))

    w3 = MagicMock()
    w3.eth.contract.return_value = strategy
    w3.to_checksum_address = lambda a: a

    reader = BeefyClmReader(
        w3=w3, strategy_address="0x0", wallet_address="0x0",
        decimals0=18, decimals1=6,
    )
    pos = await reader.read_position()
    assert pos.tick_lower == -887220
    assert pos.tick_upper == -880000
    assert abs(pos.share - 0.1) < 1e-9  # 5 of 50
