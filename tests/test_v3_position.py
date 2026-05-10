"""Tests for chains/v3_position.py — reads positionMain/positionAlt
liquidity directly from Uniswap V3 pool storage."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from chains.v3_position import V3Position, V3PositionReader


def _make_reader(positions_return=(123456789, 0, 0, 0, 0),
                 main_range=(96040, 97540),
                 alt_range=None,
                 alt_raises=False):
    """Build a V3PositionReader with mocked w3, pool, strategy contracts."""
    w3 = MagicMock()
    w3.to_checksum_address = lambda a: a
    w3.solidity_keccak = lambda types, vals: b"\xab" * 32  # deterministic key

    pool_positions_call = AsyncMock(return_value=positions_return)
    pool_contract = MagicMock()
    pool_contract.functions.positions.return_value.call = pool_positions_call

    strategy_main_call = AsyncMock(return_value=main_range)
    if alt_raises:
        strategy_alt_call = AsyncMock(side_effect=Exception("alt not active"))
    elif alt_range is None:
        strategy_alt_call = AsyncMock(return_value=(0, 0))  # inactive sentinel
    else:
        strategy_alt_call = AsyncMock(return_value=alt_range)

    strategy_contract = MagicMock()
    strategy_contract.functions.positionMain.return_value.call = strategy_main_call
    strategy_contract.functions.positionAlt.return_value.call = strategy_alt_call
    strategy_contract.address = "0xSTRATEGY"

    w3.eth.contract = MagicMock(side_effect=[pool_contract, strategy_contract])

    reader = V3PositionReader(
        w3=w3,
        pool_address="0xPOOL",
        beefy_strategy_address="0xSTRATEGY",
    )
    return reader, pool_contract, strategy_contract


@pytest.mark.asyncio
async def test_compute_position_key_uses_strategy_owner_and_ticks():
    """Position key must be keccak256(owner, int24 tickLower, int24 tickUpper)
    matching Uniswap V3's PositionKey.compute() encoding. Pin the
    solidity_keccak call shape so anyone reordering args or changing
    int24->int256 fails this test instead of silently mining wrong keys."""
    reader, _, _ = _make_reader()
    spy = MagicMock(return_value=b"\xab" * 32)
    reader._w3.solidity_keccak = spy
    key = reader._compute_position_key(96040, 97540)
    spy.assert_called_once_with(
        ["address", "int24", "int24"],
        ["0xSTRATEGY", 96040, 97540],
    )
    assert key == b"\xab" * 32
    assert len(key) == 32


@pytest.mark.asyncio
async def test_read_position_main_returns_liquidity_from_pool():
    """positionMain reads tick range from strategy, then pool.positions(key)
    returns (liquidity, fee_growth_0, fee_growth_1, tokens_owed_0, tokens_owed_1)."""
    reader, pool_contract, strategy_contract = _make_reader(
        positions_return=(987654321, 0, 0, 0, 0),
        main_range=(96040, 97540),
    )
    pos = await reader.read_position_main()
    assert isinstance(pos, V3Position)
    assert pos.liquidity == 987654321
    assert pos.tick_lower == 96040
    assert pos.tick_upper == 97540
    pool_contract.functions.positions.assert_called_once()


@pytest.mark.asyncio
async def test_read_position_alt_returns_none_when_inactive():
    """When positionAlt range is (0, 0) (inactive sentinel), return None."""
    reader, _, _ = _make_reader(alt_range=(0, 0))
    pos = await reader.read_position_alt()
    assert pos is None


@pytest.mark.asyncio
async def test_read_position_alt_returns_none_on_strategy_failure():
    """If strategy.positionAlt() raises (e.g. method missing on older
    strategy contracts), return None rather than propagating."""
    reader, _, _ = _make_reader(alt_raises=True)
    pos = await reader.read_position_alt()
    assert pos is None


@pytest.mark.asyncio
async def test_read_position_alt_returns_v3position_when_active():
    """Active alt range produces a V3Position with both ticks and liquidity."""
    reader, _, _ = _make_reader(
        positions_return=(555, 0, 0, 0, 0),
        main_range=(96040, 97540),
        alt_range=(96100, 97400),
    )
    pos = await reader.read_position_alt()
    assert pos is not None
    assert pos.liquidity == 555
    assert pos.tick_lower == 96100
    assert pos.tick_upper == 97400
