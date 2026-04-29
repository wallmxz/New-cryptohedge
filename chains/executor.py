"""Base ChainExecutor: web3 mechanics (signing, gas, retry, idempotency).

Subclasses (UniswapExecutor, BeefyExecutor) compose contract call functions
and pass them to send_tx() without worrying about nonce/gas/receipt waiting.
"""
from __future__ import annotations
import inspect
import logging
from web3 import AsyncWeb3
from eth_account.signers.local import LocalAccount

logger = logging.getLogger(__name__)

GAS_BUFFER = 1.20  # 20% safety margin over estimated gas
DEFAULT_RECEIPT_TIMEOUT = 180  # seconds


async def _resolve_async_attr(value):
    """Resolve a web3 attribute that may be an awaitable (property) or a
    callable (async method / AsyncMock). Real web3 7.x exposes gas_price /
    chain_id as awaitable properties; test mocks wire them as AsyncMock."""
    if callable(value):
        value = value()
    if inspect.isawaitable(value):
        value = await value
    return value


class ChainExecutor:
    """Wraps AsyncWeb3 + LocalAccount with high-level send_tx() / wait_for_receipt().

    Idempotency is the responsibility of the caller (lifecycle): the caller
    persists tx_hash to DB before/after each step. send_tx() itself just
    submits and waits.
    """

    def __init__(self, *, w3: AsyncWeb3, account: LocalAccount):
        self._w3 = w3
        self._account = account

    @property
    def address(self) -> str:
        return self._account.address

    async def estimate_gas(self, contract_fn) -> int:
        """Returns estimated gas with 20% safety buffer."""
        raw = await contract_fn.estimate_gas({"from": self._account.address})
        return int(raw * GAS_BUFFER)

    async def get_nonce(self) -> int:
        return await self._w3.eth.get_transaction_count(self._account.address, "pending")

    async def send_tx(
        self, contract_fn, *,
        gas_limit: int | None = None,
        value: int = 0,
    ) -> str:
        """Build, sign, submit, wait for receipt. Returns tx_hash hex string.

        Raises RuntimeError if receipt.status == 0 (revert).
        """
        if gas_limit is None:
            gas_limit = await self.estimate_gas(contract_fn)

        nonce = await self.get_nonce()
        chain_id = await _resolve_async_attr(self._w3.eth.chain_id)
        gas_price = await _resolve_async_attr(self._w3.eth.gas_price)

        tx_params = {
            "from": self._account.address,
            "nonce": nonce,
            "gas": gas_limit,
            "gasPrice": gas_price,
            "value": value,
            "chainId": chain_id,
        }
        tx = await contract_fn.build_transaction(tx_params)

        signed = self._account.sign_transaction(tx)
        # eth_account 0.13+ uses snake_case raw_transaction; older versions and
        # some test mocks use camelCase rawTransaction. Support both.
        raw_tx = getattr(signed, "raw_transaction", None) or getattr(signed, "rawTransaction")
        raw_hash = await self._w3.eth.send_raw_transaction(raw_tx)
        tx_hash = self._w3.to_hex(raw_hash) if not isinstance(raw_hash, str) else raw_hash
        logger.info(f"Submitted tx {tx_hash} (nonce={nonce}, gas={gas_limit})")

        receipt = await self.wait_for_receipt(tx_hash)
        if receipt.get("status") != 1:
            raise RuntimeError(f"Transaction {tx_hash} reverted")
        return tx_hash

    async def wait_for_receipt(
        self, tx_hash: str, *, timeout: int = DEFAULT_RECEIPT_TIMEOUT,
    ) -> dict:
        """Wait for tx confirmation. Returns receipt dict.

        Used both for new txs (in send_tx) and to resume after crash
        (lifecycle.resume_in_flight passes the persisted tx_hash here).
        """
        return await self._w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
