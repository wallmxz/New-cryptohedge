import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from chains.beefy_executor import BeefyExecutor


@pytest.fixture
def mock_w3():
    w3 = MagicMock()
    w3.eth = MagicMock()
    w3.eth.contract = MagicMock()
    w3.to_checksum_address = lambda a: a
    return w3


@pytest.fixture
def mock_account():
    acc = MagicMock()
    acc.address = "0xWallet"
    return acc


@pytest.mark.asyncio
async def test_deposit_calls_strategy_deposit(mock_w3, mock_account):
    """deposit forwards (amount0, amount1, min_shares) to strategy.deposit."""
    strategy_contract = MagicMock()
    deposit_fn = MagicMock()
    strategy_contract.functions.deposit = MagicMock(return_value=deposit_fn)
    mock_w3.eth.contract.return_value = strategy_contract

    ex = BeefyExecutor(w3=mock_w3, account=mock_account, strategy_address="0xStrategy")
    with patch.object(ex, "send_tx", AsyncMock(return_value="0xdeptx")):
        tx = await ex.deposit(amount0=10**17, amount1=200 * 10**6, min_shares=10**15)
    assert tx == "0xdeptx"
    strategy_contract.functions.deposit.assert_called_once_with(10**17, 200 * 10**6, 10**15)


@pytest.mark.asyncio
async def test_withdraw_calls_strategy_withdraw(mock_w3, mock_account):
    """withdraw forwards (shares, min_amount0, min_amount1)."""
    strategy_contract = MagicMock()
    withdraw_fn = MagicMock()
    strategy_contract.functions.withdraw = MagicMock(return_value=withdraw_fn)
    mock_w3.eth.contract.return_value = strategy_contract

    ex = BeefyExecutor(w3=mock_w3, account=mock_account, strategy_address="0xStrategy")
    with patch.object(ex, "send_tx", AsyncMock(return_value="0xwdtx")):
        tx = await ex.withdraw(shares=10**16, min_amount0=0, min_amount1=0)
    assert tx == "0xwdtx"
    strategy_contract.functions.withdraw.assert_called_once_with(10**16, 0, 0)


@pytest.mark.asyncio
async def test_ensure_approval_for_strategy(mock_w3, mock_account):
    """ensure_approval delegates to ERC20 (same logic as UniswapExecutor)."""
    token_contract = MagicMock()
    token_contract.functions.allowance = MagicMock(return_value=MagicMock(
        call=AsyncMock(return_value=0)
    ))
    approve_fn = MagicMock()
    token_contract.functions.approve = MagicMock(return_value=approve_fn)
    mock_w3.eth.contract.return_value = token_contract

    ex = BeefyExecutor(w3=mock_w3, account=mock_account, strategy_address="0xStrategy")
    with patch.object(ex, "send_tx", AsyncMock(return_value="0xapprovetx")):
        result = await ex.ensure_approval(token_address="0xUSDC", amount=10**18)
    # Spender for Beefy approval is the strategy itself
    token_contract.functions.approve.assert_called_once()
    spender_arg = token_contract.functions.approve.call_args[0][0]
    assert spender_arg == "0xStrategy"
    assert result == "0xapprovetx"
