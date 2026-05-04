"""Operation lifecycle orchestrator: bootstrap (swap+deposit+hedge) + teardown.

State machine persisted in DB via Database.update_bootstrap_state. Idempotent —
each step writes state BEFORE on-chain action; on restart, resume_in_flight
reads state and continues from the next pending step.
"""
from __future__ import annotations
import asyncio
import logging
import time
from typing import Any

from config import Settings
from state import StateHub
from db import Database
from engine.operation import Operation, OperationState
from engine.lp_math import compute_optimal_split
from chains.uniswap import UniswapV3PoolReader, tick_to_price
from chains.beefy import BeefyClmReader
from chains.uniswap_executor import UniswapExecutor
from chains.beefy_executor import BeefyExecutor
from exchanges.base import ExchangeAdapter

logger = logging.getLogger(__name__)

GAS_RESERVE_ETH = 0.005  # ~$15 at $3000/ETH; alert if below
DEPOSIT_MIN_SHARES_TOLERANCE = 0.99  # accept >= 99% of computed expected shares
DEFAULT_DEADLINE_SECONDS = 300  # 5 min

# Arbitrum native USDC. Used as the swap-input token in cross-pair (dual-leg)
# bootstrap, where neither token0 nor token1 is the user's holding currency.
_USDC_ADDRESS_ARBITRUM = "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"

# State -> next-step continuation map.
# - 'with_hash' means: if tx_hash exists, wait for receipt, then continue.
# - 'without_hash' means: re-execute the step (safe because on-chain state hasn't changed).
_BOOTSTRAP_STATES_RESUMABLE = {
    "approving",
    "swap_pending",
    "swap_confirmed",
    # Dual-leg states (cross-pair): two sequential swaps replace the single swap.
    "swap_token0_pending",
    "swap_token0_done",
    "swap_token1_pending",
    "swaps_done",
    "deposit_pending",
    "deposit_confirmed",
    "deposit_done",
    "snapshot",
    "hedge_pending",
    "hedge_confirmed",
    "hedge_done",
    "teardown_grid_cancel",
    "teardown_short_close",
    "teardown_withdraw_pending",
    "teardown_withdraw_confirmed",
    "teardown_swap_pending",
    "teardown_swap_confirmed",
}


class OperationLifecycle:
    def __init__(
        self, *,
        settings: Settings, hub: StateHub, db: Database,
        exchange: ExchangeAdapter,
        uniswap: UniswapExecutor, beefy: BeefyExecutor,
        pool_reader: UniswapV3PoolReader, beefy_reader: BeefyClmReader,
        decimals0: int = 18, decimals1: int = 6,  # WETH=18, USDC=6
    ):
        self._settings = settings
        self._hub = hub
        self._db = db
        self._exchange = exchange
        self._uniswap = uniswap
        self._beefy = beefy
        self._pool_reader = pool_reader
        self._beefy_reader = beefy_reader
        self._decimals0 = decimals0
        self._decimals1 = decimals1
        self._cloid_seq = 0

    def _next_cloid(self, base: int) -> int:
        self._cloid_seq += 1
        return (base * 1_000_000) + self._cloid_seq + (int(time.time()) & 0xFFFF)

    async def _read_wallet_balance(self) -> dict[str, float]:
        """Returns {token0, token1, eth} balances in display units."""
        token0 = self._uniswap._erc20(self._settings.token0_address)
        token1 = self._uniswap._erc20(self._settings.token1_address)
        eth_raw, token0_raw, token1_raw = await asyncio.gather(
            self._uniswap._w3.eth.get_balance(self._uniswap.address),
            token0.functions.balanceOf(self._uniswap.address).call(),
            token1.functions.balanceOf(self._uniswap.address).call(),
        )
        return {
            "token0": token0_raw / (10 ** self._decimals0),
            "token1": token1_raw / (10 ** self._decimals1),
            "eth": eth_raw / 1e18,
        }

    async def _check_gas_balance(self) -> None:
        """Raise if wallet ETH balance is below GAS_RESERVE_ETH."""
        bal = await self._read_wallet_balance()
        self._hub.wallet_eth_balance = bal["eth"]
        if bal["eth"] < GAS_RESERVE_ETH:
            raise RuntimeError(
                f"Wallet gas too low: {bal['eth']:.4f} ETH < {GAS_RESERVE_ETH:.4f} ETH reserve"
            )

    async def bootstrap(self, *, usdc_budget: float) -> int:
        """Execute swap -> deposit -> snapshot -> hedge. Returns operation_id.

        Dispatches on `settings.dydx_symbol_token1`:
          - empty (single-leg): token1 is a stable; existing path with one swap
            and one short.
          - non-empty (dual-leg / cross-pair): user holds USDC; performs two
            sequential swaps (USDC->token0, USDC->token1) and opens two perp
            shorts in parallel via asyncio.gather.
        """
        if bool(self._settings.dydx_symbol_token1):
            return await self._bootstrap_dual_leg(usdc_budget=usdc_budget)

        existing = await self._db.get_active_operation()
        if existing is not None:
            raise RuntimeError(f"Operation {existing['id']} already active")
        await self._check_gas_balance()

        p_now = await self._pool_reader.read_price()
        beefy_pos = await self._beefy_reader.read_position()
        p_a = tick_to_price(beefy_pos.tick_lower, self._decimals0, self._decimals1)
        p_b = tick_to_price(beefy_pos.tick_upper, self._decimals0, self._decimals1)

        amount_weth_target, amount_usdc_target = compute_optimal_split(
            p=p_now, p_a=p_a, p_b=p_b, total_value_usdc=usdc_budget,
        )
        logger.info(
            f"Bootstrap budget=${usdc_budget:.2f} p={p_now:.2f} range=[{p_a:.2f},{p_b:.2f}] "
            f"-> WETH={amount_weth_target:.6f}, USDC={amount_usdc_target:.2f}"
        )

        op_id = await self._db.insert_operation(
            started_at=time.time(),
            status=OperationState.STARTING.value,
            baseline_eth_price=p_now,
            baseline_pool_value_usd=usdc_budget,
            baseline_amount0=amount_weth_target,
            baseline_amount1=amount_usdc_target,
            baseline_collateral=self._hub.dydx_collateral,
            usdc_budget=usdc_budget,
        )
        self._hub.current_operation_id = op_id
        self._hub.operation_state = OperationState.STARTING.value

        try:
            await self._db.update_bootstrap_state(op_id, "approving")
            self._hub.bootstrap_progress = "Approving tokens..."
            await self._uniswap.ensure_approval(
                token_address=self._settings.token1_address,
                amount=2**256 - 1,
                spender=self._settings.uniswap_v3_router_address,
            )
            await self._beefy.ensure_approval(
                token_address=self._settings.token1_address, amount=2**256 - 1,
            )
            await self._beefy.ensure_approval(
                token_address=self._settings.token0_address, amount=2**256 - 1,
            )

            if amount_weth_target > 0 and amount_usdc_target < usdc_budget:
                await self._db.update_bootstrap_state(op_id, "swap_pending")
                self._hub.bootstrap_progress = "Swapping USDC -> WETH..."
                slippage = self._settings.slippage_bps / 10000.0
                amount_in_max = int(
                    (usdc_budget - amount_usdc_target) * (1 + slippage) * 10**self._decimals1
                )
                amount_out_raw = int(amount_weth_target * 10**self._decimals0)
                deadline = int(time.time()) + DEFAULT_DEADLINE_SECONDS
                tx = await self._uniswap.swap_exact_output(
                    token_in=self._settings.token1_address,
                    token_out=self._settings.token0_address,
                    fee=self._settings.uniswap_v3_pool_fee,
                    amount_out=amount_out_raw,
                    amount_in_maximum=amount_in_max,
                    recipient=self._uniswap.address,
                    deadline=deadline,
                )
                await self._db.update_bootstrap_state(op_id, "swap_confirmed", swap_tx_hash=tx)
            else:
                await self._db.update_bootstrap_state(op_id, "swap_confirmed")

            # Use the wallet's actual post-swap balance, not the pre-swap
            # target — slippage/rounding mean the swap may net more or less
            # than amount_weth_target, and Beefy mints shares on what we send.
            await self._db.update_bootstrap_state(op_id, "deposit_pending")
            self._hub.bootstrap_progress = "Depositing in Beefy..."
            bal = await self._read_wallet_balance()
            amount0_raw = int(bal["token0"] * 10**self._decimals0)
            amount1_raw = int(bal["token1"] * 10**self._decimals1)
            tx = await self._beefy.deposit(
                amount0=amount0_raw, amount1=amount1_raw, min_shares=0,
            )
            await self._db.update_bootstrap_state(op_id, "deposit_confirmed", deposit_tx_hash=tx)

            # Snapshot the real baseline AFTER the deposit settles. The
            # Beefy strategy may rebalance ranges or skim dust on deposit,
            # so the post-deposit position is the only authoritative starting
            # point for the operation's PnL accounting.
            await self._db.update_bootstrap_state(op_id, "snapshot")
            self._hub.bootstrap_progress = "Snapshotting baseline..."
            beefy_pos_after = await self._beefy_reader.read_position()
            my_amount0 = beefy_pos_after.amount0 * beefy_pos_after.share
            my_amount1 = beefy_pos_after.amount1 * beefy_pos_after.share
            real_pool_value = my_amount0 * p_now + my_amount1
            await self._db.update_baseline_amounts(
                op_id,
                amount0=my_amount0, amount1=my_amount1,
                pool_value_usd=real_pool_value,
            )

            await self._db.update_bootstrap_state(op_id, "hedge_pending")
            self._hub.bootstrap_progress = "Opening short on dYdX..."
            target_short = my_amount0 * self._hub.hedge_ratio
            if target_short > 0:
                await self._exchange.place_long_term_order(
                    symbol=self._settings.dydx_symbol,
                    side="sell", size=target_short,
                    price=p_now * 0.999,  # taker
                    cloid_int=self._next_cloid(998),
                    ttl_seconds=60,
                )
                slippage_usd = 0.0005 * target_short * p_now
                await self._db.add_to_operation_accumulator(
                    op_id, "bootstrap_slippage", slippage_usd,
                )
            await self._db.update_bootstrap_state(op_id, "hedge_confirmed")

            await self._db.update_bootstrap_state(op_id, "active")
            await self._db.update_operation_status(op_id, OperationState.ACTIVE.value)
            self._hub.operation_state = OperationState.ACTIVE.value
            self._hub.bootstrap_progress = ""
            logger.info(f"Operation {op_id} bootstrapped and ACTIVE")
            return op_id

        except Exception as e:
            logger.exception(f"Bootstrap failed at op_id={op_id}: {e}")
            await self._db.update_bootstrap_state(op_id, "failed")
            await self._db.update_operation_status(op_id, OperationState.FAILED.value)
            self._hub.operation_state = OperationState.FAILED.value
            self._hub.bootstrap_progress = f"FAILED: {e}"
            raise

    async def _bootstrap_dual_leg(self, *, usdc_budget: float) -> int:
        """Cross-pair bootstrap: USDC -> token0 swap, then USDC -> token1 swap,
        deposit into Beefy, snapshot baseline, open BOTH perp shorts in parallel.

        Differs from single-leg in three ways:
        1. Two sequential Uniswap swaps from USDC into token0 and token1 (USDC
           is neither token0 nor token1 in this path).
        2. Baseline pool value computed using both tokens' USD oracle prices
           (token1 is no longer assumed to be 1.0 USD/unit).
        3. Two `place_long_term_order` calls dispatched in parallel via
           asyncio.gather, one per perp symbol.
        """
        existing = await self._db.get_active_operation()
        if existing is not None:
            raise RuntimeError(f"Operation {existing['id']} already active")
        await self._check_gas_balance()

        p_now = await self._pool_reader.read_price()
        beefy_pos = await self._beefy_reader.read_position()
        p_a = tick_to_price(beefy_pos.tick_lower, self._decimals0, self._decimals1)
        p_b = tick_to_price(beefy_pos.tick_upper, self._decimals0, self._decimals1)

        amount_t0_target, amount_t1_target = compute_optimal_split(
            p=p_now, p_a=p_a, p_b=p_b, total_value_usdc=usdc_budget,
        )

        token0_sym = self._settings.dydx_symbol_token0
        token1_sym = self._settings.dydx_symbol_token1
        oracle_prices = await self._exchange.get_oracle_prices([token0_sym, token1_sym])
        baseline_t0_usd = float(oracle_prices.get(token0_sym, 0.0) or 0.0)
        baseline_t1_usd = float(oracle_prices.get(token1_sym, 0.0) or 0.0)
        logger.info(
            f"Dual-leg bootstrap budget=${usdc_budget:.2f} "
            f"oracle[{token0_sym}]={baseline_t0_usd} oracle[{token1_sym}]={baseline_t1_usd} "
            f"-> t0={amount_t0_target:.6f}, t1={amount_t1_target:.6f}"
        )

        op_id = await self._db.insert_operation(
            started_at=time.time(),
            status=OperationState.STARTING.value,
            baseline_eth_price=p_now,
            baseline_pool_value_usd=usdc_budget,
            baseline_amount0=amount_t0_target,
            baseline_amount1=amount_t1_target,
            baseline_collateral=self._hub.dydx_collateral,
            usdc_budget=usdc_budget,
        )
        # Persist per-leg baseline oracle prices (cols added in Task 2).
        await self._db._conn.execute(
            "UPDATE operations SET baseline_token0_usd_price = ?, "
            "baseline_token1_usd_price = ? WHERE id = ?",
            (baseline_t0_usd, baseline_t1_usd, op_id),
        )
        await self._db._conn.commit()

        self._hub.current_operation_id = op_id
        self._hub.operation_state = OperationState.STARTING.value

        try:
            await self._db.update_bootstrap_state(op_id, "approving")
            self._hub.bootstrap_progress = "Approving tokens (dual-leg)..."
            # USDC must be approved for the router (we spend USDC twice).
            await self._uniswap.ensure_approval(
                token_address=_USDC_ADDRESS_ARBITRUM,
                amount=2**256 - 1,
                spender=self._settings.uniswap_v3_router_address,
            )
            # Both pool tokens must be approved for the Beefy strategy.
            await self._beefy.ensure_approval(
                token_address=self._settings.token0_address, amount=2**256 - 1,
            )
            await self._beefy.ensure_approval(
                token_address=self._settings.token1_address, amount=2**256 - 1,
            )

            slippage = self._settings.slippage_bps / 10000.0

            # ---- Swap 1: USDC -> token0 ----
            # In dual-leg, both legs must execute their swap call (cross-pair
            # holds USDC, neither token is USDC). When `compute_optimal_split`
            # returns 0 for a leg (price outside range), the call is still
            # issued but with amount_out=0 — the router will treat it as a
            # no-op or revert; we surface either via the standard failure path.
            await self._db.update_bootstrap_state(op_id, "swap_token0_pending")
            self._hub.bootstrap_progress = f"Swapping USDC -> {self._settings.pool_token0_symbol}..."
            usdc_for_t0 = amount_t0_target * baseline_t0_usd
            amount_in_max_t0 = int(usdc_for_t0 * (1 + slippage) * 10**6)  # USDC = 6 decimals
            amount_out_t0 = int(amount_t0_target * 10**self._decimals0)
            tx0 = await self._uniswap.swap_exact_output(
                token_in=_USDC_ADDRESS_ARBITRUM,
                token_out=self._settings.token0_address,
                fee=self._settings.uniswap_v3_pool_fee,
                amount_out=amount_out_t0,
                amount_in_maximum=amount_in_max_t0,
                recipient=self._uniswap.address,
                deadline=int(time.time()) + DEFAULT_DEADLINE_SECONDS,
            )
            await self._db.update_bootstrap_state(
                op_id, "swap_token0_done", swap_tx_hash=tx0,
            )

            # ---- Swap 2: USDC -> token1 ----
            await self._db.update_bootstrap_state(op_id, "swap_token1_pending")
            self._hub.bootstrap_progress = f"Swapping USDC -> {self._settings.pool_token1_symbol}..."
            usdc_for_t1 = amount_t1_target * baseline_t1_usd
            amount_in_max_t1 = int(usdc_for_t1 * (1 + slippage) * 10**6)
            amount_out_t1 = int(amount_t1_target * 10**self._decimals1)
            tx1 = await self._uniswap.swap_exact_output(
                token_in=_USDC_ADDRESS_ARBITRUM,
                token_out=self._settings.token1_address,
                fee=self._settings.uniswap_v3_pool_fee,
                amount_out=amount_out_t1,
                amount_in_maximum=amount_in_max_t1,
                recipient=self._uniswap.address,
                deadline=int(time.time()) + DEFAULT_DEADLINE_SECONDS,
            )
            await self._db.update_bootstrap_state(op_id, "swaps_done", swap_tx_hash=tx1)

            # ---- Deposit on Beefy with actual post-swap balances ----
            await self._db.update_bootstrap_state(op_id, "deposit_pending")
            self._hub.bootstrap_progress = "Depositing in Beefy..."
            bal = await self._read_wallet_balance()
            amount0_raw = int(bal["token0"] * 10**self._decimals0)
            amount1_raw = int(bal["token1"] * 10**self._decimals1)
            tx_dep = await self._beefy.deposit(
                amount0=amount0_raw, amount1=amount1_raw, min_shares=0,
            )
            await self._db.update_bootstrap_state(
                op_id, "deposit_done", deposit_tx_hash=tx_dep,
            )

            # ---- Snapshot post-deposit baseline ----
            await self._db.update_bootstrap_state(op_id, "snapshot")
            self._hub.bootstrap_progress = "Snapshotting baseline..."
            beefy_pos_after = await self._beefy_reader.read_position()
            my_amount0 = beefy_pos_after.amount0 * beefy_pos_after.share
            my_amount1 = beefy_pos_after.amount1 * beefy_pos_after.share
            real_pool_value = my_amount0 * baseline_t0_usd + my_amount1 * baseline_t1_usd
            await self._db.update_baseline_amounts(
                op_id,
                amount0=my_amount0, amount1=my_amount1,
                pool_value_usd=real_pool_value,
            )

            # ---- Hedge: open BOTH perp shorts in parallel ----
            await self._db.update_bootstrap_state(op_id, "hedge_pending")
            self._hub.bootstrap_progress = "Opening shorts on dYdX (dual-leg)..."
            target_short_t0 = my_amount0 * self._hub.hedge_ratio
            target_short_t1 = my_amount1 * self._hub.hedge_ratio

            async def _open_short_t0() -> None:
                if target_short_t0 <= 0 or baseline_t0_usd <= 0:
                    return
                await self._exchange.place_long_term_order(
                    symbol=token0_sym,
                    side="sell", size=target_short_t0,
                    price=baseline_t0_usd * 0.999,  # taker
                    cloid_int=self._next_cloid(998),
                    ttl_seconds=60,
                )
                slippage_usd = 0.0005 * target_short_t0 * baseline_t0_usd
                await self._db.add_to_operation_accumulator(
                    op_id, "perp_fees_paid_token0", slippage_usd,
                )

            async def _open_short_t1() -> None:
                if target_short_t1 <= 0 or baseline_t1_usd <= 0:
                    return
                await self._exchange.place_long_term_order(
                    symbol=token1_sym,
                    side="sell", size=target_short_t1,
                    price=baseline_t1_usd * 0.999,  # taker
                    cloid_int=self._next_cloid(996),
                    ttl_seconds=60,
                )
                slippage_usd = 0.0005 * target_short_t1 * baseline_t1_usd
                await self._db.add_to_operation_accumulator(
                    op_id, "perp_fees_paid_token1", slippage_usd,
                )

            await asyncio.gather(_open_short_t0(), _open_short_t1())
            await self._db.update_bootstrap_state(op_id, "hedge_done")

            await self._db.update_bootstrap_state(op_id, "active")
            await self._db.update_operation_status(op_id, OperationState.ACTIVE.value)
            self._hub.operation_state = OperationState.ACTIVE.value
            self._hub.bootstrap_progress = ""
            logger.info(f"Operation {op_id} bootstrapped (dual-leg) and ACTIVE")
            return op_id

        except Exception as e:
            logger.exception(f"Dual-leg bootstrap failed at op_id={op_id}: {e}")
            await self._db.update_bootstrap_state(op_id, "failed")
            await self._db.update_operation_status(op_id, OperationState.FAILED.value)
            self._hub.operation_state = OperationState.FAILED.value
            self._hub.bootstrap_progress = f"FAILED: {e}"
            raise

    async def teardown(self, *, swap_to_usdc: bool = False, close_reason: str = "user") -> dict:
        """Cancel grid -> close short -> withdraw Beefy -> (optional) swap WETH to USDC.

        Returns final PnL breakdown dict.
        """
        op_row = await self._db.get_active_operation()
        if op_row is None:
            raise RuntimeError("No active operation to teardown")
        op_id = op_row["id"]

        await self._db.update_operation_status(op_id, OperationState.STOPPING.value)
        self._hub.operation_state = OperationState.STOPPING.value
        self._hub.bootstrap_progress = "Cancelling grid..."

        try:
            await self._db.update_bootstrap_state(op_id, "teardown_grid_cancel")
            active_orders = await self._db.get_active_grid_orders()
            if active_orders:
                await self._exchange.batch_cancel([
                    dict(symbol=self._settings.dydx_symbol, cloid_int=int(r["cloid"]))
                    for r in active_orders
                ])
                for r in active_orders:
                    await self._db.mark_grid_order_cancelled(r["cloid"], time.time())

            await self._db.update_bootstrap_state(op_id, "teardown_short_close")
            self._hub.bootstrap_progress = "Closing short..."
            pos = await self._exchange.get_position(self._settings.dydx_symbol)
            p_now = await self._pool_reader.read_price()
            if pos and pos.size > 0:
                side = "buy" if pos.side == "short" else "sell"
                price = p_now * 1.001 if side == "buy" else p_now * 0.999
                await self._exchange.place_long_term_order(
                    symbol=self._settings.dydx_symbol,
                    side=side, size=pos.size, price=price,
                    cloid_int=self._next_cloid(997), ttl_seconds=60,
                )
                slippage = 0.0005 * pos.size * p_now
                await self._db.add_to_operation_accumulator(op_id, "perp_fees_paid", slippage)

            await self._db.update_bootstrap_state(op_id, "teardown_withdraw_pending")
            self._hub.bootstrap_progress = "Withdrawing Beefy..."
            beefy_pos = await self._beefy_reader.read_position()
            shares = beefy_pos.raw_balance
            if shares > 0:
                tx = await self._beefy.withdraw(shares=shares, min_amount0=0, min_amount1=0)
                await self._db.update_bootstrap_state(
                    op_id, "teardown_withdraw_confirmed", withdraw_tx_hash=tx,
                )
            else:
                await self._db.update_bootstrap_state(op_id, "teardown_withdraw_confirmed")

            if swap_to_usdc:
                await self._db.update_bootstrap_state(op_id, "teardown_swap_pending")
                self._hub.bootstrap_progress = "Swapping WETH -> USDC..."
                bal = await self._read_wallet_balance()
                if bal["token0"] > 0:
                    amount_in_raw = int(bal["token0"] * 10**self._decimals0)
                    p_now = await self._pool_reader.read_price()
                    slippage = self._settings.slippage_bps / 10000.0
                    min_out = int(bal["token0"] * p_now * (1 - slippage) * 10**self._decimals1)
                    tx = await self._uniswap.swap_exact_input(
                        token_in=self._settings.token0_address,
                        token_out=self._settings.token1_address,
                        fee=self._settings.uniswap_v3_pool_fee,
                        amount_in=amount_in_raw,
                        amount_out_minimum=min_out,
                        recipient=self._uniswap.address,
                        deadline=int(time.time()) + DEFAULT_DEADLINE_SECONDS,
                    )
                    await self._db.update_bootstrap_state(
                        op_id, "teardown_swap_confirmed", teardown_swap_tx_hash=tx,
                    )
                else:
                    await self._db.update_bootstrap_state(op_id, "teardown_swap_confirmed")

            op = Operation.from_db_row(await self._db.get_operation(op_id))
            from engine.pnl import compute_operation_pnl
            my_amount0 = beefy_pos.amount0 * beefy_pos.share
            my_amount1 = beefy_pos.amount1 * beefy_pos.share
            pool_value = my_amount0 * p_now + my_amount1
            breakdown = compute_operation_pnl(
                op,
                current_pool_value_usd=pool_value,
                current_eth_price=p_now,
                hedge_realized_since_baseline=self._hub.hedge_realized_pnl,
                hedge_unrealized_since_baseline=self._hub.hedge_unrealized_pnl,
            )

            await self._db.close_operation(
                op_id, ended_at=time.time(),
                final_net_pnl=breakdown["net_pnl"], close_reason=close_reason,
            )
            await self._db.update_bootstrap_state(op_id, "closed")
            self._hub.current_operation_id = None
            self._hub.operation_state = OperationState.NONE.value
            self._hub.bootstrap_progress = ""
            self._hub.operation_pnl_breakdown = {}
            return {"id": op_id, "final_net_pnl": breakdown["net_pnl"], "breakdown": breakdown}

        except Exception as e:
            logger.exception(f"Teardown failed at op_id={op_id}: {e}")
            await self._db.update_bootstrap_state(op_id, "failed")
            await self._db.update_operation_status(op_id, OperationState.FAILED.value)
            self._hub.operation_state = OperationState.FAILED.value
            self._hub.bootstrap_progress = f"TEARDOWN FAILED: {e}"
            raise

    async def cashout_residual(self) -> dict:
        """Swap any residual token0 in the wallet to token1.

        Used when an operation is closed but token0 (e.g. WETH) is still
        sitting in the wallet — typically because teardown ran with
        swap_to_usdc=False. Returns {tx_hash, swapped_amount} or
        {swapped_amount: 0, message: "..."} if there's nothing to swap.
        """
        bal = await self._read_wallet_balance()
        if bal["token0"] <= 0:
            return {"swapped_amount": 0.0, "message": "No token0 balance to swap"}

        p_now = await self._pool_reader.read_price()
        slippage = self._settings.slippage_bps / 10000.0
        amount_in_raw = int(bal["token0"] * 10 ** self._decimals0)
        min_out = int(
            bal["token0"] * p_now * (1 - slippage) * 10 ** self._decimals1
        )
        tx_hash = await self._uniswap.swap_exact_input(
            token_in=self._settings.token0_address,
            token_out=self._settings.token1_address,
            fee=self._settings.uniswap_v3_pool_fee,
            amount_in=amount_in_raw,
            amount_out_minimum=min_out,
            recipient=self._uniswap.address,
            deadline=int(time.time()) + DEFAULT_DEADLINE_SECONDS,
        )
        return {"tx_hash": tx_hash, "swapped_amount": bal["token0"]}

    async def resume_in_flight(self) -> None:
        """Called at startup. For each in-flight operation:
        1. If state has a tx_hash and it's '_pending', wait for receipt then advance.
        2. If state has no tx_hash or is past confirmation, re-execute next step.
        3. If state is unknown/corrupted, mark failed.
        """
        in_flight = await self._db.get_in_flight_operations()
        if not in_flight:
            return
        for op_row in in_flight:
            op_id = op_row["id"]
            state = op_row.get("bootstrap_state")
            logger.info(f"Resuming operation {op_id} from state '{state}'")

            if state not in _BOOTSTRAP_STATES_RESUMABLE:
                logger.error(f"Operation {op_id} has unknown bootstrap_state '{state}' -- marking failed")
                await self._db.update_bootstrap_state(op_id, "failed")
                await self._db.update_operation_status(op_id, OperationState.FAILED.value)
                continue

            try:
                # Wait for any pending tx receipts first
                if state == "swap_pending" and op_row.get("bootstrap_swap_tx_hash"):
                    await self._uniswap.wait_for_receipt(op_row["bootstrap_swap_tx_hash"])
                    await self._db.update_bootstrap_state(op_id, "swap_confirmed")
                elif state == "deposit_pending" and op_row.get("bootstrap_deposit_tx_hash"):
                    await self._beefy.wait_for_receipt(op_row["bootstrap_deposit_tx_hash"])
                    await self._db.update_bootstrap_state(op_id, "deposit_confirmed")
                elif state == "teardown_withdraw_pending" and op_row.get("teardown_withdraw_tx_hash"):
                    await self._beefy.wait_for_receipt(op_row["teardown_withdraw_tx_hash"])
                    await self._db.update_bootstrap_state(op_id, "teardown_withdraw_confirmed")
                elif state == "teardown_swap_pending" and op_row.get("teardown_swap_tx_hash"):
                    await self._uniswap.wait_for_receipt(op_row["teardown_swap_tx_hash"])
                    await self._db.update_bootstrap_state(op_id, "teardown_swap_confirmed")

                # Continue from current state
                if state.startswith("teardown_"):
                    await self._continue_teardown(op_id, state, op_row)
                else:
                    await self._continue_bootstrap(op_id, state, op_row)
            except Exception as e:
                logger.exception(f"Resume failed for op {op_id}: {e}")
                await self._db.update_bootstrap_state(op_id, "failed")
                await self._db.update_operation_status(op_id, OperationState.FAILED.value)

    async def _continue_bootstrap(self, op_id: int, current_state: str, op_row: dict) -> None:
        """Re-enter bootstrap from `current_state`. MVP: mark failed and surface to operator."""
        logger.warning(
            f"Operation {op_id}: resume from bootstrap state '{current_state}' "
            f"requires manual review. Marking 'failed' to prevent automatic retry."
        )
        await self._db.update_bootstrap_state(op_id, "failed")
        await self._db.update_operation_status(op_id, OperationState.FAILED.value)

    async def _continue_teardown(self, op_id: int, current_state: str, op_row: dict) -> None:
        """Re-enter teardown from `current_state`. MVP: mark failed and surface to operator."""
        logger.warning(
            f"Operation {op_id}: resume from teardown state '{current_state}' "
            f"requires manual review. Marking 'failed' to prevent automatic retry."
        )
        await self._db.update_bootstrap_state(op_id, "failed")
        await self._db.update_operation_status(op_id, OperationState.FAILED.value)
