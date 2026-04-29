"""In-memory pool/Beefy readers driven by simulator. Replace web3 calls."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class _BeefyPosition:
    tick_lower: int
    tick_upper: int
    amount0: float
    amount1: float
    share: float
    raw_balance: int


class MockPoolReader:
    """Replaces UniswapV3PoolReader. set_price() drives current value."""

    def __init__(self):
        self._price: float = 0.0

    def set_price(self, price: float) -> None:
        self._price = price

    async def read_price(self) -> float:
        return self._price

    async def read_slot0(self) -> tuple[int, int]:
        # Stub: returns synthetic (sqrtPriceX96, tick) value, real reader queries pool.slot0().
        # GridMakerEngine only uses read_price(), but kept for interface parity.
        return (int((self._price ** 0.5) * (2**96)), 0)


class MockBeefyReader:
    """Replaces BeefyClmReader. set_position() drives current state."""

    def __init__(self):
        self._pos: _BeefyPosition | None = None

    def set_position(self, *, tick_lower: int, tick_upper: int,
                     amount0: float, amount1: float, share: float,
                     raw_balance: int) -> None:
        self._pos = _BeefyPosition(
            tick_lower=tick_lower, tick_upper=tick_upper,
            amount0=amount0, amount1=amount1,
            share=share, raw_balance=raw_balance,
        )

    async def read_position(self) -> _BeefyPosition:
        if self._pos is None:
            raise RuntimeError("MockBeefyReader: position not set")
        return self._pos
