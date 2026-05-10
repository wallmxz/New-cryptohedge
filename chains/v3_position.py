"""V3PositionReader — reads positionMain/positionAlt liquidity directly
from Uniswap V3 pool storage, NOT derived from Beefy aggregate balances.

This avoids the v1 bug where compute_l_from_value derived a single L
from total strategy holdings (positionMain + positionAlt + idle + fees),
inflating L by ~3x and producing wrong predicted amounts.

Spec: docs/superpowers/specs/2026-05-10-predictive-hedge-model-design.md
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from web3 import AsyncWeb3

# Reuse the pool ABI (now extended with positions(bytes32) — Task 1)
_POOL_ABI_PATH = Path(__file__).parent.parent / "abi" / "uniswap_v3_pool.json"
with open(_POOL_ABI_PATH) as f:
    _POOL_ABI = json.load(f)

_STRATEGY_ABI_PATH = Path(__file__).parent.parent / "abi" / "beefy_clm_strategy.json"
with open(_STRATEGY_ABI_PATH) as f:
    _STRATEGY_ABI = json.load(f)


@dataclass
class V3Position:
    liquidity: int
    tick_lower: int
    tick_upper: int


class V3PositionReader:
    """Reads positionMain + positionAlt liquidity directly from the
    Uniswap V3 pool, using `pool.positions(keccak(owner, lower, upper))`."""

    def __init__(self, w3: AsyncWeb3, pool_address: str, beefy_strategy_address: str):
        self._w3 = w3
        self._pool = w3.eth.contract(
            address=w3.to_checksum_address(pool_address),
            abi=_POOL_ABI,
        )
        self._strategy = w3.eth.contract(
            address=w3.to_checksum_address(beefy_strategy_address),
            abi=_STRATEGY_ABI,
        )

    async def read_position_main(self) -> V3Position:
        """Reads (tickLower, tickUpper) from Beefy strategy.positionMain(),
        then queries pool.positions(key) for L. Raises on RPC failure
        — caller (HedgeModel.refresh_cache) handles."""
        main_range = await self._strategy.functions.positionMain().call()
        tick_lower = int(main_range[0])
        tick_upper = int(main_range[1])
        return await self._read_v3_position_at(tick_lower, tick_upper)

    async def read_position_alt(self) -> V3Position | None:
        """Reads positionAlt range. Returns None on:
        - inactive alt sentinel (tick_lower == tick_upper, including (0,0))
        - any RPC failure (positionAlt method may not exist on older strategies)
        """
        try:
            alt_range = await self._strategy.functions.positionAlt().call()
            tick_lower = int(alt_range[0])
            tick_upper = int(alt_range[1])
            if tick_lower == tick_upper:
                return None
            return await self._read_v3_position_at(tick_lower, tick_upper)
        except Exception:
            return None

    async def _read_v3_position_at(self, tick_lower: int, tick_upper: int) -> V3Position:
        position_key = self._compute_position_key(tick_lower, tick_upper)
        result = await self._pool.functions.positions(position_key).call()
        liquidity = int(result[0])
        return V3Position(
            liquidity=liquidity,
            tick_lower=tick_lower,
            tick_upper=tick_upper,
        )

    def _compute_position_key(self, tick_lower: int, tick_upper: int) -> bytes:
        """Computes the V3 position key as keccak256(abi.encodePacked(
        owner, int24(tickLower), int24(tickUpper))). Matches Uniswap V3
        Position library's keccak hashing convention."""
        return self._w3.solidity_keccak(
            ["address", "int24", "int24"],
            [self._strategy.address, tick_lower, tick_upper],
        )
