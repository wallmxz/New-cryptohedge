"""Beefy CLM executor: deposit, withdraw, approve.

Important: in Beefy CLM v2, user-facing operations (deposit/withdraw) go
through the EARN VAULT, not the strategy. The strategy holds the V3 NFT
and exposes view-only state (positionMain, balances, totalSupply) plus
internal write functions guarded by `onlyVault`. Calling `deposit()`
directly on the strategy reverts.

This executor wires deposit/withdraw to the earn vault, and approvals
also go to the earn vault (it's the contract that does transferFrom).
"""
from __future__ import annotations
import asyncio
import json
import logging
from pathlib import Path
from web3 import AsyncWeb3
from eth_account.signers.local import LocalAccount
from chains.executor import ChainExecutor

logger = logging.getLogger(__name__)

# Beefy CLM custom error: pool's current tick is too far from TWAP, anti-MEV
# guard rejects the action. Retried with backoff — pool calms down once the
# big swap that triggered it ages out of the TWAP window (~30s-2min typical).
_NOT_CALM_SELECTOR = "0x26c87876"
_CALM_RETRY_DELAYS = [30, 60, 120]  # seconds; 3 retries

_ABI_DIR = Path(__file__).parent.parent / "abi"
with open(_ABI_DIR / "erc20.json") as f:
    ERC20_ABI = json.load(f)
with open(_ABI_DIR / "beefy_clm_strategy_write.json") as f:
    # The deposit/withdraw/previewDeposit selectors are identical between
    # strategy and earn vault, so we reuse the same write ABI here on the
    # earn vault contract.
    EARN_WRITE_ABI = json.load(f)

MAX_UINT256 = 2**256 - 1


class BeefyExecutor(ChainExecutor):
    def __init__(
        self, *, w3: AsyncWeb3, account: LocalAccount,
        strategy_address: str, earn_address: str | None = None,
    ):
        """`strategy_address` is kept for backwards compatibility / inspection;
        when `earn_address` is omitted, the executor falls back to the
        strategy address (same legacy behavior). New callers should always
        pass `earn_address` — that's where deposit/withdraw must go in CLM v2.
        """
        super().__init__(w3=w3, account=account)
        self._strategy_address = w3.to_checksum_address(strategy_address)
        # Spender = the contract user-facing approvals go to AND the contract
        # the deposit/withdraw txs are sent to. In CLM v2 that's the earn
        # vault. Keep the legacy single-address path so older deployments
        # without a discovered earn vault still work (will fail at deposit
        # time with a clear revert).
        spender = earn_address if earn_address is not None else strategy_address
        self._spender_address = w3.to_checksum_address(spender)
        self._spender_contract = w3.eth.contract(
            address=self._spender_address, abi=EARN_WRITE_ABI,
        )

    def _erc20(self, token_address: str):
        return self._w3.eth.contract(
            address=self._w3.to_checksum_address(token_address), abi=ERC20_ABI,
        )

    async def ensure_approval(self, *, token_address: str, amount: int) -> str | None:
        """Approve the earn vault (or strategy in legacy mode) as spender.
        Returns tx_hash or None if already approved."""
        token = self._erc20(token_address)
        current = await token.functions.allowance(
            self._account.address, self._spender_address,
        ).call()
        if current >= amount:
            return None
        logger.info(
            f"Approving {token_address} to spender {self._spender_address}"
        )
        return await self.send_tx(
            token.functions.approve(self._spender_address, MAX_UINT256),
            gas_limit=80_000,
        )

    async def _is_calm(self, contract_fn) -> bool:
        """Pre-flight eth_call. Returns True if the call would succeed,
        False if it reverts with `NotCalm()`. Re-raises any OTHER revert."""
        try:
            await contract_fn.call({"from": self._account.address})
            return True
        except Exception as e:
            msg = repr(e)
            if _NOT_CALM_SELECTOR in msg or "NotCalm" in msg:
                return False
            # Some other revert — not a calm-period issue. Let the actual
            # send_tx surface it (with full gas estimate + receipt).
            return True

    async def deposit(self, *, amount0: int, amount1: int, min_shares: int) -> str:
        """Deposit both tokens to the CLM earn vault. Returns tx_hash.

        Reverts if shares minted < min_shares (slippage protection).

        Gas: don't hardcode. Beefy CLM v2 deposit can consume 700k+ gas
        (transferFrom × 2 + harvest + V3 NFT increaseLiquidity + share
        mint). A hardcoded 500k limit causes out-of-gas reverts that look
        identical to logic reverts on-chain. Let send_tx() call
        estimate_gas + 20% buffer instead.

        Calm-period retry: Beefy CLM v2 reverts with `NotCalm()` (selector
        0x26c87876) when the pool's current tick deviates too far from the
        TWAP — anti-MEV guard against deposits during big price moves.
        We pre-flight eth_call before submitting the real tx; if it would
        revert with NotCalm, sleep and retry up to 3 times with backoff
        (30s, 60s, 120s) — total ~3.5 min worst case. Other reverts go
        straight to send_tx.
        """
        fn = self._spender_contract.functions.deposit(amount0, amount1, min_shares)
        for attempt, delay in enumerate([0, *_CALM_RETRY_DELAYS]):
            if delay:
                logger.warning(
                    f"Beefy NotCalm() detected — pool too volatile for deposit. "
                    f"Sleeping {delay}s before retry #{attempt}..."
                )
                await asyncio.sleep(delay)
            if await self._is_calm(fn):
                return await self.send_tx(fn)
        raise RuntimeError(
            f"Beefy NotCalm() persisted after {len(_CALM_RETRY_DELAYS)} retries "
            f"(~{sum(_CALM_RETRY_DELAYS)}s). Pool is in a sustained volatile "
            f"period; try again in a few minutes when prices stabilize."
        )

    async def withdraw(self, *, shares: int, min_amount0: int = 0, min_amount1: int = 0) -> str:
        """Withdraw `shares` worth of liquidity from the earn vault.
        Returns tx_hash.

        For MVP min_amount0/1 default to 0 (accept any amount). Caller is
        responsible for sanity-checking returned amounts off-chain.

        Gas: same as deposit — let send_tx() use estimate_gas + buffer.

        Calm-period retry: same as deposit. Withdraw is also gated by the
        anti-MEV `requireCalm()` modifier in CLM v2. Pre-flight eth_call
        and back off on NotCalm() rather than burning gas on doomed txs.
        """
        fn = self._spender_contract.functions.withdraw(shares, min_amount0, min_amount1)
        for attempt, delay in enumerate([0, *_CALM_RETRY_DELAYS]):
            if delay:
                logger.warning(
                    f"Beefy NotCalm() on withdraw — sleeping {delay}s before retry #{attempt}..."
                )
                await asyncio.sleep(delay)
            if await self._is_calm(fn):
                return await self.send_tx(fn)
        raise RuntimeError(
            f"Beefy NotCalm() persisted on withdraw after {len(_CALM_RETRY_DELAYS)} retries "
            f"(~{sum(_CALM_RETRY_DELAYS)}s). Try again in a few minutes."
        )

    async def preview_deposit(self, *, amount0: int, amount1: int) -> dict:
        """Read-only call: simulate deposit, returns expected shares + amounts used.

        Useful for computing min_shares with a slippage tolerance.
        """
        result = await self._spender_contract.functions.previewDeposit(amount0, amount1).call()
        return {
            "shares": result[0],
            "amount0_used": result[1],
            "amount1_used": result[2],
        }
