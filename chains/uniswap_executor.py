"""Uniswap V3 SwapRouter executor: approve, swap_exact_output, swap_exact_input."""
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
with open(_ABI_DIR / "uniswap_v3_swap_router.json") as f:
    SWAP_ROUTER_ABI = json.load(f)

MAX_UINT256 = 2**256 - 1


class UniswapExecutor(ChainExecutor):
    def __init__(self, *, w3: AsyncWeb3, account: LocalAccount, router_address: str):
        super().__init__(w3=w3, account=account)
        self._router_address = w3.to_checksum_address(router_address)
        self._router_contract = w3.eth.contract(
            address=self._router_address, abi=SWAP_ROUTER_ABI,
        )

    def _erc20(self, token_address: str):
        return self._w3.eth.contract(
            address=self._w3.to_checksum_address(token_address), abi=ERC20_ABI,
        )

    async def ensure_approval(
        self, *, token_address: str, amount: int, spender: str,
    ) -> str | None:
        """Returns tx_hash if approve was sent; None if allowance already sufficient.

        For MVP we approve MAX_UINT256 in one shot to avoid re-approving on each
        operation. Spender is typically the SwapRouter, but the API allows any
        spender for use by other executors (Beefy).
        """
        token = self._erc20(token_address)
        spender_cs = self._w3.to_checksum_address(spender)
        current = await token.functions.allowance(self._account.address, spender_cs).call()
        if current >= amount:
            return None
        logger.info(f"Approving {token_address} to {spender_cs} (current={current}, required={amount})")
        return await self.send_tx(
            token.functions.approve(spender_cs, MAX_UINT256),
            gas_limit=80_000,
        )

    async def swap_exact_output(
        self, *,
        token_in: str, token_out: str, fee: int = 500,
        amount_out: int, amount_in_maximum: int,
        recipient: str, deadline: int,
    ) -> str:
        """Swap up to amount_in_maximum of token_in for exactly amount_out of token_out.

        Returns tx_hash. Raises if router reverts (slippage breach).
        """
        params = {
            "tokenIn": self._w3.to_checksum_address(token_in),
            "tokenOut": self._w3.to_checksum_address(token_out),
            "fee": fee,
            "recipient": self._w3.to_checksum_address(recipient),
            "deadline": deadline,
            "amountOut": amount_out,
            "amountInMaximum": amount_in_maximum,
            "sqrtPriceLimitX96": 0,
        }
        return await self.send_tx(
            self._router_contract.functions.exactOutputSingle(params),
            gas_limit=200_000,
        )

    async def swap_exact_input(
        self, *,
        token_in: str, token_out: str, fee: int = 500,
        amount_in: int, amount_out_minimum: int,
        recipient: str, deadline: int,
    ) -> str:
        """Swap exactly amount_in of token_in for at least amount_out_minimum of token_out.

        Used for teardown WETH -> USDC.
        """
        params = {
            "tokenIn": self._w3.to_checksum_address(token_in),
            "tokenOut": self._w3.to_checksum_address(token_out),
            "fee": fee,
            "recipient": self._w3.to_checksum_address(recipient),
            "deadline": deadline,
            "amountIn": amount_in,
            "amountOutMinimum": amount_out_minimum,
            "sqrtPriceLimitX96": 0,
        }
        return await self.send_tx(
            self._router_contract.functions.exactInputSingle(params),
            gas_limit=200_000,
        )
