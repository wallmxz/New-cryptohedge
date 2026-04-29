"""Beefy CLM Strategy executor: deposit, withdraw, approve."""
from __future__ import annotations
import json
import logging
from pathlib import Path
from web3 import AsyncWeb3
from eth_account.signers.local import LocalAccount
from chains.executor import ChainExecutor

logger = logging.getLogger(__name__)

_ABI_DIR = Path(__file__).parent.parent / "abi"
with open(_ABI_DIR / "erc20.json") as f:
    ERC20_ABI = json.load(f)
with open(_ABI_DIR / "beefy_clm_strategy_write.json") as f:
    STRATEGY_WRITE_ABI = json.load(f)

MAX_UINT256 = 2**256 - 1


class BeefyExecutor(ChainExecutor):
    def __init__(self, *, w3: AsyncWeb3, account: LocalAccount, strategy_address: str):
        super().__init__(w3=w3, account=account)
        self._strategy_address = w3.to_checksum_address(strategy_address)
        self._strategy_contract = w3.eth.contract(
            address=self._strategy_address, abi=STRATEGY_WRITE_ABI,
        )

    def _erc20(self, token_address: str):
        return self._w3.eth.contract(
            address=self._w3.to_checksum_address(token_address), abi=ERC20_ABI,
        )

    async def ensure_approval(self, *, token_address: str, amount: int) -> str | None:
        """Approve strategy as spender. Returns tx_hash or None if already approved."""
        token = self._erc20(token_address)
        current = await token.functions.allowance(self._account.address, self._strategy_address).call()
        if current >= amount:
            return None
        logger.info(f"Approving {token_address} to strategy {self._strategy_address}")
        return await self.send_tx(
            token.functions.approve(self._strategy_address, MAX_UINT256),
            gas_limit=80_000,
        )

    async def deposit(self, *, amount0: int, amount1: int, min_shares: int) -> str:
        """Deposit both tokens to the CLM strategy. Returns tx_hash.

        Reverts if shares minted < min_shares (slippage protection).
        """
        return await self.send_tx(
            self._strategy_contract.functions.deposit(amount0, amount1, min_shares),
            gas_limit=500_000,
        )

    async def withdraw(self, *, shares: int, min_amount0: int = 0, min_amount1: int = 0) -> str:
        """Withdraw `shares` worth of liquidity. Returns tx_hash.

        For MVP min_amount0/1 default to 0 (accept any amount). Caller is
        responsible for sanity-checking returned amounts off-chain.
        """
        return await self.send_tx(
            self._strategy_contract.functions.withdraw(shares, min_amount0, min_amount1),
            gas_limit=500_000,
        )

    async def preview_deposit(self, *, amount0: int, amount1: int) -> dict:
        """Read-only call: simulate deposit, returns expected shares + amounts used.

        Useful for computing min_shares with a slippage tolerance.
        """
        result = await self._strategy_contract.functions.previewDeposit(amount0, amount1).call()
        return {
            "shares": result[0],
            "amount0_used": result[1],
            "amount1_used": result[2],
        }
