"""Lifecycle.bootstrap dual-leg: 2 swaps sequenciais + 2 short opens paralelos."""
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from engine.lifecycle import OperationLifecycle


@pytest.fixture
def cross_pair_settings():
    from config import Settings
    return Settings(
        auth_user="a", auth_pass="p",
        wallet_address="0xW", wallet_private_key="0x" + "1" * 64,
        arbitrum_rpc_url="https://rpc", arbitrum_rpc_fallback="",
        clm_vault_address="0xVAULT", clm_pool_address="0xPOOL",
        dydx_mnemonic="m", dydx_address="d", dydx_network="mainnet", dydx_subaccount=0,
        dydx_symbol_token0="ARB-USD", dydx_symbol_token1="ETH-USD",
        alert_webhook_url="", max_open_orders=200, hedge_ratio=1.0,
        threshold_aggressive=0.01, active_exchange="dydx",
        pool_token0_symbol="ARB", pool_token1_symbol="WETH",
        uniswap_v3_router_address="0xR",
        token0_address="0xARB", token1_address="0xWETH",
        token0_decimals=18, token1_decimals=18,
        slippage_bps=30, uniswap_v3_pool_fee=3000,
    )


@pytest.mark.asyncio
async def test_bootstrap_dual_leg_does_two_swaps_sequentially(cross_pair_settings):
    hub = MagicMock()
    hub.dydx_collateral = 130.0
    hub.hedge_ratio = 1.0
    db = MagicMock()
    db.get_active_operation = AsyncMock(return_value=None)
    db.insert_operation = AsyncMock(return_value=42)
    db.update_bootstrap_state = AsyncMock()
    db.update_operation_status = AsyncMock()
    db.update_baseline_amounts = AsyncMock()
    db.add_to_operation_accumulator = AsyncMock()
    # The spec writes baseline_token0/token1_usd_price via raw conn UPDATE.
    # Mock _conn so the SQL execute + commit doesn't crash.
    db._conn = MagicMock()
    db._conn.execute = AsyncMock()
    db._conn.commit = AsyncMock()

    exchange = MagicMock()
    exchange.place_long_term_order = AsyncMock()
    exchange.get_oracle_prices = AsyncMock(return_value={"ARB-USD": 1.50, "ETH-USD": 4000.0})

    swap_calls = []
    uniswap = MagicMock()
    uniswap._w3 = MagicMock()
    uniswap._w3.eth.get_balance = AsyncMock(return_value=10**16)  # 0.01 ETH gas
    uniswap._erc20 = MagicMock(return_value=MagicMock())
    uniswap._erc20.return_value.functions.balanceOf.return_value.call = AsyncMock(return_value=300 * 10**6)
    uniswap.address = "0xWALLET"
    async def _swap(**kwargs):
        swap_calls.append(kwargs)
        return f"0xtx{len(swap_calls)}"
    uniswap.swap_exact_output = _swap
    uniswap.ensure_approval = AsyncMock(return_value=None)

    beefy = MagicMock()
    beefy.ensure_approval = AsyncMock(return_value=None)
    beefy.deposit = AsyncMock(return_value="0xtx_deposit")

    pool = MagicMock()
    pool.read_price = AsyncMock(return_value=0.000375)
    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-201386, tick_upper=-198363,
        amount0=100.0, amount1=0.0375, share=1.0, raw_balance=10**18,
    ))

    lifecycle = OperationLifecycle(
        settings=cross_pair_settings, hub=hub, db=db,
        exchange=exchange, uniswap=uniswap, beefy=beefy,
        pool_reader=pool, beefy_reader=beefy_reader,
        decimals0=18, decimals1=18,
    )

    op_id = await lifecycle.bootstrap(usdc_budget=300.0)
    assert op_id == 42

    # Two swaps were called (USDC->ARB, USDC->WETH), in order
    assert len(swap_calls) == 2
    # First call: token_out is ARB; second is WETH
    assert swap_calls[0]["token_out"] in {"0xARB", cross_pair_settings.token0_address}
    assert swap_calls[1]["token_out"] in {"0xWETH", cross_pair_settings.token1_address}

    # Two short orders on the perps (paralelos via gather)
    assert exchange.place_long_term_order.await_count == 2
    symbols = [c.kwargs["symbol"] for c in exchange.place_long_term_order.await_args_list]
    assert "ARB-USD" in symbols
    assert "ETH-USD" in symbols


@pytest.mark.asyncio
async def test_teardown_dual_leg_closes_both_shorts_parallel(cross_pair_settings):
    hub = MagicMock()
    hub.hedge_ratio = 1.0
    hub.hedge_realized_pnl = 0.0
    hub.hedge_unrealized_pnl = 0.0
    hub.hedge_realized_pnls = {"ARB-USD": 0.0, "ETH-USD": 0.0}
    hub.hedge_unrealized_pnls = {"ARB-USD": 0.0, "ETH-USD": 0.0}
    db = MagicMock()
    op_data = {
        "id": 42, "started_at": 0, "status": "active",
        "bootstrap_state": "active",
        "baseline_eth_price": 4000, "baseline_pool_value_usd": 300,
        "baseline_amount0": 100, "baseline_amount1": 0.0375,
        "baseline_collateral": 130, "perp_fees_paid": 0,
        "funding_paid": 0, "lp_fees_earned": 0, "bootstrap_slippage": 0,
        "baseline_token0_usd_price": 1.50, "baseline_token1_usd_price": 4000,
        "perp_fees_paid_token0": 0, "perp_fees_paid_token1": 0,
        "funding_paid_token0": 0, "funding_paid_token1": 0,
        "final_net_pnl": None, "close_reason": None,
        "usdc_budget": 300, "bootstrap_swap_tx_hash": None,
        "bootstrap_deposit_tx_hash": None,
        "teardown_withdraw_tx_hash": None, "teardown_swap_tx_hash": None,
        "ended_at": None,
    }
    db.get_active_operation = AsyncMock(return_value=dict(op_data))
    db.get_operation = AsyncMock(return_value=dict(op_data))
    db.update_bootstrap_state = AsyncMock()
    db.update_operation_status = AsyncMock()
    db.add_to_operation_accumulator = AsyncMock()
    db.close_operation = AsyncMock()
    db.get_active_grid_orders = AsyncMock(return_value=[])

    exchange = MagicMock()
    exchange.get_oracle_prices = AsyncMock(return_value={"ARB-USD": 1.50, "ETH-USD": 4000.0})
    exchange.get_position = AsyncMock(side_effect=[
        MagicMock(side="short", size=100.0, entry_price=1.50, unrealized_pnl=0.0),  # ARB
        MagicMock(side="short", size=0.0375, entry_price=4000.0, unrealized_pnl=0.0),  # ETH
    ])
    exchange.batch_cancel = AsyncMock(return_value=0)
    exchange.place_long_term_order = AsyncMock()

    uniswap = MagicMock()
    uniswap._w3 = MagicMock()
    uniswap._w3.eth.get_balance = AsyncMock(return_value=10**16)
    uniswap._erc20 = MagicMock(return_value=MagicMock())
    uniswap._erc20.return_value.functions.balanceOf.return_value.call = AsyncMock(return_value=0)
    uniswap.address = "0xWALLET"
    beefy = MagicMock()
    beefy.withdraw = AsyncMock(return_value="0xtx_withdraw")
    pool = MagicMock()
    pool.read_price = AsyncMock(return_value=0.000375)
    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-201386, tick_upper=-198363,
        amount0=100.0, amount1=0.0375, share=1.0, raw_balance=10**18,
    ))

    lifecycle = OperationLifecycle(
        settings=cross_pair_settings, hub=hub, db=db,
        exchange=exchange, uniswap=uniswap, beefy=beefy,
        pool_reader=pool, beefy_reader=beefy_reader,
        decimals0=18, decimals1=18,
    )

    result = await lifecycle.teardown(swap_to_usdc=False)
    assert result["id"] == 42

    # Two close orders (BUY ARB + BUY WETH)
    close_calls = [c for c in exchange.place_long_term_order.await_args_list]
    assert len(close_calls) == 2
    sides = [c.kwargs["side"] for c in close_calls]
    assert sides.count("buy") == 2
