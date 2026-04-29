from __future__ import annotations
import asyncio
import time
import logging
from state import StateHub
from db import Database
from config import Settings
from math import sqrt
from chains.uniswap import UniswapV3PoolReader, tick_to_price
from chains.beefy import BeefyClmReader
from exchanges.dydx import DydxAdapter
from exchanges.base import ExchangeAdapter
from engine.curve import compute_l_from_value, compute_x, compute_target_grid, GridLevel
from engine.grid import GridManager
from engine.hedge import compute_hedge_action
from engine.operation import Operation, OperationState
from engine.reconciler import Reconciler
from engine.margin import compute_required_collateral, compute_margin_ratio, classify_margin
from web.alerts import post_alert
from web3 import AsyncWeb3, AsyncHTTPProvider

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
        decimals0: int = 18, decimals1: int = 6,
    ):
        self._settings = settings
        self._hub = hub
        self._db = db
        self._exchange = exchange
        self._pool_reader = pool_reader
        self._beefy_reader = beefy_reader
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

    def _ensure_reconciler(self):
        if self._reconciler is None and self._exchange is not None:
            self._reconciler = Reconciler(
                db=self._db, exchange=self._exchange, settings=self._settings,
            )
        return self._reconciler

    async def start_operation(self) -> int:
        """Begin a new operation: snapshot baseline, bootstrap short, mark ACTIVE."""
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
                self._hub.operation_state = OperationState.FAILED.value
                self._hub.current_operation_id = None
                raise

        await self._db.update_operation_status(op_id, OperationState.ACTIVE.value)
        self._hub.operation_state = OperationState.ACTIVE.value
        logger.info(f"Operation {op_id} started")
        return op_id

    async def stop_operation(self, *, close_reason: str = "user") -> dict:
        """Encerra a operação ativa: cancela grade, fecha short, grava final_pnl."""
        op_row = await self._db.get_active_operation()
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
        level = classify_margin(ratio)

        if level != "healthy" and level != self._last_alert_level:
            await post_alert(
                url=self._settings.alert_webhook_url,
                level=level,
                message=f"Margin ratio is {ratio:.2f} (collateral=${self._hub.dydx_collateral:.2f}, required=${required:.2f})",
                data={"ratio": ratio, "collateral": self._hub.dydx_collateral, "required": required},
            )
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
        while self._running:
            try:
                await self._iterate()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"Engine loop error: {e}")
            await asyncio.sleep(1.0)

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
        """One cycle of the main loop."""
        self._iter_count += 1
        # Periodic reconciliation (runs regardless of in-range/out-of-range path).
        await self._maybe_reconcile()
        # 1. Read on-chain state
        beefy_pos = await self._beefy_reader.read_position()
        p_now = await self._pool_reader.read_price()

        p_a = tick_to_price(beefy_pos.tick_lower, self._decimals0, self._decimals1)
        p_b = tick_to_price(beefy_pos.tick_upper, self._decimals0, self._decimals1)

        # User's portion of the pool
        my_amount0 = beefy_pos.amount0 * beefy_pos.share
        my_amount1 = beefy_pos.amount1 * beefy_pos.share
        my_value = my_amount0 * p_now + my_amount1
        if my_value <= 0:
            self._hub.last_update = time.time()
            return

        # Update range state before any out-of-range short-circuit so dashboard sees current bounds.
        self._hub.range_lower = p_a
        self._hub.range_upper = p_b
        self._hub.pool_value_usd = my_value
        self._hub.pool_tokens = {
            self._settings.pool_token0_symbol: my_amount0,
            self._settings.pool_token1_symbol: my_amount1,
        }

        # 2. Out-of-range handling (must precede compute_l_from_value, which
        # divides by (2*sqrt(p) - sqrt(p_a) - p/sqrt(p_b)) and only holds for
        # p strictly inside [p_a, p_b]). When out of range we still record the
        # liquidity L derived from the all-one-token formula so the dashboard
        # has a non-zero value to display.
        if p_now >= p_b:
            self._hub.out_of_range = True
            denom = sqrt(p_b) - sqrt(p_a)
            self._hub.liquidity_l = my_amount1 / denom if denom > 0 else 0.0
            await self._handle_out_of_range_upper()
            self._hub.last_update = time.time()
            return
        if p_now <= p_a:
            self._hub.out_of_range = True
            denom = (1.0 / sqrt(p_a)) - (1.0 / sqrt(p_b))
            L_oor = my_amount0 / denom if denom > 0 else 0.0
            self._hub.liquidity_l = L_oor
            # Refresh collateral so the margin check below sees current value.
            try:
                self._hub.dydx_collateral = await self._exchange.get_collateral()
            except Exception:
                pass
            # Peak short = max(LP-implied peak at p_a, current actual short).
            # Captures risk whether driven by LP needs or already-open position.
            peak_lp = compute_x(L_oor, p_a, p_b) * self._hub.hedge_ratio
            try:
                pos_now = await self._exchange.get_position(self._settings.dydx_symbol)
                cur_short = abs(pos_now.size) if pos_now else 0.0
            except Exception:
                cur_short = 0.0
            peak_short = max(peak_lp, cur_short)
            await self._check_margin_and_alert(peak_short, p_now)
            await self._handle_out_of_range_lower(p_a, p_b, L_oor)
            self._hub.last_update = time.time()
            return

        self._hub.out_of_range = False
        L_user = compute_l_from_value(my_value, p_a, p_b, p_now)
        self._hub.liquidity_l = L_user

        # 2.5. If no active operation, stop here — read state but skip grid
        if self._hub.operation_state != "active":
            self._hub.last_update = time.time()
            return

        # Refresh collateral so margin check sees current exchange value.
        try:
            self._hub.dydx_collateral = await self._exchange.get_collateral()
        except Exception:
            pass
        # Peak short = max(LP-implied peak at p_a, current actual short).
        # Captures risk whether driven by LP needs or already-open position.
        peak_lp = compute_x(L_user, p_a, p_b) * self._hub.hedge_ratio
        try:
            pos_pre = await self._exchange.get_position(self._settings.dydx_symbol)
            cur_short = abs(pos_pre.size) if pos_pre else 0.0
        except Exception:
            cur_short = 0.0
        peak_short = max(peak_lp, cur_short)
        await self._check_margin_and_alert(peak_short, p_now)

        # 3. Compute target grid
        meta = await self._exchange.get_market_meta(self._settings.dydx_symbol)
        target = compute_target_grid(
            L=L_user, p_a=p_a, p_b=p_b, p_now=p_now,
            hedge_ratio=self._hub.hedge_ratio,
            min_notional_usd=meta.min_notional * p_now,
            max_orders=self._settings.max_open_orders,
        )

        # 4. Reconcile current short with target
        target_short_at_now = compute_x(L_user, p_now, p_b) * self._hub.hedge_ratio
        pos = await self._exchange.get_position(self._settings.dydx_symbol)
        current_short = pos.size if pos else 0.0
        if pos:
            self._hub.hedge_position = {
                "side": pos.side, "size": pos.size, "entry": pos.entry_price,
            }
            self._hub.hedge_unrealized_pnl = pos.unrealized_pnl

        # Exposure check
        token0_pool = my_amount0
        if token0_pool > 0:
            exposure_pct = abs(current_short - target_short_at_now) / token0_pool
        else:
            exposure_pct = 0.0

        if exposure_pct > self._settings.threshold_aggressive:
            await self._aggressive_correct(current_short, target_short_at_now, p_now, meta)
            self._hub.last_update = time.time()
            return

        # 5. Diff and place/cancel
        active = await self._db.get_active_grid_orders()
        # Convert DB rows back to GridLevel approximations for diff
        current_levels = []
        for row in active:
            current_levels.append((row["cloid"], GridLevel(
                price=row["target_price"], size=row["size"],
                side=row["side"], target_short=0,  # not used in diff
            )))

        diff = self._grid_mgr.diff(current=current_levels, target=target)

        # Cancel
        if diff.to_cancel:
            await self._exchange.batch_cancel([
                dict(symbol=self._settings.dydx_symbol, cloid_int=int(c))
                for c in diff.to_cancel
            ])
            for cloid in diff.to_cancel:
                await self._db.mark_grid_order_cancelled(cloid, time.time())

        # Place
        if diff.to_place:
            specs = []
            for idx, lv in enumerate(diff.to_place):
                cloid_int = self._next_cloid(idx)
                specs.append(dict(
                    symbol=self._settings.dydx_symbol,
                    side=lv.side, size=lv.size, price=round(lv.price, 4),
                    cloid_int=cloid_int,
                ))
            placed = await self._exchange.batch_place(specs)
            for spec, p in zip(specs, placed):
                if p.status == "open":
                    await self._db.insert_grid_order(
                        cloid=str(spec["cloid_int"]),
                        side=spec["side"], target_price=spec["price"],
                        size=spec["size"], placed_at=time.time(),
                        operation_id=self._hub.current_operation_id,
                    )

        # 6. Update margin/collateral
        try:
            self._hub.dydx_collateral = await self._exchange.get_collateral()
        except Exception:
            pass

        self._hub.last_update = time.time()

    async def _aggressive_correct(self, current_short, target_short, p_now, meta):
        """Use taker orders to correct exposure quickly."""
        delta = target_short - current_short
        side = "sell" if delta > 0 else "buy"
        size = abs(delta)
        price = p_now * (1.001 if side == "sell" else 0.999)  # cross spread
        cloid = self._next_cloid(999)
        try:
            await self._exchange.place_long_term_order(
                symbol=self._settings.dydx_symbol,
                side=side, size=size, price=price,
                cloid_int=cloid, ttl_seconds=60,
            )
            await self._db.insert_order_log(
                timestamp=time.time(), exchange=self._exchange.name,
                action="place", side=side, size=size, price=price,
                reason="aggressive_correction",
                operation_id=self._hub.current_operation_id,
            )
            logger.warning(f"Aggressive correction: {side} {size} @ {price}")
        except Exception as e:
            logger.exception(f"Aggressive order failed: {e}")

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
        self._hub.hedge_realized_pnl += fill.realized_pnl
        self._hub.last_update = time.time()

        # Attribute fee to the active operation
        if op_id is not None and fill.fee > 0:
            await self._db.add_to_operation_accumulator(op_id, "perp_fees_paid", fill.fee)

        await self._db.insert_order_log(
            timestamp=time.time(), exchange=self._exchange.name,
            action="fill", side=fill.side, size=fill.size, price=fill.price,
            reason=fill.liquidity, operation_id=op_id,
        )

    async def _handle_out_of_range_upper(self):
        """Price > p_b: pool is 100% USDC, target short = 0. Cancel grid."""
        active = await self._db.get_active_grid_orders()
        if active:
            await self._exchange.batch_cancel([
                dict(symbol=self._settings.dydx_symbol, cloid_int=int(r["cloid"]))
                for r in active
            ])
            for r in active:
                await self._db.mark_grid_order_cancelled(r["cloid"], time.time())

    async def _handle_out_of_range_lower(self, p_a, p_b, L):
        """Price < p_a: pool is 100% WETH. Hold short at boundary x(p_a)."""
        active = await self._db.get_active_grid_orders()
        if active:
            await self._exchange.batch_cancel([
                dict(symbol=self._settings.dydx_symbol, cloid_int=int(r["cloid"]))
                for r in active
            ])
            for r in active:
                await self._db.mark_grid_order_cancelled(r["cloid"], time.time())


# Keep old Engine as alias for backwards compat (will be removed in cleanup task)
Engine = GridMakerEngine
