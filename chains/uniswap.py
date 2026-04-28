from __future__ import annotations
import json
from pathlib import Path
from web3 import AsyncWeb3


_ABI_PATH = Path(__file__).parent.parent / "abi" / "uniswap_v3_pool.json"
with open(_ABI_PATH) as f:
    POOL_ABI = json.load(f)

Q96 = 2**96


def sqrt_price_x96_to_price(sqrt_price_x96: int, decimals0: int, decimals1: int) -> float:
    """Convert Uniswap V3 sqrtPriceX96 to display price token1/token0.

    Result is adjusted for token decimals: price units = display units of token1 per token0.
    For (token0=WETH 18 decimals, token1=USDC 6 decimals): price = USD per ETH.
    """
    p_raw = (sqrt_price_x96 / Q96) ** 2
    return p_raw * (10 ** decimals0) / (10 ** decimals1)


def tick_to_price(tick: int, decimals0: int, decimals1: int) -> float:
    """Convert tick to display price (token1/token0)."""
    p_raw = 1.0001 ** tick
    return p_raw * (10 ** decimals0) / (10 ** decimals1)


class UniswapV3PoolReader:
    def __init__(self, w3: AsyncWeb3, pool_address: str, decimals0: int, decimals1: int):
        self._w3 = w3
        self._pool_address = pool_address
        self._decimals0 = decimals0
        self._decimals1 = decimals1
        self._contract = w3.eth.contract(
            address=w3.to_checksum_address(pool_address), abi=POOL_ABI,
        )

    async def read_slot0(self) -> tuple[int, int]:
        """Returns (sqrtPriceX96, tick)."""
        slot0 = await self._contract.functions.slot0().call()
        return slot0[0], slot0[1]

    async def read_price(self) -> float:
        """Returns display price (token1/token0)."""
        sqrt_p, _ = await self.read_slot0()
        return sqrt_price_x96_to_price(sqrt_p, self._decimals0, self._decimals1)
