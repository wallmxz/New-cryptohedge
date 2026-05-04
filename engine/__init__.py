from __future__ import annotations
import asyncio
import time
import logging
from typing import TYPE_CHECKING
from state import StateHub
from db import Database
from config import Settings
from math import sqrt
from chains.uniswap import UniswapV3PoolReader, tick_to_price
from chains.beefy import BeefyClmReader
from exchanges.dydx import DydxAdapter
from exchanges.base import ExchangeAdapter
from engine.curve import compute_l_from_value, compute_x, compute_y, compute_target_grid, GridLevel
from engine.grid import GridManager
from engine.operation import Operation, OperationState
from engine.pnl import compute_operation_pnl
from engine.reconciler import Reconciler
from engine.margin import compute_required_collateral, compute_margin_ratio, classify_margin
from engine import metrics
from web.alerts import post_alert
from web3 import AsyncWeb3, AsyncHTTPProvider

if TYPE_CHECKING:
    from engine.lifecycle import OperationLifecycle

logger = logging.getLogger(__name__)


class GridMakerEngine:
    """Main loop:
    1. Read pool position (Beefy + Uniswap pool)
    2. Compute target grid via curve math
    3. Diff against current grid
    4. Cancel + place via exchange adapter
    5. Reconcile + monitor margin
    """
    def __init__(
        self, *, settings: Settings, hub: StateHub, db: Database,
        exchange: ExchangeAdapter | None = None,
        pool_reader: UniswapV3PoolReader | None = None,
        beefy_reader: BeefyClmReader | None = None,
        lifecycle: "OperationLifecycle | None" = None,
        pair_factory_w3=None,
        pair_factory_account=None,
        decimals0: int = 18, decimals1: int = 6,
    ):
        self._settings = settings
        self._hub = hub
        self._db = db
        self._exchange = exchange
        self._pool_reader = pool_reader
        self._beefy_reader = beefy_reader
        self._lifecycle = lifecycle
        self._pair_factory_w3 = pair_factory_w3
        self._pair_factory_account = pair_factory_account
        self._decimals0 = decimals0
        self._decimals1 = decimals1
        self._grid_mgr = GridManager()
        self._task: asyncio.Task | None = None
        self._running = False
        self._cloid_seq = 0
        self._run_id = int(time.time())  # unique per process run
        self._reconciler: Reconciler | None = None
        self._iter_count = 0
        self.RECONCILE_EVERY_N_ITERATIONS = 30  # ~30s
        self._last_alert_level: str | None = None
        # Cooldown for aggressive correction: prevents re-firing the same
        # taker correction every iteration while a previous one is still in
        # flight (taker on real dYdX fills in ~100ms, but on the simulator
        # mock or under network latency it can lag). Without this, multiple
        # corrections stack up and explode the position when price crosses.
        self._last_aggressive_correction_at: float = 0.0
        self.AGGRESSIVE_CORRECTION_COOLDOWN_SECONDS = 30.0

    def _ensure_reconciler(self):
        if self._reconciler is None and self._exchange is not None:
            self._reconciler = Reconciler(
                db=self._db, exchange=self._exchange, settings=self._settings,
            )
        return self._reconciler

    async def start_operation(self, *, usdc_budget: float | None = None) -> int:
        """Begin a new operation. Routes via pair_factory > singleton lifecycle > legacy.

        When pair_factory is configured AND a vault is selected in DB, build a
        fresh lifecycle for that vault and bootstrap. When the singleton
        lifecycle is configured (Phase 2.0), use it. Otherwise fall back to the
        legacy Phase 1.2 snapshot-only path.

        usdc_budget is REQUIRED when either lifecycle path is used. Missing
        budget raises RuntimeError to prevent silent fallback.
        """
        # NEW: Pair-picker path (if user has selected a pair via UI)
        selected_vault_id = await self._db.get_selected_vault_id()
        if (
            selected_vault_id
            and self._pair_factory_w3 is not None
            and self._pair_factory_account is not None
        ):
            if usdc_budget is None:
                raise RuntimeError(
                    "usdc_budget required when pair is selected. "
                    "Pass {usdc_budget: <float>} in request body."
                )
            from engine.pair_factory import build_lifecycle
            lifecycle = await build_lifecycle(
                settings=self._settings, hub=self._hub, db=self._db,
                exchange=self._exchange,
                selected_vault_id=selected_vault_id,
                w3=self._pair_factory_w3,
                account=self._pair_factory_account,
            )
            return await lifecycle.bootstrap(usdc_budget=usdc_budget)

        # Phase 2.0 path: singleton lifecycle if configured
        if self._lifecycle is not None:
            if usdc_budget is None:
                raise RuntimeError(
                    "usdc_budget required when lifecycle is configured. "
                    "Pass {usdc_budget: <float>} in request body."
                )
            return await self._lifecycle.bootstrap(usdc_budget=usdc_budget)

        # Legacy path: existing Phase 1.2 behavior (no on-chain bootstrap)
        existing = await self._db.get_active_operation()
        if existing is not None:
            raise RuntimeError(f"Operation {existing['id']} already active")

        # Snapshot baseline
        p_now = await self._pool_reader.read_price()
        beefy_pos = await self._beefy_reader.read_position()
        my_amount0 = beefy_pos.amount0 * beefy_pos.share
        my_amount1 = beefy_pos.amount1 * beefy_pos.share
        pool_value = my_amount0 * p_now + my_amount1
        try:
            collateral = await self._exchange.get_collateral()
        except Exception:
            collateral = 0.0

        op_id = await self._db.insert_operation(
            started_at=time.time(), status=OperationState.STARTING.value,
            baseline_eth_price=p_now,
            baseline_pool_value_usd=pool_value,
            baseline_amount0=my_amount0,
            baseline_amount1=my_amount1,
            baseline_collateral=collateral,
        )
        self._hub.current_operation_id = op_id
        self._hub.operation_state = OperationState.STARTING.value

        # Bootstrap: open short = my_amount0 * hedge_ratio via taker
        target_short = my_amount0 * self._hub.hedge_ratio
        if target_short > 0:
            try:
                await self._exchange.place_long_term_order(
                    symbol=self._settings.dydx_symbol,
                    side="sell", size=target_short,
                    price=p_now * 0.999,  # cross spread (taker)
                    cloid_int=self._next_cloid(998),
                    ttl_seconds=60,
                )
                # Slippage estimate: 5 bps of notional
                slippage = 0.0005 * target_short * p_now
                await self._db.add_to_operation_accumulator(
                    op_id, "bootstrap_slippage", slippage,
                )
            except Exception as e:
                logger.exception(f"Bootstrap failed: {e}")
                await self._db.update_operation_status(op_id, OperationState.FAILED.value)
                metrics.operations_total.labels(status="failed").inc()
                self._hub.operation_state = OperationState.FAILED.value
                self._hub.current_operation_id = None
                raise

        await self._db.update_operation_status(op_id, OperationState.ACTIVE.value)
        metrics.operations_total.labels(status="started").inc()
        self._hub.operation_state = OperationState.ACTIVE.value
        logger.info(f"Operation {op_id} started")
        return op_id

    async def stop_operation(
        self, *, close_reason: str = "user", swap_to_usdc: bool = False,
    ) -> dict:
        """Stop the active operation. Routes via pair_factory > singleton > legacy.

        If this op was bootstrapped through any lifecycle, do full teardown
        (preferring pair_factory when available, else singleton). Otherwise
        legacy path.

        Legacy ops (Phase 1.2) have bootstrap_state='pending' (default) since they
        never went through lifecycle.bootstrap. Routing them through lifecycle.teardown
        would call beefy.withdraw on the user's full vault balance, draining shares
        not deposited by this op. Gate on bootstrap_state to prevent that.
        """
        op_row = await self._db.get_active_operation()
        bootstrap_state = (op_row or {}).get("bootstrap_state") or "pending"
        op_was_bootstrapped_via_lifecycle = bootstrap_state not in ("pending", None)

        # If op went through lifecycle, route teardown via lifecycle
        # (factory or singleton).
        if op_was_bootstrapped_via_lifecycle:
            # NEW: try pair_factory path first
            selected_vault_id = await self._db.get_selected_vault_id()
            if (
                selected_vault_id
                and self._pair_factory_w3 is not None
                and self._pair_factory_account is not None
            ):
                from engine.pair_factory import build_lifecycle
                lifecycle = await build_lifecycle(
                    settings=self._settings, hub=self._hub, db=self._db,
                    exchange=self._exchange,
                    selected_vault_id=selected_vault_id,
                    w3=self._pair_factory_w3,
                    account=self._pair_factory_account,
                )
                return await lifecycle.teardown(
                    swap_to_usdc=swap_to_usdc, close_reason=close_reason,
                )
            # Fallback to singleton lifecycle (Phase 2.0 path)
            if self._lifecycle is not None:
                return await self._lifecycle.teardown(
                    swap_to_usdc=swap_to_usdc, close_reason=close_reason,
                )

        # Legacy path
        if op_row is None:
            raise RuntimeError("No active operation to stop")
        op_id = op_row["id"]

        await self._db.update_operation_status(op_id, OperationState.STOPPING.value)
        self._hub.operation_state = OperationState.STOPPING.value

        # 1. Cancel all active grid orders
        active_orders = await self._db.get_active_grid_orders()
        if active_orders:
            try:
                await self._exchange.batch_cancel([
                    dict(symbol=self._settings.dydx_symbol, cloid_int=int(r["cloid"]))
                    for r in active_orders
                ])
                for r in active_orders:
                    await self._db.mark_grid_order_cancelled(r["cloid"], time.time())
            except Exception as e:
                logger.error(f"Cancel grid during stop failed: {e}")

        # 2. Close short via taker
        pos = await self._exchange.get_position(self._settings.dydx_symbol)
        if pos and pos.size > 0:
            p_now = await self._pool_reader.read_price()
            side = "buy" if pos.side == "short" else "sell"
            # Cross spread for fast fill
            price = p_now * 1.001 if side == "buy" else p_now * 0.999
            try:
                await self._exchange.place_long_term_order(
                    symbol=self._settings.dydx_symbol,
                    side=side, size=pos.size, price=price,
                    cloid_int=self._next_cloid(997), ttl_seconds=60,
                )
                slippage = 0.0005 * pos.size * p_now
                await self._db.add_to_operation_accumulator(
                    op_id, "perp_fees_paid", slippage,
                )
            except Exception as e:
                logger.exception(f"Close short during stop failed: {e}")

        # 3. Compute final PnL
        op = Operation.from_db_row(await self._db.get_operation(op_id))
        p_now = await self._pool_reader.read_price()
        beefy_pos = await self._beefy_reader.read_position()
        my_amount0 = beefy_pos.amount0 * beefy_pos.share
        my_amount1 = beefy_pos.amount1 * beefy_pos.share
        pool_value = my_amount0 * p_now + my_amount1

        from engine.pnl import compute_operation_pnl
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
        metrics.operations_total.labels(status="closed").inc()
        self._hub.current_operation_id = None
        self._hub.operation_state = OperationState.NONE.value
        self._hub.operation_pnl_breakdown = {}
        logger.info(f"Operation {op_id} closed; final PnL = {breakdown['net_pnl']:.2f}")
        return {"id": op_id, "final_net_pnl": breakdown["net_pnl"], "breakdown": breakdown}

    async def _check_margin_and_alert(self, peak_short_size: float, p_now: float):
        required = compute_required_collateral(
            peak_short_size=peak_short_size, current_price=p_now,
        )
        ratio = compute_margin_ratio(collateral=self._hub.dydx_collateral, required=required)
        self._hub.margin_ratio = ratio
        metrics.margin_ratio.set(ratio)
        level = classify_margin(ratio)

        if level != "healthy" and level != self._last_alert_level:
            await post_alert(
                url=self._settings.alert_webhook_url,
                level=level,
                message=f"Margin ratio is {ratio:.2f} (collateral=${self._hub.dydx_collateral:.2f}, required=${required:.2f})",
                data={"ratio": ratio, "collateral": self._hub.dydx_collateral, "required": required},
            )
            metrics.alerts_total.labels(level=level).inc()
            self._last_alert_level = level
        if level == "healthy":
            self._last_alert_level = None

    async def _maybe_reconcile(self):
        if self._iter_count % self.RECONCILE_EVERY_N_ITERATIONS == 0:
            rec = self._ensure_reconciler()
            if rec is not None:
                try:
                    await rec.reconcile()
                except Exception as e:
                    logger.error(f"Reconciler error: {e}")

    async def start(self):
        if self._exchange is None:
            self._exchange = DydxAdapter(
                mnemonic=self._settings.dydx_mnemonic,
                wallet_address=self._settings.dydx_address,
                network=self._settings.dydx_network,
                subaccount=self._settings.dydx_subaccount,
            )
            await self._exchange.connect()
            self._hub.connected_exchange = True

        if self._pool_reader is None or self._beefy_reader is None:
            w3 = AsyncWeb3(AsyncHTTPProvider(self._settings.arbitrum_rpc_url))
            self._pool_reader = UniswapV3PoolReader(
                w3, self._settings.clm_pool_address, self._decimals0, self._decimals1,
            )
            self._beefy_reader = BeefyClmReader(
                w3, self._settings.clm_vault_address, self._settings.wallet_address,
                self._decimals0, self._decimals1,
            )
            self._hub.connected_chain = True

        # Initial reconciliation on startup, BEFORE main loop begins.
        # Recovers from crashes: cancels orphan orders on exchange, marks
        # lost DB orders as cancelled.
        rec = self._ensure_reconciler()
        if rec is not None:
            try:
                await rec.reconcile()
                logger.info("Initial reconciliation complete")
            except Exception as e:
                logger.error(f"Initial reconciliation failed: {e}")

        # Restore active operation, if any
        active_op = await self._db.get_active_operation()
        if active_op is not None:
            self._hub.current_operation_id = active_op["id"]
            self._hub.operation_state = active_op["status"]
            logger.info(f"Restored active operation {active_op['id']} (status={active_op['status']})")

        await self._exchange.subscribe_fills(self._settings.dydx_symbol, self._on_fill)

        self._running = True
        self._task = asyncio.create_task(self._main_loop())
        logger.info("GridMakerEngine started")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
        if self._exchange:
            await self._exchange.disconnect()

    async def _main_loop(self):
        # Hold a stable 1Hz cadence: subtract the iteration's elapsed time so
        # a slow iter doesn't compound into a 2Hz/0.5Hz drift.
        period = 1.0
        while self._running:
            iter_start = time.monotonic()
            try:
                await self._iterate()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Engine loop error: {e}")
            elapsed = time.monotonic() - iter_start
            await asyncio.sleep(max(0.0, period - elapsed))

    def _next_cloid(self, level_idx: int) -> int:
        """Generate unique cloid as int (dYdX requires int)."""
        self._cloid_seq += 1
        # Combine run_id (low 16 bits) + level_idx (low 8 bits) + seq (low 8 bits)
        return (
            ((self._run_id & 0xFFFF) << 16) |
            ((level_idx & 0xFF) << 8) |
            (self._cloid_seq & 0xFF)
        )

    async def _iterate(self):
        iter_start = time.monotonic()
        self._iter_count += 1
        timings: dict[str, float] = {}
        try:
            await self._maybe_reconcile()

            t = time.monotonic()
            beefy_pos, p_now = await asyncio.gather(
                self._beefy_reader.read_position(),
                self._pool_reader.read_price(),
            )
            timings["chain_read"] = (time.monotonic() - t) * 1000
            metrics.loop_duration.labels(step="chain_read").observe(timings["chain_read"] / 1000)

            p_a = tick_to_price(beefy_pos.tick_lower, self._decimals0, self._decimals1)
            p_b = tick_to_price(beefy_pos.tick_upper, self._decimals0, self._decimals1)

            my_amount0 = beefy_pos.amount0 * beefy_pos.share
            my_amount1 = beefy_pos.amount1 * beefy_pos.share
            my_value_t1 = my_amount0 * p_now + my_amount1
            if my_value_t1 <= 0:
                return

            # Determine active legs
            symbols = [self._settings.dydx_symbol_token0]
            is_dual_leg = bool(self._settings.dydx_symbol_token1)
            if is_dual_leg:
                symbols.append(self._settings.dydx_symbol_token1)

            # One round-trip each: positions per symbol, oracle prices, collateral
            positions, oracle_prices, collateral = await asyncio.gather(
                asyncio.gather(*[self._safe_get_position(s) for s in symbols]),
                self._exchange.get_oracle_prices(symbols),
                self._safe_get_collateral(),
            )
            if collateral is not None:
                self._hub.dydx_collateral = collateral

            # Update hub state for each leg
            for sym, pos in zip(symbols, positions):
                if pos:
                    self._hub.hedge_positions[sym] = {
                        "side": pos.side, "size": pos.size, "entry": pos.entry_price,
                    }
                    self._hub.hedge_unrealized_pnls[sym] = pos.unrealized_pnl
                    metrics.hedge_position_size.set(pos.size)
                else:
                    self._hub.hedge_positions.pop(sym, None)
                    self._hub.hedge_unrealized_pnls[sym] = 0.0

            # Compute USD pool value
            if is_dual_leg:
                p0_usd = oracle_prices.get(symbols[0], p_now)
                p1_usd = oracle_prices.get(symbols[1], 1.0)
                pool_value_usd = my_amount0 * p0_usd + my_amount1 * p1_usd
            else:
                p0_usd = oracle_prices.get(symbols[0], p_now)
                p1_usd = 1.0
                pool_value_usd = my_value_t1

            metrics.pool_value_usd.set(pool_value_usd)
            self._hub.range_lower = p_a
            self._hub.range_upper = p_b
            self._hub.pool_value_usd = pool_value_usd
            self._hub.pool_tokens = {
                self._settings.pool_token0_symbol: my_amount0,
                self._settings.pool_token1_symbol: my_amount1,
            }

            # Out-of-range: taker-only has no grid to cancel; just idle
            if not (p_a < p_now < p_b):
                self._hub.out_of_range = True
                metrics.out_of_range.set(1)
                return
            self._hub.out_of_range = False
            metrics.out_of_range.set(0)

            L_user = compute_l_from_value(my_value_t1, p_a, p_b, p_now)
            self._hub.liquidity_l = L_user

            if self._hub.operation_state != OperationState.ACTIVE.value:
                return

            # Live PnL breakdown
            if self._hub.current_operation_id is not None:
                try:
                    op_row = await self._db.get_operation(self._hub.current_operation_id)
                    if op_row:
                        op = Operation.from_db_row(op_row)
                        if is_dual_leg:
                            self._hub.operation_pnl_breakdown = compute_operation_pnl(
                                op,
                                current_pool_value_usd=pool_value_usd,
                                current_token0_usd_price=p0_usd,
                                current_token1_usd_price=p1_usd,
                                hedge_realized_per_symbol=dict(self._hub.hedge_realized_pnls),
                                hedge_unrealized_per_symbol=dict(self._hub.hedge_unrealized_pnls),
                            )
                        else:
                            self._hub.operation_pnl_breakdown = compute_operation_pnl(
                                op,
                                current_pool_value_usd=pool_value_usd,
                                current_eth_price=p_now,
                                hedge_realized_since_baseline=self._hub.hedge_realized_pnl,
                                hedge_unrealized_since_baseline=self._hub.hedge_unrealized_pnl,
                            )
                except Exception as e:
                    logger.error(f"PnL breakdown update failed: {e}")

            # Margin check: peak short notional summed across legs, in token1 units
            peak_short_notional_usd = 0.0
            for sym, pos in zip(symbols, positions):
                cur = abs(pos.size) if pos else 0.0
                peak_short_notional_usd += cur * oracle_prices.get(sym, 0.0)
            peak_eth_equiv = peak_short_notional_usd / max(p1_usd, 1e-9)
            await self._check_margin_and_alert(peak_eth_equiv, p1_usd)

            # Compute targets per leg
            targets: dict[str, float] = {
                symbols[0]: compute_x(L_user, p_now, p_b) * self._hub.hedge_ratio,
            }
            if is_dual_leg:
                targets[symbols[1]] = compute_y(L_user, p_now, p_a) * self._hub.hedge_ratio

            # Fire rebalance per leg
            for sym in symbols:
                meta = await self._exchange.get_market_meta(sym)
                idx = symbols.index(sym)
                current = abs(positions[idx].size) if positions[idx] else 0.0
                ref_price = oracle_prices.get(sym, 0.0)
                if ref_price <= 0:
                    continue
                await self._maybe_rebalance_leg(
                    symbol=sym, target=targets[sym], current=current,
                    min_notional=meta.min_notional, ref_price=ref_price,
                )
        finally:
            timings["total"] = (time.monotonic() - iter_start) * 1000
            metrics.loop_duration.labels(step="total").observe(timings["total"] / 1000)
            metrics.operation_state.set(
                1.0 if self._hub.operation_state == OperationState.ACTIVE.value else 0.0
            )
            self._hub.last_iter_timings = timings
            self._hub.last_update = time.time()

    async def _safe_get_collateral(self) -> float | None:
        try:
            return await self._exchange.get_collateral()
        except Exception:
            return None

    async def _safe_get_position(self, symbol: str | None = None):
        sym = symbol if symbol is not None else self._settings.dydx_symbol
        try:
            return await self._exchange.get_position(sym)
        except Exception:
            return None

    async def _maybe_rebalance_leg(
        self, *, symbol: str, target: float, current: float,
        min_notional: float, ref_price: float,
    ) -> None:
        """Level-triggered taker: fire market order when |drift| * ref_price >= min_notional.

        target: desired short size in token base units (e.g. 100.0 ARB).
        current: current absolute short size in same units.
        min_notional: exchange minimum order notional in USD.
        ref_price: USD price of the leg's token (used both as the filter
          threshold and to compute the cross-spread price for the market order).

        Cross-spread convention for taker:
          side=sell -> price = ref_price * 0.999 (cross the bid)
          side=buy  -> price = ref_price * 1.001 (cross the ask)
        """
        drift = target - current
        notional_drift_usd = abs(drift) * ref_price
        if notional_drift_usd < min_notional:
            return  # sub-level, idle

        side = "sell" if drift > 0 else "buy"
        size = abs(drift)
        cross_price = ref_price * (0.999 if side == "sell" else 1.001)
        cloid = self._next_cloid_for_leg(symbol)
        metrics.aggressive_corrections_total.inc()
        try:
            await self._exchange.place_long_term_order(
                symbol=symbol, side=side, size=size, price=cross_price,
                cloid_int=cloid, ttl_seconds=60,
            )
            op_id = self._hub.current_operation_id
            if op_id is not None:
                slippage_usd = 0.0005 * size * ref_price
                field = (
                    "perp_fees_paid_token0"
                    if symbol == self._settings.dydx_symbol_token0
                    else "perp_fees_paid_token1"
                )
                await self._db.add_to_operation_accumulator(op_id, field, slippage_usd)
            await self._db.insert_order_log(
                timestamp=time.time(), exchange=self._exchange.name,
                action="place", side=side, size=size, price=cross_price,
                reason=f"level_triggered_{symbol}",
                operation_id=self._hub.current_operation_id,
            )
            logger.info(
                f"Rebalance fire [{symbol}]: {side} {size:.6f} @ ~{cross_price:.4f}"
            )
        except Exception as e:
            logger.exception(f"Rebalance fire failed [{symbol}]: {e}")

    def _next_cloid_for_leg(self, symbol: str) -> int:
        """Generate a cloid scoped per leg so concurrent fires from different
        legs never collide. Encodes a byte for the leg identity."""
        self._cloid_seq += 1
        leg_byte = 0xA0 if symbol == self._settings.dydx_symbol_token0 else 0xA1
        return (
            ((self._run_id & 0xFFFF) << 16) |
            (leg_byte << 8) |
            (self._cloid_seq & 0xFF)
        )

    async def _aggressive_correct(self, current_short, target_short, p_now, meta):
        """Deprecated thin wrapper: delegates to _maybe_rebalance_leg for the
        single-leg (token0) symbol. Task 10 refactors `_iterate` to call
        `_maybe_rebalance_leg` directly per leg; until then, keep this wrapper
        so the existing single-leg `_iterate` call site and its tests still
        work.
        """
        symbol = self._settings.dydx_symbol_token0
        min_notional = (
            meta.min_notional * p_now if meta is not None else 0.0
        )
        await self._maybe_rebalance_leg(
            symbol=symbol,
            target=target_short,
            current=current_short,
            min_notional=min_notional,
            ref_price=p_now,
        )

    async def _on_fill(self, fill):
        """Handle a fill event from the exchange WS, attribute to active operation."""
        op_id = self._hub.current_operation_id  # may be None

        fill_id = await self._db.insert_fill(
            timestamp=fill.timestamp, exchange=self._exchange.name,
            symbol=fill.symbol, side=fill.side, size=fill.size, price=fill.price,
            fee=fill.fee, fee_currency=fill.fee_currency, liquidity=fill.liquidity,
            realized_pnl=fill.realized_pnl, order_id=fill.order_id,
            operation_id=op_id,
        )

        if fill.order_id:
            try:
                await self._db.mark_grid_order_filled(fill.order_id, fill_id)
            except Exception:
                pass

        if fill.liquidity == "maker":
            self._hub.total_maker_fills += 1
            self._hub.total_maker_volume += fill.size
        else:
            self._hub.total_taker_fills += 1
            self._hub.total_taker_volume += fill.size
        self._hub.total_fees_paid += fill.fee
        sym = fill.symbol
        self._hub.hedge_realized_pnls[sym] = (
            self._hub.hedge_realized_pnls.get(sym, 0.0) + fill.realized_pnl
        )
        self._hub.last_update = time.time()

        metrics.fills_total.labels(liquidity=fill.liquidity, side=fill.side).inc()

        # Attribute fee to the active operation
        if op_id is not None and fill.fee > 0:
            await self._db.add_to_operation_accumulator(op_id, "perp_fees_paid", fill.fee)

        await self._db.insert_order_log(
            timestamp=time.time(), exchange=self._exchange.name,
            action="fill", side=fill.side, size=fill.size, price=fill.price,
            reason=fill.liquidity, operation_id=op_id,
        )

    async def _cancel_active_grid(self) -> None:
        """Cancel every order tracked as active in the DB.

        Used by both out-of-range branches (above p_b: pool is all token1, no
        grid; below p_a: pool is all token0, hold the short at the boundary
        and don't post new grid). The choice of "no grid" is the same in both
        directions, so a single helper covers both.
        """
        active = await self._db.get_active_grid_orders()
        if not active:
            return
        await self._exchange.batch_cancel([
            dict(symbol=self._settings.dydx_symbol, cloid_int=int(r["cloid"]))
            for r in active
        ])
        now = time.time()
        for r in active:
            await self._db.mark_grid_order_cancelled(r["cloid"], now)
