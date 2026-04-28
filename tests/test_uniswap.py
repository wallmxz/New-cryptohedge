import pytest
from unittest.mock import AsyncMock, MagicMock
from chains.uniswap import sqrt_price_x96_to_price, tick_to_price, UniswapV3PoolReader


def test_sqrt_price_x96_to_price_eth_usdc():
    """At sqrtPriceX96 corresponding to ETH=$3000 with USDC 6 decimals, WETH 18.

    For pool (token0=USDC, token1=WETH), price token1/token0 = WETH/USDC.
    For our use case (we want USD per ETH), depends on token order.
    """
    # Test the raw math: sqrtPriceX96 = sqrt(p) * 2**96 where p = token1/token0
    # If we pass sqrtPriceX96 for p=1.0, we should get back 1.0
    Q96 = 2**96
    sqrt_p = 1.0
    sqrt_price_x96 = int(sqrt_p * Q96)
    price = sqrt_price_x96_to_price(sqrt_price_x96, decimals0=18, decimals1=18)
    assert abs(price - 1.0) < 1e-9


def test_tick_to_price():
    """Tick 0 = price 1.0 (raw); tick 60 = price ~1.006."""
    assert abs(tick_to_price(0, decimals0=18, decimals1=18) - 1.0) < 1e-9
    p60 = tick_to_price(60, decimals0=18, decimals1=18)
    assert abs(p60 - 1.0001**60) < 1e-6


@pytest.mark.asyncio
async def test_pool_reader_slot0(monkeypatch):
    """Mock web3 contract; reader returns sqrt price + tick."""
    fake_slot0 = (int(1.0 * 2**96), 0, 0, 0, 0, 0, True)
    contract = MagicMock()
    contract.functions.slot0.return_value.call = AsyncMock(return_value=fake_slot0)

    w3 = MagicMock()
    w3.eth.contract.return_value = contract
    w3.to_checksum_address = lambda a: a

    reader = UniswapV3PoolReader(w3, "0xpool", decimals0=18, decimals1=18)
    sqrt_p, tick = await reader.read_slot0()
    assert sqrt_p == int(1.0 * 2**96)
    assert tick == 0
