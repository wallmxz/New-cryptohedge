import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from engine.lifecycle import OperationLifecycle


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.dydx_symbol = "ETH-USD"
    s.uniswap_v3_router_address = "0xRouter"
    s.token0_address = "0xWETH"
    s.token1_address = "0xUSDC"
    s.token0_decimals = 18
    s.token1_decimals = 6
    s.slippage_bps = 30
    s.uniswap_v3_pool_fee = 500
    s.alert_webhook_url = ""
    s.clm_vault_address = "0xStrategy"
    return s


@pytest.fixture
def mock_hub():
    h = MagicMock()
    h.hedge_ratio = 1.0; h.dydx_collateral = 130.0
    h.bootstrap_progress = ""
    h.operation_state = "none"
    h.current_operation_id = None
    return h


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.update_bootstrap_state = AsyncMock()
    db.update_operation_status = AsyncMock()
    db.update_baseline_amounts = AsyncMock()
    return db


@pytest.fixture
def mock_pool_reader():
    p = MagicMock()
    p.read_price = AsyncMock(return_value=3000.0)
    return p


@pytest.fixture
def mock_beefy_reader():
    b = MagicMock()
    pos = MagicMock()
    pos.tick_lower = -197310; pos.tick_upper = -195303
    pos.amount0 = 0.5; pos.amount1 = 1500.0; pos.share = 0.01; pos.raw_balance = 10**16
    b.read_position = AsyncMock(return_value=pos)
    return b


@pytest.fixture
def mock_exchange():
    ex = MagicMock()
    ex.place_long_term_order = AsyncMock()
    ex.batch_cancel = AsyncMock()
    ex.get_position = AsyncMock(return_value=None)
    return ex


@pytest.fixture
def mock_uniswap():
    u = MagicMock()
    u.address = "0xWallet"
    u.swap_exact_output = AsyncMock(return_value="0xnewswap")
    u.swap_exact_input = AsyncMock(return_value="0xnewswapin")
    u.ensure_approval = AsyncMock(return_value=None)
    u.wait_for_receipt = AsyncMock(return_value={"status": 1})
    return u


@pytest.fixture
def mock_beefy_exec():
    b = MagicMock()
    b.deposit = AsyncMock(return_value="0xnewdeposit")
    b.withdraw = AsyncMock(return_value="0xnewwd")
    b.ensure_approval = AsyncMock(return_value=None)
    b.wait_for_receipt = AsyncMock(return_value={"status": 1})
    return b


@pytest.fixture
def lifecycle(mock_settings, mock_hub, mock_db, mock_exchange, mock_uniswap, mock_beefy_exec, mock_pool_reader, mock_beefy_reader):
    return OperationLifecycle(
        settings=mock_settings, hub=mock_hub, db=mock_db,
        exchange=mock_exchange, uniswap=mock_uniswap, beefy=mock_beefy_exec,
        pool_reader=mock_pool_reader, beefy_reader=mock_beefy_reader,
    )


@pytest.mark.asyncio
async def test_resume_in_flight_no_ops_does_nothing(lifecycle, mock_db):
    mock_db.get_in_flight_operations = AsyncMock(return_value=[])
    await lifecycle.resume_in_flight()


@pytest.mark.asyncio
async def test_resume_swap_pending_with_hash_waits_then_continues(
    lifecycle, mock_db, mock_uniswap,
):
    """If swap was submitted (tx_hash exists) but not confirmed: wait for receipt, then advance."""
    mock_db.get_in_flight_operations = AsyncMock(return_value=[{
        "id": 5, "bootstrap_state": "swap_pending",
        "bootstrap_swap_tx_hash": "0xpending_swap",
        "bootstrap_deposit_tx_hash": None,
        "usdc_budget": 300.0,
    }])
    mock_uniswap.wait_for_receipt = AsyncMock(return_value={"status": 1})

    with patch.object(lifecycle, "_continue_bootstrap", AsyncMock()) as mock_continue:
        await lifecycle.resume_in_flight()
    mock_uniswap.wait_for_receipt.assert_awaited_with("0xpending_swap")
    mock_continue.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_swap_pending_no_hash_resubmits(lifecycle, mock_db):
    """If swap was started but no tx_hash recorded (crash before submit): re-execute step."""
    mock_db.get_in_flight_operations = AsyncMock(return_value=[{
        "id": 6, "bootstrap_state": "swap_pending",
        "bootstrap_swap_tx_hash": None,
        "bootstrap_deposit_tx_hash": None,
        "usdc_budget": 300.0,
    }])
    with patch.object(lifecycle, "_continue_bootstrap", AsyncMock()) as mock_continue:
        await lifecycle.resume_in_flight()
    mock_continue.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_marks_failed_on_unexpected_state(lifecycle, mock_db):
    """Unknown state (corruption) -> mark failed, alert, don't crash startup."""
    mock_db.get_in_flight_operations = AsyncMock(return_value=[{
        "id": 7, "bootstrap_state": "unknown_garbage",
        "bootstrap_swap_tx_hash": None,
        "bootstrap_deposit_tx_hash": None,
    }])
    await lifecycle.resume_in_flight()
    mock_db.update_bootstrap_state.assert_any_call(7, "failed")


@pytest.mark.asyncio
async def test_resume_deposit_pending_with_hash_waits_then_continues(
    lifecycle, mock_db, mock_beefy_exec,
):
    """If deposit was submitted (tx_hash exists) but not confirmed: wait, advance, continue."""
    mock_db.get_in_flight_operations = AsyncMock(return_value=[{
        "id": 8, "bootstrap_state": "deposit_pending",
        "bootstrap_swap_tx_hash": "0xswap_done",
        "bootstrap_deposit_tx_hash": "0xpending_deposit",
        "usdc_budget": 300.0,
    }])
    with patch.object(lifecycle, "_continue_bootstrap", AsyncMock()) as mock_continue:
        await lifecycle.resume_in_flight()
    mock_beefy_exec.wait_for_receipt.assert_awaited_with("0xpending_deposit")
    mock_continue.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_teardown_withdraw_pending_waits_then_continues(
    lifecycle, mock_db, mock_beefy_exec,
):
    """If withdraw was submitted (tx_hash exists) but not confirmed: wait, advance, continue."""
    mock_db.get_in_flight_operations = AsyncMock(return_value=[{
        "id": 9, "bootstrap_state": "teardown_withdraw_pending",
        "teardown_withdraw_tx_hash": "0xpending_withdraw",
    }])
    with patch.object(lifecycle, "_continue_teardown", AsyncMock()) as mock_continue:
        await lifecycle.resume_in_flight()
    mock_beefy_exec.wait_for_receipt.assert_awaited_with("0xpending_withdraw")
    mock_continue.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_teardown_swap_pending_waits_then_continues(
    lifecycle, mock_db, mock_uniswap,
):
    """If teardown swap was submitted (tx_hash exists) but not confirmed: wait, advance, continue."""
    mock_db.get_in_flight_operations = AsyncMock(return_value=[{
        "id": 10, "bootstrap_state": "teardown_swap_pending",
        "teardown_swap_tx_hash": "0xpending_teardownswap",
    }])
    with patch.object(lifecycle, "_continue_teardown", AsyncMock()) as mock_continue:
        await lifecycle.resume_in_flight()
    mock_uniswap.wait_for_receipt.assert_awaited_with("0xpending_teardownswap")
    mock_continue.assert_awaited_once()
