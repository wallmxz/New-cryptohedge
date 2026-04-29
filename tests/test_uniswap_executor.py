import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from chains.uniswap_executor import UniswapExecutor


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
async def test_ensure_approval_skips_when_allowance_sufficient(mock_w3, mock_account):
    """If current allowance >= amount, ensure_approval returns None (no tx)."""
    token_contract = MagicMock()
    token_contract.functions.allowance = MagicMock(return_value=MagicMock(
        call=AsyncMock(return_value=10**30)  # huge allowance
    ))
    mock_w3.eth.contract.return_value = token_contract

    ex = UniswapExecutor(
        w3=mock_w3, account=mock_account,
        router_address="0xRouter",
    )
    result = await ex.ensure_approval(token_address="0xUSDC", amount=10**18, spender="0xRouter")
    assert result is None


@pytest.mark.asyncio
async def test_ensure_approval_sends_tx_when_insufficient(mock_w3, mock_account):
    """If allowance < amount, sends approve tx."""
    token_contract = MagicMock()
    token_contract.functions.allowance = MagicMock(return_value=MagicMock(
        call=AsyncMock(return_value=0)
    ))
    approve_fn = MagicMock()
    token_contract.functions.approve = MagicMock(return_value=approve_fn)
    mock_w3.eth.contract.return_value = token_contract

    ex = UniswapExecutor(
        w3=mock_w3, account=mock_account,
        router_address="0xRouter",
    )
    with patch.object(ex, "send_tx", AsyncMock(return_value="0xapprovetx")) as mock_send:
        result = await ex.ensure_approval(token_address="0xUSDC", amount=10**18, spender="0xRouter")
    assert result == "0xapprovetx"
    mock_send.assert_awaited_once()


@pytest.mark.asyncio
async def test_swap_exact_output_builds_correct_params(mock_w3, mock_account):
    """swap_exact_output passes the right tuple to router.exactOutputSingle."""
    router_contract = MagicMock()
    swap_fn = MagicMock()
    router_contract.functions.exactOutputSingle = MagicMock(return_value=swap_fn)
    mock_w3.eth.contract.return_value = router_contract

    ex = UniswapExecutor(
        w3=mock_w3, account=mock_account, router_address="0xRouter",
    )
    with patch.object(ex, "send_tx", AsyncMock(return_value="0xswaptx")):
        tx = await ex.swap_exact_output(
            token_in="0xUSDC", token_out="0xWETH", fee=500,
            amount_out=10**17, amount_in_maximum=200 * 10**6,
            recipient="0xWallet", deadline=1700000000,
        )
    assert tx == "0xswaptx"
    call_args = router_contract.functions.exactOutputSingle.call_args[0][0]
    assert call_args["tokenIn"] == "0xUSDC"
    assert call_args["tokenOut"] == "0xWETH"
    assert call_args["fee"] == 500
    assert call_args["amountOut"] == 10**17
    assert call_args["amountInMaximum"] == 200 * 10**6
    assert call_args["sqrtPriceLimitX96"] == 0


@pytest.mark.asyncio
async def test_swap_exact_input_builds_correct_params(mock_w3, mock_account):
    """swap_exact_input is symmetric — used for teardown WETH -> USDC."""
    router_contract = MagicMock()
    swap_fn = MagicMock()
    router_contract.functions.exactInputSingle = MagicMock(return_value=swap_fn)
    mock_w3.eth.contract.return_value = router_contract

    ex = UniswapExecutor(
        w3=mock_w3, account=mock_account, router_address="0xRouter",
    )
    with patch.object(ex, "send_tx", AsyncMock(return_value="0xtx")):
        await ex.swap_exact_input(
            token_in="0xWETH", token_out="0xUSDC", fee=500,
            amount_in=10**17, amount_out_minimum=295 * 10**6,
            recipient="0xWallet", deadline=1700000000,
        )
    call_args = router_contract.functions.exactInputSingle.call_args[0][0]
    assert call_args["amountIn"] == 10**17
    assert call_args["amountOutMinimum"] == 295 * 10**6
