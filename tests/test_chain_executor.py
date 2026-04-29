import pytest
from unittest.mock import AsyncMock, MagicMock
from chains.executor import ChainExecutor


@pytest.fixture
def mock_w3():
    w3 = MagicMock()
    w3.eth = MagicMock()
    w3.eth.get_transaction_count = AsyncMock(return_value=42)
    w3.eth.send_raw_transaction = AsyncMock(return_value=bytes.fromhex("a" * 64))
    w3.eth.wait_for_transaction_receipt = AsyncMock(return_value={"status": 1, "transactionHash": bytes.fromhex("a" * 64)})
    w3.eth.gas_price = AsyncMock(return_value=10**8)  # 0.1 gwei (Arbitrum-ish)
    w3.eth.chain_id = AsyncMock(return_value=42161)
    w3.to_hex = lambda b: "0x" + b.hex() if isinstance(b, bytes) else b
    return w3


@pytest.fixture
def mock_account():
    acc = MagicMock()
    acc.address = "0xWalletAddress"
    acc.sign_transaction = MagicMock(return_value=MagicMock(rawTransaction=b"signed"))
    return acc


@pytest.mark.asyncio
async def test_send_tx_returns_tx_hash(mock_w3, mock_account):
    """send_tx submits, waits for receipt, returns hex hash."""
    executor = ChainExecutor(w3=mock_w3, account=mock_account)
    fn = MagicMock()
    fn.build_transaction = AsyncMock(return_value={"to": "0xC", "data": "0x", "value": 0})
    tx_hash = await executor.send_tx(fn, gas_limit=100_000)
    assert tx_hash.startswith("0x")
    assert len(tx_hash) == 66  # 0x + 64 hex chars


@pytest.mark.asyncio
async def test_send_tx_raises_on_revert(mock_w3, mock_account):
    """Receipt with status=0 raises RuntimeError."""
    mock_w3.eth.wait_for_transaction_receipt = AsyncMock(
        return_value={"status": 0, "transactionHash": bytes.fromhex("b" * 64)}
    )
    executor = ChainExecutor(w3=mock_w3, account=mock_account)
    fn = MagicMock()
    fn.build_transaction = AsyncMock(return_value={"to": "0xC", "data": "0x", "value": 0})
    with pytest.raises(RuntimeError, match="reverted"):
        await executor.send_tx(fn, gas_limit=100_000)


@pytest.mark.asyncio
async def test_wait_for_receipt_returns_dict(mock_w3, mock_account):
    """wait_for_receipt is the resume primitive — exposes raw web3 receipt."""
    executor = ChainExecutor(w3=mock_w3, account=mock_account)
    receipt = await executor.wait_for_receipt("0x" + "a" * 64)
    assert receipt["status"] == 1


@pytest.mark.asyncio
async def test_estimate_gas_with_buffer(mock_w3, mock_account):
    """estimate_gas adds a 20% buffer to web3's estimate."""
    fn = MagicMock()
    fn.estimate_gas = AsyncMock(return_value=100_000)
    executor = ChainExecutor(w3=mock_w3, account=mock_account)
    gas = await executor.estimate_gas(fn)
    assert gas == 120_000  # 100_000 * 1.20


@pytest.mark.asyncio
async def test_send_tx_uses_provided_gas_limit(mock_w3, mock_account):
    """If gas_limit is passed, executor doesn't call estimate_gas."""
    fn = MagicMock()
    fn.build_transaction = AsyncMock(return_value={"to": "0xC", "data": "0x", "value": 0})
    fn.estimate_gas = AsyncMock(return_value=999_999)  # should not be called
    executor = ChainExecutor(w3=mock_w3, account=mock_account)
    await executor.send_tx(fn, gas_limit=80_000)
    fn.estimate_gas.assert_not_called()
