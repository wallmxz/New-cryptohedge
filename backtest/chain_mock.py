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
    """Replaces BeefyClmReader. Two modes:
    - Static (legacy): set_position() locks in fixed amounts.
    - Dynamic (cross-pair): configure() + set_p() re-derives amounts from V3 curve.
    """

    def __init__(self):
        self._pos: _BeefyPosition | None = None
        # Dynamic mode (V3 curve-driven)
        self._p_a: float | None = None
        self._p_b: float | None = None
        self._L: float | None = None
        self._share: float = 1.0
        self._tick_lower: int = 0
        self._tick_upper: int = 0
        self._p_now: float | None = None

    def configure(self, *, p_a: float, p_b: float, L: float, share: float,
                  tick_lower: int, tick_upper: int) -> None:
        """Switch to dynamic mode: amounts re-derived via V3 curve as set_p() updates."""
        self._p_a, self._p_b = p_a, p_b
        self._L = L
        self._share = share
        self._tick_lower = tick_lower
        self._tick_upper = tick_upper
        self._pos = None  # disable static mode

    def set_p(self, p_now: float) -> None:
        self._p_now = p_now

    def set_position(self, *, tick_lower: int, tick_upper: int,
                     amount0: float, amount1: float, share: float,
                     raw_balance: int) -> None:
        """Legacy static mode."""
        self._pos = _BeefyPosition(
            tick_lower=tick_lower, tick_upper=tick_upper,
            amount0=amount0, amount1=amount1,
            share=share, raw_balance=raw_balance,
        )

    async def read_position(self) -> _BeefyPosition:
        # Dynamic mode wins if configured
        if self._L is not None and self._p_now is not None:
            from engine.curve import compute_x, compute_y
            if self._p_now <= self._p_a:
                amount0 = compute_x(self._L, self._p_a, self._p_b)
                amount1 = 0.0
            elif self._p_now >= self._p_b:
                amount0 = 0.0
                amount1 = compute_y(self._L, self._p_b, self._p_a)
            else:
                amount0 = compute_x(self._L, self._p_now, self._p_b)
                amount1 = compute_y(self._L, self._p_now, self._p_a)
            return _BeefyPosition(
                tick_lower=self._tick_lower, tick_upper=self._tick_upper,
                amount0=amount0, amount1=amount1,
                share=self._share, raw_balance=10**18,
            )

        if self._pos is None:
            raise RuntimeError("MockBeefyReader: position not set")
        return self._pos
