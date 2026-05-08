from __future__ import annotations
import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from web3 import AsyncWeb3


_ABI_DIR = Path(__file__).parent.parent / "abi"
with open(_ABI_DIR / "beefy_clm_strategy.json") as f:
    STRATEGY_ABI = json.load(f)
with open(_ABI_DIR / "beefy_clm_earn.json") as f:
    EARN_ABI = json.load(f)


@dataclass
class BeefyPosition:
    tick_lower: int
    tick_upper: int
    amount0: float        # display units (e.g., WETH)
    amount1: float        # display units (e.g., USDC)
    share: float          # user's share of the vault (0..1)
    raw_balance: int      # share token raw balance


class BeefyClmReader:
    """Reads on-chain state of a Beefy CLM (split between strategy and earn).

    Beefy CLM v2 architecture:
    - The STRATEGY contract holds the Uniswap V3 NFT and exposes
      `positionMain()` (the main range) + `positionAlt()` (the smaller
      limit range) and `balances()` (token0/token1 currently held).
    - The EARN contract is the user-facing ERC20 vault. It exposes
      `totalSupply()` and `balanceOf(addr)` for share accounting.
    - The REWARD POOL contract (a "gov-vault" in Beefy's API) is a
      separate ERC20 that the user STAKES the earn-vault LP tokens
      into to receive boost rewards (BIFI etc). When a user stakes,
      their earn-vault LP leaves their wallet for the reward pool's
      address — so `earn.balanceOf(wallet)` reads 0 even though the
      position is fully active. We sum `earn.balanceOf(wallet) +
      rewardPool.balanceOf(wallet)` so the bot tracks the position
      regardless of where the LP token currently sits.

    Beefy's older `range()` selector exists on these proxies but returns
    storage-slot garbage; `positionMain()` is the canonical accessor for
    the active liquidity range and is what Beefy's own UI / docs use.
    """
    def __init__(
        self, w3: AsyncWeb3, strategy_address: str, earn_address: str,
        wallet_address: str, decimals0: int, decimals1: int,
        reward_pool_address: str | None = None,
    ):
        self._w3 = w3
        self._strategy = w3.eth.contract(
            address=w3.to_checksum_address(strategy_address), abi=STRATEGY_ABI,
        )
        self._earn = w3.eth.contract(
            address=w3.to_checksum_address(earn_address), abi=EARN_ABI,
        )
        # Reward pool exposes ERC20 `balanceOf` only — we don't need any
        # of the gov-vault-specific methods, so re-use the EARN_ABI.
        self._reward_pool = None
        if reward_pool_address:
            self._reward_pool = w3.eth.contract(
                address=w3.to_checksum_address(reward_pool_address),
                abi=EARN_ABI,
            )
        self._wallet = w3.to_checksum_address(wallet_address)
        self._decimals0 = decimals0
        self._decimals1 = decimals1

    async def read_position(self) -> BeefyPosition:
        calls = [
            self._strategy.functions.positionMain().call(),
            self._strategy.functions.balances().call(),
            self._earn.functions.totalSupply().call(),
            self._earn.functions.balanceOf(self._wallet).call(),
        ]
        if self._reward_pool is not None:
            calls.append(self._reward_pool.functions.balanceOf(self._wallet).call())

        results = await asyncio.gather(*calls, return_exceptions=True)
        (tick_lower, tick_upper) = results[0]
        (amount0_raw, amount1_raw) = results[1]
        total_supply = results[2]
        wallet_balance = results[3]
        # Reward pool read may fail (network blip, contract paused) —
        # treat as 0 rather than blowing up the whole read.
        staked_balance = 0
        if self._reward_pool is not None:
            staked = results[4]
            if not isinstance(staked, BaseException):
                staked_balance = staked
        balance = wallet_balance + staked_balance

        share = balance / total_supply if total_supply > 0 else 0.0
        return BeefyPosition(
            tick_lower=tick_lower,
            tick_upper=tick_upper,
            amount0=amount0_raw / (10 ** self._decimals0),
            amount1=amount1_raw / (10 ** self._decimals1),
            share=share,
            raw_balance=balance,
        )
