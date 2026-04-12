from __future__ import annotations
import asyncio
import logging
from typing import Callable, Awaitable
from web3 import AsyncWeb3, AsyncHTTPProvider
from chains.base import ChainReader

logger = logging.getLogger(__name__)

CLM_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "totalSupply", "type": "function", "stateMutability": "view",
     "inputs": [], "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "balances", "type": "function", "stateMutability": "view",
     "inputs": [],
     "outputs": [{"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}]},
]


def calc_pool_position(
    *, cow_balance: float, total_supply: float,
    vault_token0: float, vault_token1: float,
    price_token0_usd: float, price_token1_usd: float,
) -> dict:
    if total_supply <= 0 or cow_balance <= 0:
        return {"my_token0": 0.0, "my_token1": 0.0, "value_usd": 0.0, "share": 0.0}
    share = cow_balance / total_supply
    my_token0 = round(vault_token0 * share, 15)
    my_token1 = round(vault_token1 * share, 15)
    value_usd = round(my_token0 * price_token0_usd + my_token1 * price_token1_usd, 15)
    return {"my_token0": my_token0, "my_token1": my_token1, "value_usd": value_usd, "share": share}


class EVMChainReader(ChainReader):
    def __init__(
        self, rpc_url: str, fallback_rpc_url: str, vault_address: str,
        pool_address: str, wallet_address: str, poll_interval: float = 1.0,
        on_update: Callable[[dict], Awaitable[None]] | None = None,
    ):
        self._rpc_url = rpc_url
        self._fallback_rpc_url = fallback_rpc_url
        self._vault_address = vault_address
        self._pool_address = pool_address
        self._wallet_address = wallet_address
        self._poll_interval = poll_interval
        self._on_update = on_update
        self._w3: AsyncWeb3 | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._consecutive_failures = 0

    async def start(self) -> None:
        self._w3 = AsyncWeb3(AsyncHTTPProvider(self._rpc_url))
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"EVM chain reader started (poll every {self._poll_interval}s)")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                data = await self.read_pool_position()
                self._consecutive_failures = 0
                if self._on_update:
                    await self._on_update(data)
            except Exception as e:
                self._consecutive_failures += 1
                logger.error(f"Chain read failed ({self._consecutive_failures}): {e}")
                if self._consecutive_failures >= 5:
                    logger.critical("Chain reader: 5 consecutive failures")
            await asyncio.sleep(self._poll_interval)

    async def read_pool_position(self) -> dict:
        vault = self._w3.eth.contract(
            address=self._w3.to_checksum_address(self._vault_address), abi=CLM_ABI,
        )
        cow_balance = await vault.functions.balanceOf(
            self._w3.to_checksum_address(self._wallet_address)
        ).call()
        total_supply = await vault.functions.totalSupply().call()
        balances = await vault.functions.balances().call()
        return {
            "cow_balance": cow_balance / 1e18,
            "total_supply": total_supply / 1e18,
            "vault_token0": balances[0] / 1e18,
            "vault_token1": balances[1] / 1e18,
        }
