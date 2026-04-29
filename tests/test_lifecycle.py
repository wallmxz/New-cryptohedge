import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch
from engine.lifecycle import OperationLifecycle


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.dydx_symbol = "ETH-USD"
    s.uniswap_v3_router_address = "0xRouter"
    s.usdc_token_address = "0xUSDC"
    s.weth_token_address = "0xWETH"
    s.slippage_bps = 30
    s.clm_vault_address = "0xStrategy"
    s.alert_webhook_url = ""
    return s


@pytest.fixture
def mock_hub():
    h = MagicMock()
    h.hedge_ratio = 1.0
    h.operation_state = "none"
    h.current_operation_id = None
    h.bootstrap_progress = ""
    h.dydx_collateral = 130.0
    h.hedge_realized_pnl = 0.0
    h.hedge_unrealized_pnl = 0.0
    return h


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_active_operation = AsyncMock(return_value=None)
    db.insert_operation = AsyncMock(return_value=1)
    db.update_bootstrap_state = AsyncMock()
    db.update_operation_status = AsyncMock()
    db.add_to_operation_accumulator = AsyncMock()
    db.get_in_flight_operations = AsyncMock(return_value=[])
    db.close_operation = AsyncMock()
    db.get_operation = AsyncMock(return_value=None)
    db.get_active_grid_orders = AsyncMock(return_value=[])
    db.mark_grid_order_cancelled = AsyncMock()
    return db


@pytest.fixture
def mock_pool_reader():
    p = MagicMock()
    p.read_price = AsyncMock(return_value=3000.0)
    p.read_slot0 = AsyncMock(return_value=(2**96 * 54, 0))
    return p


@pytest.fixture
def mock_beefy_reader():
    b = MagicMock()
    pos = MagicMock()
    pos.tick_lower = -197310
    pos.tick_upper = -195303
    pos.amount0 = 0.5
    pos.amount1 = 1500.0
    pos.share = 1.0  # post-deposit: user owns full position (mock simplification)
    pos.raw_balance = 10**18
    b.read_position = AsyncMock(return_value=pos)
    return b


@pytest.fixture
def mock_exchange():
    ex = MagicMock()
    ex.place_long_term_order = AsyncMock()
    ex.get_collateral = AsyncMock(return_value=130.0)
    ex.batch_cancel = AsyncMock()
    ex.get_position = AsyncMock(return_value=None)
    return ex


@pytest.fixture
def mock_uniswap():
    u = MagicMock()
    u.address = "0xWallet"
    u.ensure_approval = AsyncMock(return_value=None)  # already approved
    u.swap_exact_output = AsyncMock(return_value="0xswap")
    u.swap_exact_input = AsyncMock(return_value="0xswapin")
    return u


@pytest.fixture
def mock_beefy_exec():
    b = MagicMock()
    b.ensure_approval = AsyncMock(return_value=None)
    b.deposit = AsyncMock(return_value="0xdeposit")
    b.withdraw = AsyncMock(return_value="0xwithdraw")
    return b


@pytest.fixture
def lifecycle(mock_settings, mock_hub, mock_db, mock_exchange, mock_uniswap, mock_beefy_exec, mock_pool_reader, mock_beefy_reader):
    return OperationLifecycle(
        settings=mock_settings, hub=mock_hub, db=mock_db,
        exchange=mock_exchange, uniswap=mock_uniswap, beefy=mock_beefy_exec,
        pool_reader=mock_pool_reader, beefy_reader=mock_beefy_reader,
    )


@pytest.mark.asyncio
async def test_bootstrap_happy_path(lifecycle, mock_db, mock_uniswap, mock_beefy_exec, mock_exchange):
    """bootstrap with $300 budget calls swap + deposit + opens short, marks active."""
    with patch.object(lifecycle, "_read_wallet_balance", AsyncMock(return_value={"weth": 0.046, "usdc": 162.0, "eth": 0.01})):
        with patch.object(lifecycle, "_check_gas_balance", AsyncMock(return_value=None)):
            op_id = await lifecycle.bootstrap(usdc_budget=300.0)

    assert op_id == 1
    mock_uniswap.swap_exact_output.assert_awaited_once()
    mock_beefy_exec.deposit.assert_awaited_once()
    mock_exchange.place_long_term_order.assert_awaited_once()

    # Verify final state was 'active'
    final_states = [c.args[1] for c in mock_db.update_bootstrap_state.call_args_list]
    assert "active" in final_states


@pytest.mark.asyncio
async def test_bootstrap_rejects_when_active_exists(lifecycle, mock_db):
    """bootstrap raises if there's already an active operation."""
    mock_db.get_active_operation = AsyncMock(return_value={"id": 99, "status": "active"})
    with pytest.raises(RuntimeError, match="already active"):
        await lifecycle.bootstrap(usdc_budget=300.0)


@pytest.mark.asyncio
async def test_bootstrap_skips_swap_when_price_above_range(
    lifecycle, mock_pool_reader, mock_uniswap, mock_beefy_exec,
):
    """When p >= p_b, no swap needed — deposit only USDC."""
    mock_pool_reader.read_price = AsyncMock(return_value=10_000.0)
    with patch.object(lifecycle, "_read_wallet_balance", AsyncMock(return_value={"weth": 0.0, "usdc": 300.0, "eth": 0.01})):
        with patch.object(lifecycle, "_check_gas_balance", AsyncMock(return_value=None)):
            await lifecycle.bootstrap(usdc_budget=300.0)
    mock_uniswap.swap_exact_output.assert_not_awaited()
    mock_beefy_exec.deposit.assert_awaited_once()


@pytest.mark.asyncio
async def test_bootstrap_aborts_on_low_gas(lifecycle):
    """If wallet ETH balance < threshold, bootstrap raises."""
    with patch.object(lifecycle, "_check_gas_balance", AsyncMock(side_effect=RuntimeError("Wallet gas too low"))):
        with pytest.raises(RuntimeError, match="gas"):
            await lifecycle.bootstrap(usdc_budget=300.0)


@pytest.mark.asyncio
async def test_teardown_happy_path(lifecycle, mock_db, mock_exchange, mock_beefy_reader, mock_beefy_exec, mock_uniswap):
    """teardown cancels grid, closes short, withdraws Beefy, optionally swaps."""
    mock_db.get_active_operation = AsyncMock(return_value={
        "id": 1, "status": "active", "started_at": 1700000000.0,
        "ended_at": None,
        "baseline_eth_price": 3000.0,
        "baseline_pool_value_usd": 300.0,
        "baseline_amount0": 0.05,
        "baseline_amount1": 150.0,
        "baseline_collateral": 130.0,
        "perp_fees_paid": 0.0,
        "funding_paid": 0.0,
        "lp_fees_earned": 0.0,
        "bootstrap_slippage": 0.15,
        "final_net_pnl": None,
        "close_reason": None,
        "usdc_budget": 300.0,
        "bootstrap_state": "active",
    })
    mock_db.get_active_grid_orders = AsyncMock(return_value=[
        {"cloid": "1234", "side": "sell"}
    ])
    mock_db.mark_grid_order_cancelled = AsyncMock()
    mock_db.update_operation_status = AsyncMock()
    mock_db.close_operation = AsyncMock()
    mock_db.get_operation = AsyncMock(return_value={
        "id": 1, "status": "stopping", "started_at": 1700000000.0, "ended_at": None,
        "baseline_eth_price": 3000.0, "baseline_pool_value_usd": 300.0,
        "baseline_amount0": 0.05, "baseline_amount1": 150.0,
        "baseline_collateral": 130.0, "perp_fees_paid": 0.0, "funding_paid": 0.0,
        "lp_fees_earned": 0.0, "bootstrap_slippage": 0.15,
        "final_net_pnl": None, "close_reason": None,
    })

    mock_pos = MagicMock()
    mock_pos.size = 0.05
    mock_pos.side = "short"
    mock_exchange.get_position = AsyncMock(return_value=mock_pos)

    pos = MagicMock()
    pos.share = 0.01
    pos.raw_balance = 10**16
    pos.amount0 = 0.5
    pos.amount1 = 1500.0
    mock_beefy_reader.read_position = AsyncMock(return_value=pos)

    result = await lifecycle.teardown(swap_to_usdc=False)
    assert "id" in result
    mock_exchange.batch_cancel.assert_awaited()
    mock_exchange.place_long_term_order.assert_awaited()  # close short
    mock_beefy_exec.withdraw.assert_awaited_once()
    # No swap when swap_to_usdc=False
    mock_uniswap.swap_exact_input.assert_not_awaited()


@pytest.mark.asyncio
async def test_teardown_with_cashout_swaps(lifecycle, mock_db, mock_exchange, mock_beefy_reader, mock_beefy_exec, mock_uniswap):
    """teardown(swap_to_usdc=True) does the WETH->USDC swap at the end."""
    mock_db.get_active_operation = AsyncMock(return_value={
        "id": 2, "status": "active", "started_at": 1700000000.0, "ended_at": None,
        "baseline_eth_price": 3000.0, "baseline_pool_value_usd": 300.0,
        "baseline_amount0": 0.05, "baseline_amount1": 150.0,
        "baseline_collateral": 130.0, "perp_fees_paid": 0.0, "funding_paid": 0.0,
        "lp_fees_earned": 0.0, "bootstrap_slippage": 0.15,
        "final_net_pnl": None, "close_reason": None,
        "usdc_budget": 300.0, "bootstrap_state": "active",
    })
    mock_db.get_active_grid_orders = AsyncMock(return_value=[])
    mock_db.update_operation_status = AsyncMock()
    mock_db.close_operation = AsyncMock()
    mock_db.get_operation = AsyncMock(return_value=mock_db.get_active_operation.return_value)

    mock_exchange.get_position = AsyncMock(return_value=None)  # already flat

    pos = MagicMock()
    pos.share = 0.01; pos.raw_balance = 10**16
    pos.amount0 = 0.5; pos.amount1 = 1500.0
    mock_beefy_reader.read_position = AsyncMock(return_value=pos)

    with patch.object(lifecycle, "_read_wallet_balance", AsyncMock(return_value={"weth": 0.04, "usdc": 162.0, "eth": 0.01})):
        await lifecycle.teardown(swap_to_usdc=True)

    mock_uniswap.swap_exact_input.assert_awaited_once()


@pytest.mark.asyncio
async def test_teardown_rejects_when_no_active(lifecycle, mock_db):
    mock_db.get_active_operation = AsyncMock(return_value=None)
    with pytest.raises(RuntimeError, match="No active operation"):
        await lifecycle.teardown()
