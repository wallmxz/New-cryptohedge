from __future__ import annotations
import asyncio
import time
import logging
from typing import TYPE_CHECKING
from state import StateHub
from db import Database
from config import Settings
from chains.uniswap import UniswapV3PoolReader, tick_to_price
from chains.beefy import BeefyClmReader
from exchanges.base import ExchangeAdapter
from engine.curve import compute_l_from_value, compute_x, compute_y, compute_target_grid
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
        self._task: asyncio.Task | None = None
        self._grid_task: asyncio.Task | None = None
        self._running = False
        self._cloid_seq = 0
        self._run_id = int(time.time())  # unique per process run
        self._reconciler: Reconciler | None = None
        self._iter_count = 0
        self.RECONCILE_EVERY_N_ITERATIONS = 30  # ~30s
        self._last_alert_level: str | None = None
        # Cache of (vault_id, readers, pair_settings). Rebuilt when DB's
        # selected_vault_id changes. Lets the main loop read the SAME
        # pool/vault that start_operation would target, instead of the
        # placeholder addresses from .env.
        self._vault_readers_cache: tuple[
            str, UniswapV3PoolReader, BeefyClmReader, "Settings", int, int,
        ] | None = None
        # Per-leg market IDs (resolved post-init via
        # resolve_market_ids_for_funding(); may be None until that
        # awaitable completes — handler tolerates).
        self._token0_mid: int | None = None
        self._token1_mid: int | None = None
        # Funding payments already attributed to the current op (dedup
        # against the poller emitting the same entry on consecutive
        # iterations). Cleared on op transitions.
        self._seen_funding_ids: set[int] = set()
        self._seen_funding_ids_op_id: int | None = None
        # Funding accumulator: adapter calls our handler per payment.
        # Default no-op on adapters that don't implement it.
        if self._exchange is not None:
            self._exchange.subscribe_funding(self._on_funding_payment)
        # Predictive hedge model (spec 2026-05-10). Cache populated on
        # first iter via _hedge_model.refresh_cache(); engine compares
        # predicted vs Beefy actual each iter and uses ACTUAL as the
        # authoritative target. _hedge_model is None when no
        # pool_reader is available (e.g. test/no-vault state); engine
        # falls back to Beefy direct in that case.
        self._hedge_model = None  # type: "HedgeModel | None"
        # Strong reference to the in-flight refresh_cache() task.
        # Python 3.13 may garbage-collect tasks that aren't referenced
        # anywhere; we overwrite each iter (prior task either completed
        # or is still running, which is fine — refresh_cache is
        # idempotent and short-lived).
        self._refresh_task: asyncio.Task | None = None
        # Event-driven grid state (spec 2026-05-15-event-driven-grid-design).
        # _last_known_position: Position | None — last seen, compared against
        #   pos_now in _grid_event_loop to detect fills.
        # _local_grid: dict[cloid, GridStop] — snapshot of stops we posted.
        # _last_safety_reconcile_at: timestamp of last full audit (90s cadence).
        self._last_known_position: "Position | None" = None
        self._local_grid: dict[int, "GridStop"] = {}
        self._last_safety_reconcile_at: float = 0.0

    def _ensure_reconciler(self):
        if self._reconciler is None and self._exchange is not None:
            self._reconciler = Reconciler(
                db=self._db, exchange=self._exchange, settings=self._settings,
            )
        return self._reconciler

    async def compute_curve_preview(self) -> dict:
        """Read the current vault's V3 range + grid that the engine would
        post against it. Used by the dashboard to render the LP curve
        visually so the user can see where buy/sell orders would trigger
        BEFORE starting an operation.

        Returns a dict with:
          - pool: { p_now, p_a, p_b, in_range, token0_symbol, token1_symbol }
          - position: { my_amount0, my_amount1, pool_value_usd, share }
          - grid: [{ price, side, size, target_short }, ...]
          - source: "active" if reading our actual LP position, "preview"
            if simulating against the wallet's total USD value.
          - min_notional_usd: minimum size of each grid order.

        Routes via the same vault as preview/start so what we render is
        what we'd actually post.
        """
        await self._refresh_vault_readers()  # ensure correct readers
        # Refuse early if pre-pair-pick (placeholder vault from .env).
        vault_addr = str(self._settings.clm_vault_address or "")
        if len(vault_addr) < 30:
            return {"error": "no_pair_selected"}

        beefy_pos = await self._beefy_reader.read_position()
        p_now = await self._pool_reader.read_price()
        p_a = tick_to_price(beefy_pos.tick_lower, self._decimals0, self._decimals1)
        p_b = tick_to_price(beefy_pos.tick_upper, self._decimals0, self._decimals1)

        my_amount0 = beefy_pos.amount0 * beefy_pos.share
        my_amount1 = beefy_pos.amount1 * beefy_pos.share
        my_value_t1 = my_amount0 * p_now + my_amount1
        is_dual_leg = bool(self._settings.dydx_symbol_token1)

        symbols = [self._settings.dydx_symbol_token0]
        if is_dual_leg:
            symbols.append(self._settings.dydx_symbol_token1)
        oracle = {}
        try:
            oracle = await self._exchange.get_oracle_prices(symbols)
        except Exception:
            pass
        p0_usd = float(oracle.get(symbols[0], p_now) or p_now)
        p1_usd = (
            float(oracle.get(symbols[1], 1.0) or 1.0)
            if is_dual_leg else 1.0
        )
        if is_dual_leg:
            pool_value_usd = my_amount0 * p0_usd + my_amount1 * p1_usd
        else:
            pool_value_usd = my_value_t1

        # If no LP position yet, simulate the curve against the wallet's
        # total USD value — gives the user a feel of where orders would go.
        source = "active" if my_value_t1 > 0 else "preview"
        if my_value_t1 <= 0:
            try:
                ws = await self._lifecycle.wallet_summary() if self._lifecycle else None
                if ws is None and self._pair_factory_w3 is not None:
                    sel = await self._db.get_selected_vault_id()
                    if sel:
                        from engine.pair_factory import build_lifecycle
                        lc = await build_lifecycle(
                            settings=self._settings, hub=self._hub, db=self._db,
                            exchange=self._exchange, selected_vault_id=sel,
                            w3=self._pair_factory_w3,
                            account=self._pair_factory_account,
                        )
                        ws = await lc.wallet_summary()
                if ws and ws.get("total_usd", 0) > 0:
                    # Convert wallet total USD into "value in token1 units"
                    # — that's what compute_l_from_value expects.
                    if is_dual_leg and p1_usd > 0:
                        my_value_t1 = ws["total_usd"] / p1_usd
                    else:
                        my_value_t1 = ws["total_usd"]
            except Exception as e:
                logger.warning(f"compute_curve_preview wallet sim failed: {e}")

        # No grid possible if out of range or no value.
        in_range = p_a < p_now < p_b
        grid_payload = []
        curve_samples: list[dict] = []
        L_value = 0.0
        x_now = 0.0
        if in_range and my_value_t1 > 0:
            try:
                L = compute_l_from_value(my_value_t1, p_a, p_b, p_now)
                L_value = L
                x_now = compute_x(L, p_now, p_b)
                # Sample the V3 curve uniformly across [p_a, p_b]. The y-axis
                # is exposure in token0 (`x(p) = L*(1/√p − 1/√p_b)`),
                # which is what the bot has to hedge with shorts. Curve
                # decreases monotonically: at p_a the LP is 100% token0
                # (max hedge needed), at p_b it's 100% token1 (zero hedge).
                n_samples = 80
                for i in range(n_samples + 1):
                    p = p_a + (p_b - p_a) * (i / n_samples)
                    # Avoid singularity at the edges
                    if p <= p_a:
                        p = p_a + (p_b - p_a) * 1e-6
                    if p >= p_b:
                        p = p_b - (p_b - p_a) * 1e-6
                    curve_samples.append({
                        "price": p,
                        "x_token0": compute_x(L, p, p_b),
                        "y_token1": compute_y(L, p, p_a),
                    })

                min_notional = 3.0
                max_orders = self._settings.max_open_orders or 200
                levels = compute_target_grid(
                    L=L, p_a=p_a, p_b=p_b, p_now=p_now,
                    hedge_ratio=self._hub.hedge_ratio or 1.0,
                    min_notional_usd=min_notional, max_orders=max_orders,
                )
                # Anchor each grid order on the curve (price, x_token0).
                # `target_short` is the cumulative short the bot SHOULD
                # hold once price reaches that level — matches `x(p)`.
                grid_payload = [
                    {
                        "price": lv.price,
                        "side": lv.side,
                        "size": lv.size,                # base units per order
                        "target_short": lv.target_short, # cumulative short
                        "x_token0": compute_x(L, lv.price, p_b),  # for plotting
                    }
                    for lv in levels
                ]
            except Exception as e:
                logger.warning(f"compute_curve_preview grid build failed: {e}")

        return {
            "source": source,
            "pool": {
                "p_now": p_now, "p_a": p_a, "p_b": p_b, "in_range": in_range,
                "token0_symbol": self._settings.pool_token0_symbol,
                "token1_symbol": self._settings.pool_token1_symbol,
                "address": self._settings.clm_pool_address,
                "fee_pct": (self._settings.uniswap_v3_pool_fee or 0) / 1_000_000.0,
            },
            "position": {
                "my_amount0": my_amount0, "my_amount1": my_amount1,
                "pool_value_usd": pool_value_usd,
                "share": beefy_pos.share,
                "L": L_value,
                "x_token0_now": x_now,  # current LP token0 exposure
            },
            "curve_samples": curve_samples,
            "grid": grid_payload,
            "is_dual_leg": is_dual_leg,
            "hedge_ratio": self._hub.hedge_ratio or 1.0,
            "min_notional_usd": 3.0,
        }

    async def open_shorts_for_existing_position(self) -> dict:
        """Open the hedge shorts against the existing Beefy position
        without going through bootstrap. Routes via selected vault.
        """
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
            return await lifecycle.open_shorts_for_existing_position()
        if self._lifecycle is not None:
            return await self._lifecycle.open_shorts_for_existing_position()
        raise RuntimeError("No lifecycle configured.")

    async def withdraw_partial(
        self, *, usd_amount: float | None = None,
        fraction: float | None = None,
    ) -> dict:
        """Withdraw a slice of the Beefy position. Pass EITHER usd_amount
        (oracle-priced) OR fraction (0..1).
        """
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
            return await lifecycle.withdraw_partial(
                usd_amount=usd_amount, fraction=fraction,
            )
        if self._lifecycle is not None:
            return await self._lifecycle.withdraw_partial(
                usd_amount=usd_amount, fraction=fraction,
            )
        raise RuntimeError("No lifecycle configured.")

    async def recover_partial_position(
        self, *, swap_to_usdc: bool = False,
    ) -> dict:
        """Emergency recovery: undo any partial state from a failed bootstrap.

        Mirrors the same lifecycle-routing as preview/start so we work
        against the SAME vault the user is configured for.

        `swap_to_usdc=False` (default) — only withdraws Beefy shares;
        keeps token0/token1 in the wallet so they can be reused.
        `swap_to_usdc=True` — also swaps residuals to native USDC.
        """
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
            return await lifecycle.recover_partial_position(swap_to_usdc=swap_to_usdc)

        if self._lifecycle is not None:
            return await self._lifecycle.recover_partial_position(swap_to_usdc=swap_to_usdc)

        raise RuntimeError("No lifecycle configured.")

    async def wallet_summary(self) -> dict | None:
        """Return total wallet value priced in USD via oracle.

        Mirrors `preview_operation`'s lifecycle-routing so the summary uses
        the same vault/pool addresses that the actual operation would use.
        Returns None if no lifecycle is reachable (no pair selected and no
        singleton lifecycle).
        """
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
            return await lifecycle.wallet_summary()

        if self._lifecycle is not None:
            return await self._lifecycle.wallet_summary()

        return None

    async def preview_operation(self, *, usdc_budget: float) -> dict:
        """Compute the swap+deposit+hedge plan WITHOUT touching the chain.

        Mirrors `start_operation`'s lifecycle-selection logic so the preview
        uses the SAME addresses the actual operation would use:
          - Pair-picker path: rebuild lifecycle via pair_factory for the
            currently-selected vault, then preview against that.
          - Singleton lifecycle path: preview against `self._lifecycle`.
          - Legacy: no preview available (returns error).
        """
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
            return await lifecycle.bootstrap_preview(usdc_budget=usdc_budget)

        if self._lifecycle is not None:
            return await self._lifecycle.bootstrap_preview(usdc_budget=usdc_budget)

        raise RuntimeError(
            "No lifecycle configured and no pair selected. "
            "Select a pair via UI before previewing."
        )

    async def start_operation(
        self, *, usdc_budget: float | None = None,
        swap_strategies: dict | None = None,
    ) -> int:
        """Begin a new operation. Routes via pair_factory > singleton lifecycle > legacy.

        When pair_factory is configured AND a vault is selected in DB, build a
        fresh lifecycle for that vault and bootstrap. When the singleton
        lifecycle is configured (Phase 2.0), use it. Otherwise fall back to the
        legacy Phase 1.2 snapshot-only path.

        usdc_budget is REQUIRED when either lifecycle path is used. Missing
        budget raises RuntimeError to prevent silent fallback.

        swap_strategies (optional, cross-pair only): per-leg user choice from
        the preview UI. Shape `{"token0": "use_existing"|"full_swap"|"swap_diff",
        "token1": "..."}`. Forwarded to `lifecycle.bootstrap()`.
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
            return await lifecycle.bootstrap(
                usdc_budget=usdc_budget, swap_strategies=swap_strategies,
            )

        # Phase 2.0 path: singleton lifecycle if configured
        if self._lifecycle is not None:
            if usdc_budget is None:
                raise RuntimeError(
                    "usdc_budget required when lifecycle is configured. "
                    "Pass {usdc_budget: <float>} in request body."
                )
            return await self._lifecycle.bootstrap(
                usdc_budget=usdc_budget, swap_strategies=swap_strategies,
            )

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
        # Skip reconciler under predictive_grid_v2 — the trailing grid
        # handler (`_on_grid_fill`) + drift correction (`_maybe_correct_drift`)
        # own the order-state sync for v2 ops. The legacy Reconciler was
        # designed for dYdX limit-order maker grids (Phase 1.1) and:
        #   (a) marks live SL_MARKET stops as "lost" in DB if its
        #       authenticated active-orders query returns stale,
        #   (b) then on the NEXT cycle, sees those same orders live on
        #       Lighter but no longer in DB → considers them ORPHANS and
        #       cancels them on the exchange → DESTROYS the entire grade.
        # Validated live 2026-05-14 op #29 smoke (cancelled_orphan logs
        # taking 5+ stops off Lighter, drift_correction firing to recover).
        if getattr(self._settings, "predictive_grid_v2", False):
            return
        if self._iter_count % self.RECONCILE_EVERY_N_ITERATIONS == 0:
            rec = self._ensure_reconciler()
            if rec is not None:
                try:
                    await rec.reconcile()
                except Exception as e:
                    logger.error(f"Reconciler error: {e}")

    async def start(self):
        if self._exchange is None:
            # Lazy import — production deploys with ACTIVE_EXCHANGE=lighter
            # don't need the dydx-v4-client SDK loaded.
            from exchanges.dydx import DydxAdapter
            self._exchange = DydxAdapter(
                mnemonic=self._settings.dydx_mnemonic,
                wallet_address=self._settings.dydx_address,
                network=self._settings.dydx_network,
                subaccount=self._settings.dydx_subaccount,
            )
        # Exchange connection is best-effort: when the venue's edge (e.g.
        # CloudFront WAF blocking the IP, regional captcha challenge) is
        # rejecting requests, the rest of the system (chain reads, recovery
        # endpoint, dashboard) should still come up. We flag it as
        # disconnected and let the user see "Offline (exchange)" instead
        # of having uvicorn crash on startup.
        try:
            await self._exchange.connect()
            self._hub.connected_exchange = True
        except Exception as e:
            logger.error(
                f"Exchange connect failed at startup ({e!r}); engine will run "
                f"with exchange offline. Chain ops + recovery endpoint still work."
            )
            self._hub.connected_exchange = False

        if self._pool_reader is None or self._beefy_reader is None:
            w3 = AsyncWeb3(AsyncHTTPProvider(self._settings.arbitrum_rpc_url))
            self._pool_reader = UniswapV3PoolReader(
                w3, self._settings.clm_pool_address, self._decimals0, self._decimals1,
            )
            # Legacy fallback path (no pair selected via picker yet). The
            # CLM v2 reader needs both strategy + earn addresses; without
            # pair-picker metadata we only have the legacy single address.
            # This path is only exercised pre-pair-pick (typically with
            # placeholder addresses), so we pass the configured address as
            # both — first real read will fail with a clear error if the
            # address actually points at a deployed contract.
            self._beefy_reader = BeefyClmReader(
                w3, self._settings.clm_vault_address,
                self._settings.clm_vault_address,
                self._settings.wallet_address,
                self._decimals0, self._decimals1,
            )
            self._hub.connected_chain = True

        # Initial reconciliation on startup, BEFORE main loop begins.
        # Recovers from crashes: cancels orphan orders on exchange, marks
        # lost DB orders as cancelled.
        # NOTA: skip under predictive_grid_v2 (trailing handles state — see
        # _maybe_reconcile docstring for the destructive interaction).
        if not getattr(self._settings, "predictive_grid_v2", False):
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

        # subscribe_fills is also best-effort. Same rationale as connect():
        # if the exchange edge is unreachable, we shouldn't kill startup —
        # the user can still recover funds and inspect state.
        if self._hub.connected_exchange:
            try:
                await self._exchange.subscribe_fills(
                    self._settings.dydx_symbol, self._on_fill,
                )
            except Exception as e:
                logger.error(
                    f"subscribe_fills failed ({e!r}); fills won't stream "
                    f"until reconnect."
                )

        self._running = True
        self._task = asyncio.create_task(self._main_loop())
        self._grid_task = asyncio.create_task(self._grid_event_loop())
        logger.info("GridMakerEngine started")

    async def stop(self):
        self._running = False
        for t in (self._task, self._grid_task):
            if t is not None and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._grid_task = None
        if self._exchange:
            await self._exchange.disconnect()

    async def _main_loop(self):
        # Hold a stable 1Hz cadence: subtract the iteration's elapsed time so
        # a slow iter doesn't compound into a 2Hz/0.5Hz drift.
        period = 1.0
        # Suppress repeated exception logs while idling against a placeholder
        # vault — common during fresh setup before any pair is selected.
        idle_log_suppressed = False
        while self._running:
            iter_start = time.monotonic()
            try:
                await self._iterate()
                idle_log_suppressed = False
            except asyncio.CancelledError:
                break
            except Exception as e:
                # When idle (no operation active), the chain reads on the
                # configured (placeholder) vault address are expected to fail.
                # Log once at WARN, then suppress until either an operation
                # starts or a successful iter resets the flag.
                if self._hub.operation_state == OperationState.NONE.value:
                    if not idle_log_suppressed:
                        logger.warning(
                            f"Idle iter failed (likely placeholder vault — "
                            f"select a pair via UI to start). Suppressing "
                            f"further idle errors. err={e!r}"
                        )
                        idle_log_suppressed = True
                else:
                    logger.exception(f"Engine loop error: {e}")
            elapsed = time.monotonic() - iter_start
            await asyncio.sleep(max(0.0, period - elapsed))

    def _next_cloid(self, level_idx: int) -> int:
        """Generate unique cloid as int (dYdX/Lighter accept int64).

        Layout (64 bits): run_id (32) | level_idx (8) | seq (24).
        24-bit seq = 16M unique cloids per (run, level) — effectively
        unlimited within a single bot run. Pre-2026-05-15 used 8-bit seq,
        which wrapped at 256 and collided with prior `grid_orders` rows
        (UNIQUE constraint), causing infinite reconciler retry loops.
        """
        self._cloid_seq += 1
        return (
            ((self._run_id & 0xFFFFFFFF) << 32) |
            ((level_idx & 0xFF) << 24) |
            (self._cloid_seq & 0xFFFFFF)
        )

    async def _refresh_vault_readers(self) -> None:
        """If a pair was picked via the UI, swap pool/beefy readers and
        per-pair settings to point at that vault — instead of using the
        .env placeholder addresses. Cached against selected_vault_id so we
        only rebuild when it actually changes (no per-iteration cost).

        No-op when:
          - pair_factory not configured (legacy single-vault deployments)
          - no vault selected yet (`/pairs/select` not called)
          - vault not in beefy_pairs_cache (refresh needed)
        """
        if self._pair_factory_w3 is None:
            return  # legacy single-vault path; nothing to do
        try:
            selected = await self._db.get_selected_vault_id()
        except Exception:
            return
        if not selected:
            return
        cache = self._vault_readers_cache
        if cache is not None and cache[0] == selected:
            return  # already cached for this vault
        from engine.pair_factory import build_readers_for_vault
        try:
            built = await build_readers_for_vault(
                settings=self._settings, db=self._db,
                w3=self._pair_factory_w3,
                selected_vault_id=selected,
            )
        except Exception as e:
            logger.warning(f"build_readers_for_vault failed: {e}")
            return
        if built is None:
            return
        pair_settings, pool_reader, beefy_reader, dec0, dec1 = built
        self._settings = pair_settings
        self._pool_reader = pool_reader
        self._beefy_reader = beefy_reader
        self._decimals0 = dec0
        self._decimals1 = dec1
        self._vault_readers_cache = (
            selected, pool_reader, beefy_reader, pair_settings, dec0, dec1,
        )
        # The Reconciler instance was built with a reference to the OLD
        # `self._settings` (at engine construction time). After a pair
        # switch via picker, `self._settings` points to a new object but
        # the reconciler still holds the old one — and queries the WRONG
        # market (e.g., ETH-USD market_id=0 when active is ARB-USD=50),
        # which always returns empty active orders and false-cancels every
        # live stop in the DB. Resync after each rebuild.
        # Validated live 2026-05-14 op #29 smoke.
        if self._reconciler is not None:
            self._reconciler._settings = pair_settings
        # Register the active perp symbols with the exchange adapter so
        # its WS pump subscribes to ONLY those order books. Subscribing
        # to all 170+ Lighter markets at once trips the server's "Too
        # Many Inflight Messages" guard (code 30010) and the WS keeps
        # dropping. Adapter subscribes to whatever's registered when its
        # WS pump (re)connects.
        active_symbols = [pair_settings.dydx_symbol_token0]
        if pair_settings.dydx_symbol_token1:
            active_symbols.append(pair_settings.dydx_symbol_token1)
        register = getattr(self._exchange, "register_active_symbols", None)
        if callable(register):
            try:
                register(active_symbols)
            except Exception as e:
                logger.warning(f"register_active_symbols failed: {e}")
        # Re-resolve funding handler mids — `self._settings` was just
        # replaced with the per-pair settings (which carries the right
        # `dydx_symbol_token1` for cross-pair). The startup-time call
        # in app.py only saw the global .env settings where token1 is
        # empty, so without this re-call `_token1_mid` stays None and
        # ARB funding entries silently skip in `_on_funding_payment`.
        try:
            await self.resolve_market_ids_for_funding()
        except Exception as e:
            logger.warning(
                f"resolve_market_ids_for_funding (post-rebuild) failed: {e}"
            )
        # Build / rebuild HedgeModel whenever vault readers change. The
        # V3PositionReader needs the pool address (from settings) and
        # the Beefy strategy address (resolved from the vault).
        from chains.v3_position import V3PositionReader
        from engine.hedge_model import HedgeModel
        try:
            strategy_addr = await self._beefy_reader._earn.functions.strategy().call()
            v3_reader = V3PositionReader(
                w3=self._beefy_reader._w3,
                pool_address=str(self._settings.clm_pool_address),
                beefy_strategy_address=strategy_addr,
            )
            self._hedge_model = HedgeModel(v3_reader)
        except Exception as e:
            logger.warning(f"HedgeModel build failed: {e}; engine will fall back to Beefy actual")
            self._hedge_model = None
        logger.info(
            f"Engine readers rebuilt for vault {selected} "
            f"({pair_settings.pool_token0_symbol}/{pair_settings.pool_token1_symbol})"
        )

    async def _iterate(self):
        iter_start = time.monotonic()
        self._iter_count += 1
        timings: dict[str, float] = {}
        try:
            # If a pair was picked via UI, swap our readers to point at the
            # actual vault/pool — not the .env placeholder addresses.
            # Cached: only rebuilds when selected_vault_id changes.
            await self._refresh_vault_readers()

            # Idle guard: no real vault means we're pre-pair-selection.
            # Skip chain reads to avoid log spam. tests pass `settings =
            # MagicMock()` so `clm_vault_address` may be a MagicMock that
            # raises TypeError on `len()` — coerce via `str()` first.
            if self._hub.operation_state == OperationState.NONE.value:
                vault_addr = str(self._settings.clm_vault_address or "")
                if len(vault_addr) < 30:
                    return

            await self._maybe_reconcile()

            t = time.monotonic()
            try:
                beefy_pos, p_now = await asyncio.wait_for(
                    asyncio.gather(
                        self._beefy_reader.read_position(),
                        self._pool_reader.read_price(),
                    ),
                    timeout=5.0,
                )
            except asyncio.TimeoutError as e:
                logger.warning(
                    "_iterate: chain RPC gather timeout, skipping iter: %s", e,
                )
                return
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

            # No idle throttle — the LighterAdapter migrated to WebSocket
            # subscriptions, so positions/oracle/collateral are read from
            # in-memory cache (zero HTTP per call). The throttle was a
            # mitigation for the CloudFront WAF that kicked in under
            # sustained 1Hz HTTP polling; since we no longer poll over
            # HTTP, the throttle just made dashboard state stale (token
            # USD prices stuck at 0, etc.) without buying anything.
            is_active = (
                self._hub.operation_state == OperationState.ACTIVE.value
            )

            # One round-trip each: positions per symbol, oracle prices, collateral
            positions, oracle_prices, collateral = await asyncio.gather(
                asyncio.gather(*[self._safe_get_position(s) for s in symbols]),
                self._exchange.get_oracle_prices(symbols),
                self._safe_get_collateral(),
            )
            if collateral is not None:
                self._hub.dydx_collateral = collateral

            # Publish live token USD prices so the dashboard can format
            # wallet residuals, curve previews, etc. without hardcoded
            # multipliers (the previous UI assumed ETH=$3000 forever).
            if oracle_prices:
                p0 = oracle_prices.get(symbols[0])
                if p0 and p0 > 0:
                    self._hub.token0_usd_price = float(p0)
                if is_dual_leg:
                    p1 = oracle_prices.get(symbols[1])
                    if p1 and p1 > 0:
                        self._hub.token1_usd_price = float(p1)

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

            # Compute USD pool value.
            # Cross-pair fallback caveat: `p_now` is the pool ratio (token1
            # per token0), NOT a USD price. Using it as a fallback for
            # `p0_usd` in dual-leg quietly corrupts pool_value_usd when
            # the oracle is offline (a CloudFront-style block had us
            # multiplying token0 amount by the WETH/ARB ratio earlier in
            # this session, inflating reported value by ~ETH-USD price).
            # Skip the USD computation rather than emit a garbage number.
            if is_dual_leg:
                p0_usd = oracle_prices.get(symbols[0], 0.0) or 0.0
                p1_usd = oracle_prices.get(symbols[1], 0.0) or 0.0
                if p0_usd > 0 and p1_usd > 0:
                    pool_value_usd = my_amount0 * p0_usd + my_amount1 * p1_usd
                else:
                    # Don't pollute hub with a wrong number — leave last
                    # known value in place. Oracle is briefly offline.
                    logger.warning(
                        "Cross-pair oracle unavailable; skipping pool USD "
                        "value update this iter."
                    )
                    pool_value_usd = self._hub.pool_value_usd
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

                        # Authoritative venue-side hedge_pnl since op.started_at
                        # (or user-selected pnl_window_since_ts if set, see
                        # spec 2026-05-09). Overrides in-memory accumulator
                        # that resets on uvicorn restart. None = fall back.
                        hedge_pnl_override = None
                        try:
                            op_started_at = float(op_row.get("started_at") or 0)
                        except Exception:
                            op_started_at = 0.0
                        # User-selectable window: if set, use it instead of
                        # op.started_at. Lets user pick "since 14:00" rather
                        # than always since op start.
                        try:
                            window_since = op_row.get("pnl_window_since_ts")
                            if window_since is not None and float(window_since) > 0:
                                op_started_at = float(window_since)
                        except Exception:
                            pass
                        if op_started_at > 0:
                            try:
                                getter = getattr(
                                    self._exchange, "get_trade_pnl_since", None,
                                )
                                if getter is not None:
                                    r = await getter(op_started_at, time.time())
                                    if r is not None:
                                        baseline, latest = r
                                        hedge_pnl_override = latest - baseline
                            except Exception as e:
                                logger.warning(
                                    f"get_trade_pnl_since failed: {e}"
                                )

                        # Pull current unrealized PnL so we can decompose
                        # hedge_pnl_override into (realized, unrealized).
                        # Spec 2026-05-14: user wants to see realized+
                        # unrealized SEPARATELY (matches Lighter UI's
                        # "Unrealized PnL" while keeping trade_pnl
                        # cumulative for "closed" portion).
                        hedge_unrealized_override = None
                        try:
                            symbol_t0 = self._settings.dydx_symbol_token0
                            pos = await self._safe_get_position(symbol_t0)
                            if pos is not None:
                                hedge_unrealized_override = float(
                                    getattr(pos, "unrealized_pnl", 0.0) or 0.0
                                )
                        except Exception as e:
                            logger.warning(
                                f"get unrealized_pnl for breakdown failed: {e}"
                                )

                        # Funding window override: when the user picked
                        # a start in the UI, sum funding from Lighter
                        # since that ts instead of the DB cumulative
                        # (which is since op.started_at).
                        funding_override = None
                        try:
                            window_since = op_row.get("pnl_window_since_ts")
                        except Exception:
                            window_since = None
                        if window_since is not None and float(window_since) > 0:
                            try:
                                fgetter = getattr(
                                    self._exchange, "get_funding_total_since", None,
                                )
                                if fgetter is not None:
                                    funding_override = await asyncio.wait_for(
                                        fgetter(
                                            since_ts=float(window_since),
                                            market_id_token0=self._token0_mid,
                                            market_id_token1=self._token1_mid,
                                        ),
                                        timeout=5.0,
                                    )
                            except Exception as e:
                                logger.warning(
                                    f"get_funding_total_since failed: {e}"
                                )
                                funding_override = None

                        if is_dual_leg:
                            self._hub.operation_pnl_breakdown = compute_operation_pnl(
                                op,
                                current_pool_value_usd=pool_value_usd,
                                current_token0_usd_price=p0_usd,
                                current_token1_usd_price=p1_usd,
                                hedge_realized_per_symbol=dict(self._hub.hedge_realized_pnls),
                                hedge_unrealized_per_symbol=dict(self._hub.hedge_unrealized_pnls),
                                hedge_pnl_aggregate_override=hedge_pnl_override,
                                hedge_unrealized_override=hedge_unrealized_override,
                                funding_override=funding_override,
                            )
                        else:
                            self._hub.operation_pnl_breakdown = compute_operation_pnl(
                                op,
                                current_pool_value_usd=pool_value_usd,
                                current_eth_price=p_now,
                                hedge_realized_since_baseline=self._hub.hedge_realized_pnl,
                                hedge_unrealized_since_baseline=self._hub.hedge_unrealized_pnl,
                                hedge_pnl_aggregate_override=hedge_pnl_override,
                                hedge_unrealized_override=hedge_unrealized_override,
                                funding_override=funding_override,
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

            # Compute targets per leg.
            #
            # Use `my_amount0`/`my_amount1` directly — those are our share
            # of the strategy's real V3 position from `strategy.balances()`.
            #
            # `compute_x(L_user, p_now, p_b)` would be mathematically
            # equivalent IF and only if the strategy held all its
            # liquidity in a single range matching [p_a, p_b]. Beefy CLM
            # v2 typically holds a `positionMain` (the [p_a, p_b] range we
            # read) PLUS a `positionAlt` (single-sided fee-collection
            # range). `compute_l_from_value` flattens the alt liquidity
            # into the main range, inflating `L_user`. Driving the hedge
            # off `my_amount0`/`my_amount1` is the correct delta even
            # when an alt range is present.
            targets: dict[str, float] = {
                symbols[0]: my_amount0 * self._hub.hedge_ratio,
            }
            if is_dual_leg:
                targets[symbols[1]] = my_amount1 * self._hub.hedge_ratio

            # Predictive hedge model (spec 2026-05-10). Compute predicted
            # target via V3 formula with cached L_main + L_alt. Verify
            # vs Beefy actual; use ACTUAL as the authoritative target
            # (predicted is informational + drives status field).
            from engine.hedge_model import DIVERGENCE_THRESHOLD
            predicted = None
            if self._hedge_model is not None:
                # Trigger async refresh if cache stale or pending — does
                # NOT await, so iter is never blocked by RPC.
                if self._hedge_model.should_refresh():
                    # Hold strong ref so 3.13 GC doesn't drop a pending
                    # task. Each iter overwrites; prior task either
                    # completed or is still running (which is fine —
                    # refresh_cache is idempotent and short-lived).
                    self._refresh_task = asyncio.create_task(
                        self._hedge_model.refresh_cache()
                    )
                # Predict (returns None if cache cold)
                predicted = self._hedge_model.predict(
                    p_now,
                    decimals0=self._decimals0,
                    decimals1=self._decimals1,
                )

            # Verify (informational; sets _refresh_pending if diverging)
            if predicted is not None:
                actual_total = (beefy_pos.amount0, beefy_pos.amount1)
                div = self._hedge_model.verify(
                    predicted=predicted, actual=actual_total,
                )
                self._hub.hedge_model_status = (
                    "active" if div <= DIVERGENCE_THRESHOLD
                    else f"verify_diverging:{div * 100:.1f}%"
                )
            elif self._hedge_model is None:
                self._hub.hedge_model_status = "unavailable"
            else:
                self._hub.hedge_model_status = "warming_up"

            # Always fire per leg via _maybe_rebalance_leg (reactive path).
            # Target = actual × share × hedge_ratio (computed above into
            # `targets`). predicted is informational only (drives
            # hedge_model_status field).
            if self._settings.predictive_grid_v2:
                # Predictive grid v2: maintain stop-limit grid event-driven.
                # No taker chase; ordens ficam dormentes na Lighter até trigger.
                # Spec: docs/superpowers/specs/2026-05-12-predictive-grid-v2-design.md
                await self._maintain_grid(
                    beefy_pos=beefy_pos, p_now=p_now,
                    oracle_prices=oracle_prices,
                )
                # Drift correction (user spec 2026-05-14): mesmo com a grade
                # ativa, comparar nossa posição short atual com o predicted
                # pela V3. Se |diff_usd| > $1, dispara taker pra corrigir
                # (não mexe nas stops triggers, só ajusta o overall). Cobre
                # gaps causados por rebalances, fills perdidos, etc.
                await self._maybe_correct_drift(
                    beefy_pos=beefy_pos, p_now=p_now,
                    positions=positions, symbols=symbols, targets=targets,
                )
            else:
                # Legacy reactive taker chase (default até cutover Phase D).
                for sym in symbols:
                    idx = symbols.index(sym)
                    current = abs(positions[idx].size) if positions[idx] else 0.0
                    ref_price = oracle_prices.get(sym, 0.0)
                    if ref_price <= 0:
                        continue
                    await self._maybe_rebalance_leg(
                        symbol=sym, target=targets[sym], current=current,
                        min_notional=self._settings.min_rebalance_notional_usd,
                        ref_price=ref_price,
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

    async def resolve_market_ids_for_funding(self) -> None:
        """Resolve the market_index for token0 and token1 perp symbols.
        Called once from the app startup path after the adapter is
        connected (and metadata cached). Stored mids are used by
        _on_funding_payment to route per-leg DB writes.
        """
        try:
            t0 = self._settings.dydx_symbol_token0
            if t0:
                m0 = await self._exchange.get_market_meta(t0)
                self._token0_mid = int(m0.market_index)
        except Exception as e:
            logger.warning(f"resolve_market_ids_for_funding token0 failed: {e}")
        try:
            t1 = self._settings.dydx_symbol_token1
            if t1:
                m1 = await self._exchange.get_market_meta(t1)
                self._token1_mid = int(m1.market_index)
        except Exception as e:
            logger.warning(f"resolve_market_ids_for_funding token1 failed: {e}")

    async def _safe_get_position(self, symbol: str | None = None):
        """Returns the position the engine should drive drift against.

        On the LighterAdapter this returns `get_effective_position`,
        which fuses WS-observed state with locally-stamped expected
        state from recent fires — making the over-hedge race
        documented in 2026-05-07 structurally impossible. Adapters
        that don't implement `get_effective_position` (e.g. test
        mocks, alternative exchanges) fall back to `get_position`.
        """
        sym = symbol if symbol is not None else self._settings.dydx_symbol
        try:
            getter = getattr(
                self._exchange, "get_effective_position", None,
            )
            if getter is None:
                return await self._exchange.get_position(sym)
            return await getter(sym)
        except Exception:
            return None

    async def _maintain_grid(
        self, *, beefy_pos, p_now: float, oracle_prices: dict[str, float],
    ) -> None:
        """Mantém grade de stop-limit orders alinhada aos ticks ativos da Beefy.

        Implementa o lifecycle event-driven descrito no spec
        (docs/superpowers/specs/2026-05-12-predictive-grid-v2-design.md, Sec 6):
          - Detecta mudança de range (tick_lower/upper) e L_main do HedgeModel
            cache → cancel-all + rebuild
          - Detecta drift de composição → rebuild
          - Detecta preço fora do range → cancel-all + idle
          - Insere próximo nível quando ordem fillha (handler separado em
            _on_grid_fill, Task B5)

        No-op se feature flag PREDICTIVE_GRID_V2 desligada (default).
        Implementação completa em B4.
        """
        if not self._settings.predictive_grid_v2:
            return
        if self._hedge_model is None or self._hedge_model._cache is None:
            # Cache cold — sem L_main / ticks confiáveis. Skipa silenciosamente;
            # próxima iter (com refresh feito) pega.
            return

        from engine.curve import compute_grid_from_pool_ticks, tick_to_human_price
        from math import log, floor

        cache = self._hedge_model._cache

        # Out-of-range guard (user spec 2026-05-14): se o preço atual está
        # fora do range Beefy, "para de mexer" — cancela grade ativa e fica
        # idle. Próximo rebalance Beefy vai reposicionar e o engine reinicia
        # a grade quando o preço voltar pro range novo.
        decimals0 = self._settings.token0_decimals
        decimals1 = self._settings.token1_decimals
        p_a_human = tick_to_human_price(
            tick=cache.tick_lower_main, decimals0=decimals0, decimals1=decimals1,
        )
        p_b_human = tick_to_human_price(
            tick=cache.tick_upper_main, decimals0=decimals0, decimals1=decimals1,
        )
        if not (p_a_human <= p_now <= p_b_human):
            # Fora do range. Cancela grade ativa se existir e sai.
            if getattr(self, "_posted_grid_signature", None) is not None:
                try:
                    await self._exchange.cancel_all_stops(
                        symbol=self._settings.dydx_symbol_token0,
                    )
                except Exception as e:
                    logger.warning(f"out-of-range cancel_all_stops failed: {e}")
                self._posted_grid_signature = None
                metrics.grid_levels_active.set(0)
            return

        # Signature atual da geometria (L + ticks bounds). Detecta Beefy
        # rebalance: quando muda, cancela tudo + rebuild fresh.
        current_sig = (cache.L_main, cache.tick_lower_main, cache.tick_upper_main)
        posted_sig = getattr(self, "_posted_grid_signature", None)

        # Range change (Beefy rebalanceou) → cancel-all + DB cleanup, depois
        # cai pro reconcile que vai postar grade nova.
        if posted_sig is not None and posted_sig != current_sig:
            metrics.beefy_range_change_total.inc()
            try:
                await self._exchange.cancel_all_stops(
                    symbol=self._settings.dydx_symbol_token0,
                )
            except Exception as e:
                logger.warning(f"range_change cancel_all_stops failed: {e}")
            try:
                active = await self._db.get_active_grid_orders()
                for row in active:
                    await self._db.mark_grid_order_cancelled(
                        row["cloid"], time.time(),
                    )
                    metrics.grid_stops_cancelled_total.inc()
            except Exception as e:
                logger.warning(f"range_change db cleanup failed: {e}")
            self._posted_grid_signature = None  # força reconcile inicial

        # Sempre reconcilia (self-healing): computa desired 8+8 ao redor de
        # tick_now atual, compara com live na Lighter, posta missing,
        # cancela extras. Idempotente. Trailing emerge naturalmente quando
        # tick_now anda — diff cancela extremo distante + posta extremo novo.
        # Crucial: fills ASSÍNCRONOS dos SL_MARKETs não disparam fill
        # callback no SDK (só `place_long_term_order` faz inline confirm).
        # Reconcile cada iter recupera de fills perdidos sem depender de WS.
        # Validado bug live 2026-05-14: sells fillaram, position cresceu 31 ARB,
        # bot_grid_stops_filled_total = 0 (WS callback nunca chamado).
        rebuild_reason = "initial" if posted_sig is None else "reconcile"
        await self._reconcile_grid(
            beefy_pos=beefy_pos, p_now=p_now, cache=cache,
            rebuild_reason=rebuild_reason,
        )
        self._posted_grid_signature = current_sig

    async def _reconcile_grid(
        self, *, beefy_pos, p_now: float, cache, rebuild_reason: str,
    ) -> None:
        """Self-healing: compara desired 8+8 com live Lighter, posta missing,
        cancela extras. Trailing emerge naturalmente da mudança de tick_now.

        Spec design 2026-05-14: substituiu o fill-callback-driven trailing,
        que não funcionava pra SL_MARKETs (fills assíncronos não disparam
        callback no SDK). Idempotente, recupera de fills perdidos.
        """
        from math import log, floor
        from engine.curve import (
            compute_grid_from_pool_ticks, tick_to_human_price,
        )

        decimals0 = self._settings.token0_decimals
        decimals1 = self._settings.token1_decimals
        decimal_factor = 10 ** (decimals0 - decimals1)
        tick_now = floor(log(p_now / decimal_factor) / log(1.0001))

        fee_tier = self._settings.uniswap_v3_pool_fee
        tick_spacing_map = {500: 10, 3000: 60, 10000: 200}
        tick_spacing = tick_spacing_map.get(fee_tier, 10)

        hedge_ratio = (
            getattr(self._hub, "hedge_ratio", None)
            or self._settings.hedge_ratio
        )
        # cache.L_main é RAW V3 strategy-total. Para retornar tokens em
        # human units com prices em human units, escalar por
        # 1/10^((d0+d1)/2) e aplicar share. (See full comment in earlier
        # commit message for ARB-USDC.e math.)
        l_decimal_factor = 10 ** ((decimals0 + decimals1) / 2)
        share = float(getattr(beefy_pos, "share", 1.0) or 1.0)
        L_for_grid = float(cache.L_main) / l_decimal_factor * share

        full_grid = compute_grid_from_pool_ticks(
            L=L_for_grid,
            tick_lower=cache.tick_lower_main,
            tick_upper=cache.tick_upper_main,
            tick_spacing=tick_spacing,
            tick_now=tick_now,
            decimals0=decimals0,
            decimals1=decimals1,
            hedge_ratio=hedge_ratio,
            lighter_price_decimals=5,  # ARB-USD on Lighter (TODO: source from meta)
            lighter_size_decimals=1,
        )

        # 8+8 closest a tick_now (max_open_orders default 16)
        max_orders = int(getattr(self._settings, "max_open_orders", 16) or 16)
        per_side = max_orders // 2
        desired_sells = sorted(
            [lv for lv in full_grid if lv.side == "sell"],
            key=lambda lv: -lv.price,
        )[:per_side]
        desired_buys = sorted(
            [lv for lv in full_grid if lv.side == "buy"],
            key=lambda lv: lv.price,
        )[:per_side]
        desired = desired_sells + desired_buys

        # Get live orders na Lighter (authoritativo — DB pode estar dessincronizado)
        symbol = self._settings.dydx_symbol_token0
        try:
            live_orders = await self._exchange.get_open_orders(symbol)
        except Exception as e:
            logger.warning(f"reconcile: get_open_orders failed: {e}")
            return

        buffer = float(getattr(self._settings, "grid_anticipation_buffer", 0.0) or 0.0)
        price_decimals = 5  # TODO source from meta

        # Build live map: (side, recovered_tick_price) → order
        # Recover tick_price desfazendo o buffer: para sell, trigger = tick + buffer
        # → tick = trigger - buffer; para buy, tick = trigger + buffer.
        live_by_key: dict[tuple[str, float], dict] = {}
        for o in live_orders:
            if o["side"] == "sell":
                recovered = o["trigger_price"] - buffer
            else:
                recovered = o["trigger_price"] + buffer
            key = (o["side"], round(recovered, price_decimals))
            live_by_key[key] = o

        # Desired key set
        desired_keys = {
            (lv.side, round(lv.price, price_decimals)) for lv in desired
        }

        # Cancel extras (live mas não desired). Order indexes opcionais
        # — alguns adapters não preenchem; nesse caso skip (cancel_all_stops
        # já tratou no range_change path).
        extras_keys = set(live_by_key.keys()) - desired_keys
        for key in extras_keys:
            o = live_by_key[key]
            oi = o.get("order_index", 0)
            if not oi:
                continue
            try:
                await self._exchange.cancel_stop_order(
                    symbol=symbol, order_index=oi,
                )
                metrics.grid_stops_cancelled_total.inc()
                try:
                    await self._db.mark_grid_order_cancelled(
                        o["cloid"], time.time(),
                    )
                except Exception:
                    pass
                logger.info(
                    f"reconcile cancel {key[0]} @ ${key[1]:.5f} (cloid {o['cloid']})"
                )
            except Exception as e:
                logger.warning(f"reconcile cancel failed: {e}")

        # Post missing (desired mas não live)
        # CRITICAL guard 2026-05-14: o buffer pode empurrar o trigger PRA
        # ALÉM do market quando o tick está muito próximo (within ~1 V3
        # spacing). Resultado: SL_SELL com trigger > market → Lighter
        # aceita o tx mas rejeita silenciosamente no settlement zk-rollup
        # (sells "somem" depois de 200ms). Validado live 2026-05-14:
        # buffer $0.00005 → 4 sells closest dead; $0.00010 → 8 sells dead.
        # Solution: dynamic clamp do trigger pra ficar do lado correto do
        # market (com safety margin ~1 V3 tick = 0.01%).
        safety_frac = 0.0001  # 0.01% safety margin (~1 V3 tick em 0.05% pool)
        posted_count = 0
        skipped_too_close = 0
        for lv in desired:
            key = (lv.side, round(lv.price, price_decimals))
            if key in live_by_key:
                continue  # já no book
            if lv.side == "sell":
                max_trigger = p_now * (1 - safety_frac)
                if lv.price >= max_trigger:
                    skipped_too_close += 1
                    continue  # tick mesmo já tá em ou acima da safety bound
                trigger = min(lv.price + buffer, max_trigger)
            else:  # buy
                min_trigger = p_now * (1 + safety_frac)
                if lv.price <= min_trigger:
                    skipped_too_close += 1
                    continue
                trigger = max(lv.price - buffer, min_trigger)
            cloid = self._next_cloid_for_leg(symbol)
            try:
                await self._exchange.place_stop_market(
                    symbol=symbol, side=lv.side, size=lv.size,
                    trigger_price=trigger, cloid_int=cloid,
                )
                metrics.grid_stops_placed_total.inc()
                posted_count += 1
                try:
                    await self._db.insert_grid_order(
                        cloid=str(cloid), side=lv.side,
                        target_price=lv.price, size=lv.size,
                        placed_at=time.time(),
                        operation_id=self._hub.current_operation_id,
                        trigger_price=trigger, is_stop_order=1,
                    )
                except Exception as e:
                    logger.warning(f"reconcile db insert failed: {e}")
                logger.info(
                    f"reconcile post {lv.side} @ ${lv.price:.5f} trigger ${trigger:.5f}"
                )
            except Exception as e:
                logger.warning(
                    f"reconcile place_stop_market failed @ {lv.price}: {e}"
                )

        # Final count: live kept + posted
        kept_count = sum(1 for k in desired_keys if k in live_by_key)
        final_active = kept_count + posted_count
        metrics.grid_levels_active.set(final_active)
        metrics.grid_rebuild_total.labels(reason=rebuild_reason).inc()

        gh = self._hub.grid_health_metrics
        gh["levels_active"] = final_active
        gh["last_rebuild_reason"] = rebuild_reason
        gh["last_rebuild_ts"] = time.time()
        if rebuild_reason == "initial":
            gh["stops_placed_total"] = gh.get("stops_placed_total", 0) + posted_count
            gh["rebuilds_total"] = gh.get("rebuilds_total", 0) + 1
        elif posted_count > 0 or extras_keys:
            gh["stops_placed_total"] = gh.get("stops_placed_total", 0) + posted_count
            # Reconcile que mexeu em algo conta como "rebuild" leve

    async def _apply_fills_to_grid(
        self, *, filled_cloids: set[int], step: float,
        live_by_cloid: dict[int, dict],
    ) -> None:
        """Process detected fills: for each filled cloid, cancel the opposite
        side's farthest stop and post 2 replacements (1 near market at fill
        trigger, 1 extending the same-side range).

        Multi-fill handling: process fills in order of distance-from-market
        (closest first), so each iteration sees a coherent local_grid.

        `live_by_cloid` maps cloid -> live order dict (from `get_open_orders`),
        used to look up the Lighter `order_index` required by the real
        `cancel_stop_order(symbol, order_index)` adapter signature. cloids
        missing from this map are assumed already-cancelled-or-filled by
        external means and the cancel call is skipped (with warning), but
        the entry is still popped from local_grid.

        Spec: docs/superpowers/specs/2026-05-15-event-driven-grid-design.md
        """
        from engine.grid_state import GridStop, lowest_buy, top_sell, highest_sell, bottom_buy

        symbol = self._settings.dydx_symbol_token0

        async def _cancel_via_live(opp_cloid: int) -> None:
            """Resolve order_index from live_by_cloid and cancel; skip+warn if missing."""
            live_order = live_by_cloid.get(opp_cloid)
            if live_order is None:
                logger.warning(
                    f"event-driven cancel skipped: cloid={opp_cloid} not in live "
                    f"(already cancelled or filled?). Popping from local_grid."
                )
                return
            order_index = live_order.get("order_index", 0)
            if not order_index:
                logger.warning(
                    f"event-driven cancel skipped: cloid={opp_cloid} has no "
                    f"order_index in live response. Popping from local_grid."
                )
                return
            try:
                await self._exchange.cancel_stop_order(
                    symbol=symbol, order_index=order_index,
                )
            except Exception as e:
                logger.warning(
                    f"event-driven cancel failed cloid={opp_cloid} "
                    f"order_index={order_index}: {e}"
                )

        # Sort filled cloids by distance from extremes (closest to market first).
        # For a sell fill, "closest to market" = lowest trigger price among sells.
        # For a buy fill, "closest to market" = highest trigger price among buys.
        # We can't know the market price here without re-reading, so use a
        # heuristic: sells sorted ASC by trigger (lowest = was closest to market),
        # buys sorted DESC by trigger (highest = was closest to market).
        ordered = sorted(
            filled_cloids,
            key=lambda c: (
                self._local_grid[c].trigger_price
                if self._local_grid[c].side == "sell"
                else -self._local_grid[c].trigger_price
            ),
        )

        for cloid in ordered:
            stop = self._local_grid.get(cloid)
            if stop is None:
                continue  # already processed (race)

            # step <= 0 path (sparse grid edge case from _estimate_grid_step):
            # extending by 0 collides with existing extreme. Skip and let the
            # 90s safety net reconcile.
            if step <= 0:
                logger.warning(
                    f"event-driven skip fill cloid={cloid} side={stop.side}: "
                    f"step={step}, would collide with existing extreme. "
                    f"Safety net (90s) will reconcile."
                )
                continue

            if stop.side == "sell":
                opp = lowest_buy(self._local_grid)
                tip = top_sell(self._local_grid)
                if opp is None or tip is None:
                    continue  # malformed grid; safety net will recover
                # Cancel lowest buy (always pop opp from local_grid afterwards:
                # Lighter either cancelled it or it was already gone).
                await _cancel_via_live(opp.cloid)
                # Post replacement buy at filled sell's trigger price (closest to market)
                new_buy_cloid = self._next_cloid_for_leg(symbol)
                buy_posted = False
                try:
                    await self._exchange.place_stop_market(
                        symbol=symbol, side="buy", size=stop.size,
                        trigger_price=stop.trigger_price, cloid_int=new_buy_cloid,
                    )
                    buy_posted = True
                except Exception as e:
                    logger.warning(
                        f"event-driven post buy failed cloid={new_buy_cloid} "
                        f"trigger={stop.trigger_price}: {e}"
                    )
                # Post new sell extending the top
                new_sell_price = tip.trigger_price + step
                new_sell_cloid = self._next_cloid_for_leg(symbol)
                sell_posted = False
                try:
                    await self._exchange.place_stop_market(
                        symbol=symbol, side="sell", size=stop.size,
                        trigger_price=new_sell_price, cloid_int=new_sell_cloid,
                    )
                    sell_posted = True
                except Exception as e:
                    logger.warning(
                        f"event-driven post sell failed cloid={new_sell_cloid} "
                        f"trigger={new_sell_price}: {e}"
                    )
                # Update local_grid: only insert cloids that actually landed on
                # Lighter (phantom cloid prevention). Cancel always pops opp.
                self._local_grid.pop(cloid, None)
                self._local_grid.pop(opp.cloid, None)
                if buy_posted:
                    self._local_grid[new_buy_cloid] = GridStop(
                        new_buy_cloid, "buy", stop.trigger_price, stop.size,
                    )
                if sell_posted:
                    self._local_grid[new_sell_cloid] = GridStop(
                        new_sell_cloid, "sell", new_sell_price, stop.size,
                    )
            else:  # buy filled
                opp = highest_sell(self._local_grid)
                tip = bottom_buy(self._local_grid)
                if opp is None or tip is None:
                    continue
                await _cancel_via_live(opp.cloid)
                new_sell_cloid = self._next_cloid_for_leg(symbol)
                sell_posted = False
                try:
                    await self._exchange.place_stop_market(
                        symbol=symbol, side="sell", size=stop.size,
                        trigger_price=stop.trigger_price, cloid_int=new_sell_cloid,
                    )
                    sell_posted = True
                except Exception as e:
                    logger.warning(
                        f"event-driven post sell failed cloid={new_sell_cloid} "
                        f"trigger={stop.trigger_price}: {e}"
                    )
                new_buy_price = tip.trigger_price - step
                new_buy_cloid = self._next_cloid_for_leg(symbol)
                buy_posted = False
                try:
                    await self._exchange.place_stop_market(
                        symbol=symbol, side="buy", size=stop.size,
                        trigger_price=new_buy_price, cloid_int=new_buy_cloid,
                    )
                    buy_posted = True
                except Exception as e:
                    logger.warning(
                        f"event-driven post buy failed cloid={new_buy_cloid} "
                        f"trigger={new_buy_price}: {e}"
                    )
                self._local_grid.pop(cloid, None)
                self._local_grid.pop(opp.cloid, None)
                if sell_posted:
                    self._local_grid[new_sell_cloid] = GridStop(
                        new_sell_cloid, "sell", stop.trigger_price, stop.size,
                    )
                if buy_posted:
                    self._local_grid[new_buy_cloid] = GridStop(
                        new_buy_cloid, "buy", new_buy_price, stop.size,
                    )

    async def _safety_reconcile(self) -> None:
        """Periodic full audit (90s cadence). Behavior depends on local_grid state.

        Bootstrap path (local_grid empty after restart): query Lighter live
        orders, populate local_grid from them. NO cancellations.

        Steady-state path (local_grid populated): bidirectional diff.
          - Orders on Lighter not in local_grid → orphan, cancel.
          - Cloids in local_grid not on Lighter → assumed filled, re-trigger
            fill detection via _apply_fills_to_grid (idempotent).
        """
        from engine.grid_state import GridStop

        symbol = self._settings.dydx_symbol_token0
        try:
            live = await self._exchange.get_open_orders(symbol)
        except Exception as e:
            logger.warning(f"_safety_reconcile: get_open_orders failed: {e}")
            return

        live_by_cloid = {int(o["cloid"]): o for o in live}

        if not self._local_grid:
            # Bootstrap path
            for cloid, o in live_by_cloid.items():
                self._local_grid[cloid] = GridStop(
                    cloid=cloid, side=o["side"],
                    trigger_price=float(o["trigger_price"]),
                    size=float(o.get("size", 0.0)),
                )
            logger.info(
                f"_safety_reconcile bootstrap: populated local_grid with "
                f"{len(self._local_grid)} stops"
            )
            return

        # Steady-state path
        local_cloids = set(self._local_grid.keys())
        live_cloids = set(live_by_cloid.keys())

        # Orphans on Lighter (not in local) → cancel
        orphans = live_cloids - local_cloids
        for cloid in orphans:
            live_order = live_by_cloid.get(cloid, {})
            order_index = live_order.get("order_index", 0)
            if not order_index:
                logger.warning(
                    f"_safety_reconcile orphan cancel skipped: cloid={cloid} "
                    f"has no order_index in live response"
                )
                continue
            try:
                await self._exchange.cancel_stop_order(
                    symbol=symbol, order_index=order_index,
                )
                logger.info(
                    f"_safety_reconcile cancelled orphan cloid={cloid} "
                    f"order_index={order_index}"
                )
            except Exception as e:
                logger.warning(
                    f"_safety_reconcile orphan cancel failed cloid={cloid} "
                    f"order_index={order_index}: {e}"
                )

        # Missing on Lighter (in local but not live) → assumed filled
        missing = local_cloids - live_cloids
        if missing:
            step = self._estimate_grid_step()
            await self._apply_fills_to_grid(
                filled_cloids=missing, step=step, live_by_cloid=live_by_cloid,
            )

    def _estimate_grid_step(self) -> float:
        """Estimate grid step from existing _local_grid (diff between consecutive same-side prices).

        Used by _safety_reconcile when applying fills detected outside the normal flow.
        Falls back to 0.0 if grid too sparse (1 stop will be added at fill price; safety net
        next iter will fix any imbalance).
        """
        sells = sorted(
            (s.trigger_price for s in self._local_grid.values() if s.side == "sell"),
        )
        if len(sells) >= 2:
            return sells[1] - sells[0]
        buys = sorted(
            (s.trigger_price for s in self._local_grid.values() if s.side == "buy"),
            reverse=True,
        )
        if len(buys) >= 2:
            return buys[0] - buys[1]
        return 0.0

    async def _grid_event_iter(self) -> None:
        """One iteration of the event-driven grid loop. Public for testing.

        - get_position (cheap read)
        - if changed since last iter → query open_orders, identify filled cloids,
          apply fills (3 writes per fill)
        - every 90s, safety_reconcile audit
        """
        symbol = self._settings.dydx_symbol_token0

        # Safety net (90s)
        now = time.time()
        if now - self._last_safety_reconcile_at > 90.0:
            await self._safety_reconcile()
            self._last_safety_reconcile_at = now

        # Position read
        try:
            pos_now = await self._exchange.get_position(symbol)
        except Exception as e:
            logger.warning(f"_grid_event_iter: get_position failed: {e}")
            return

        # Position-equality short-circuit
        if self._position_equal(pos_now, self._last_known_position):
            return

        # Position changed — query open_orders, identify filled cloids
        try:
            live = await self._exchange.get_open_orders(symbol)
        except Exception as e:
            logger.warning(f"_grid_event_iter: get_open_orders failed: {e}")
            return
        live_by_cloid = {int(o["cloid"]): o for o in live}
        live_cloids = set(live_by_cloid.keys())
        filled = set(self._local_grid.keys()) - live_cloids

        if filled:
            step = self._estimate_grid_step()
            await self._apply_fills_to_grid(
                filled_cloids=filled, step=step, live_by_cloid=live_by_cloid,
            )

        self._last_known_position = pos_now

    @staticmethod
    def _position_equal(a, b) -> bool:
        """Compare two Position-ish objects by side + size (entry_price/PnL change frequently)."""
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return (
            getattr(a, "side", None) == getattr(b, "side", None)
            and abs(getattr(a, "size", 0.0) - getattr(b, "size", 0.0)) < 1e-9
        )

    async def _grid_event_loop(self) -> None:
        """Long-running task: event-driven grid maintenance at 100ms cadence."""
        period = 0.1  # 100ms
        while self._running:
            t0 = time.monotonic()
            try:
                await self._grid_event_iter()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"_grid_event_loop iter error: {e}")
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, period - elapsed))

    async def _on_grid_fill(
        self, *, cloid: int, fill_price: float, fill_size: float, side: str,
    ) -> None:
        """Fast-path tracking ONLY (metrics + db mark) — trailing logic vive em
        `_reconcile_grid` que roda toda iter.

        Spec 2026-05-14: o callback `_fill_callback` na adapter SÓ é
        disparado dentro de `place_long_term_order` (após inline confirm).
        Stop-market orders que disparam ASSINCRONAMENTE (mark cruza trigger
        depois) NÃO chegam aqui. Por isso a fonte autoritativa do estado
        da grade é `get_open_orders` query (em `_reconcile_grid`), não
        eventos de fill.

        Este handler fica como best-effort latency tracking quando o
        callback acontece (ex: place_taker drift correction).
        """
        if not self._settings.predictive_grid_v2:
            return
        metrics.grid_stops_filled_total.inc()
        gh = self._hub.grid_health_metrics
        gh["stops_filled_total"] = gh.get("stops_filled_total", 0) + 1
        try:
            row = await self._db.get_grid_order(cloid)
            if row and row.get("placed_at"):
                latency_ms = (time.time() - float(row["placed_at"])) * 1000.0
                metrics.grid_fill_latency_ms.observe(latency_ms)
            await self._db.mark_grid_order_filled(cloid, int(time.time()))
        except Exception as e:
            logger.warning(f"_on_grid_fill tracking failed (cosmetic): {e}")

    async def _maybe_correct_drift(
        self, *, beefy_pos, p_now: float,
        positions: list, symbols: list[str], targets: dict[str, float],
    ) -> None:
        """User spec 2026-05-14: under predictive_grid_v2, mesmo com a grade
        ativa, comparar posição short atual (Lighter actual) com o predicted
        pela V3 (`targets`). Se |diff_usd| > min_rebalance_notional_usd
        (default $0.50), dispara taker pra corrigir o desvio.

        Não mexe nas stops triggers do grid — só compensa rebalances Beefy
        que aconteceram entre fills, fills perdidos no WS, etc.
        """
        threshold_usd = float(
            getattr(self._settings, "min_rebalance_notional_usd", 1.0) or 1.0
        )
        for idx, sym in enumerate(symbols):
            pos = positions[idx] if idx < len(positions) else None
            target = float(targets.get(sym, 0.0))
            # CRITICAL guard 2026-05-14: WS drops na Lighter levam o adapter
            # a logar "preserving" mas o get_position downstream pode retornar
            # None ou size=0 transiente. Se isso acontecer e o `target` é
            # significativamente não-zero, NÃO confiar — pular a iteration.
            # Sem essa guarda, drift_correction fires SELL `target` partindo
            # de zero → posição explode catastroficamente quando a leitura
            # for falsa. Validado live 2026-05-14: drift fired SELL 516 ARB
            # com pos=0 enquanto position real era ~493 ARB.
            if pos is None:
                logger.warning(
                    f"drift_correction: skip {sym} — pos is None (WS drop?)"
                )
                continue
            current = abs(pos.size)
            if current == 0 and target > 0:
                logger.warning(
                    f"drift_correction: skip {sym} — pos.size=0 but target={target:.3f} "
                    f"(WS drop suspected; refusing to short into a stale 0 reading)"
                )
                continue
            ref_price = float(self._hub.dydx_quote_prices.get(sym, 0.0)) if hasattr(self._hub, "dydx_quote_prices") else 0.0
            if ref_price <= 0:
                # Fallback: usar p_now pra token0 (USD-pair). Cross-pair seria
                # diferente per leg, mas v2 atual é single-leg.
                ref_price = p_now if idx == 0 else 0.0
            if ref_price <= 0:
                continue
            drift_arb = target - current  # quanto adicional short queremos
            drift_usd = abs(drift_arb) * ref_price
            if drift_usd < threshold_usd:
                continue
            # Direção:
            #   drift_arb > 0 → precisa MAIS short → SELL drift_arb
            #   drift_arb < 0 → precisa MENOS short → BUY abs(drift_arb)
            corr_side = "sell" if drift_arb > 0 else "buy"
            corr_size = abs(drift_arb)
            cross_price = ref_price * (0.999 if corr_side == "sell" else 1.001)
            try:
                await self._exchange.place_long_term_order(
                    symbol=sym, side=corr_side, size=corr_size,
                    price=cross_price,
                    cloid_int=self._next_cloid_for_leg(sym),
                    ttl_seconds=60,
                )
                metrics.aggressive_corrections_total.inc()
                try:
                    await self._db.insert_order_log(
                        timestamp=time.time(), exchange=self._exchange.name,
                        action="place", side=corr_side, size=corr_size,
                        price=cross_price, reason=f"drift_correction_{sym}",
                        operation_id=self._hub.current_operation_id,
                    )
                except Exception:
                    pass
                logger.info(
                    f"drift_correction: {sym} {corr_side} {corr_size:.3f} "
                    f"(drift=${drift_usd:.2f} target={target:.3f} actual={current:.3f})"
                )
                # Update _last_known_position so _grid_event_loop doesn't misinterpret
                # this drift-correction-driven position change as a stop fill.
                # Only relevant for the primary (token0) leg — that's the leg the
                # event-driven grid tracks.
                if sym == self._settings.dydx_symbol_token0:
                    try:
                        self._last_known_position = await self._exchange.get_position(sym)
                    except Exception:
                        pass  # next _grid_event_iter will re-read; non-fatal
            except Exception as e:
                logger.warning(f"drift_correction taker failed: {e}")

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

        Over-hedge protection lives in the LighterAdapter's
        `get_effective_position` (see 2026-05-07 position-truth redesign).
        Engine reads `current` via `_safe_get_position`, which now
        returns the fused observed+expected magnitude — drift goes to 0
        right after a successful fire, so re-fire is impossible during
        WS lag. No engine-level cooldown needed.
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
            # Lighter is zero-fee, so no slippage accumulator on this
            # path. (When dYdX support comes back, wire fee model from
            # adapter meta instead of hardcoding 0.05% here.)
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
        legs never collide.

        Layout (64 bits): run_id (32) | leg_byte (8) | seq (24).
        See `_next_cloid` for the rationale on the 24-bit seq window.
        """
        self._cloid_seq += 1
        leg_byte = 0xA0 if symbol == self._settings.dydx_symbol_token0 else 0xA1
        return (
            ((self._run_id & 0xFFFFFFFF) << 32) |
            (leg_byte << 24) |
            (self._cloid_seq & 0xFFFFFF)
        )

    async def _on_funding_payment(self, entry) -> None:
        """Handle one funding payment from the exchange.

        Skips:
          - no active op (op_id is None) → entry will be picked up by
            the next op if/when one starts (engine doesn't carry funding
            across op boundaries).
          - market_id not in our hedged legs.
          - market_ids unresolved (don't mark seen — next call retries).
          - timestamp before op.started_at (backfill bound).
          - funding_id already counted for this op (dedup).

        Writes signed amount to the appropriate per-leg DB column,
        respecting pnl.py's convention that 'positive in DB = we paid':
          entry.change > 0 (user received) → DB delta = -change
          entry.change < 0 (user paid)     → DB delta = -change (= +|change|)
        """
        op_id = self._hub.current_operation_id
        if op_id is None:
            return
        # Reset dedup set on op transitions — funding from prior ops
        # was already attributed to those ops.
        if self._seen_funding_ids_op_id != op_id:
            self._seen_funding_ids = set()
            self._seen_funding_ids_op_id = op_id

        try:
            mid = int(getattr(entry, "market_id"))
            funding_id = int(getattr(entry, "funding_id"))
            ts = float(getattr(entry, "timestamp"))
            change = float(getattr(entry, "change") or 0)
        except (TypeError, ValueError, AttributeError) as e:
            logger.warning(f"funding payment parse failed: {e}")
            return

        # Resolve the leg.
        if self._token0_mid is None and self._token1_mid is None:
            # Metadata hasn't loaded yet — defer (don't mark seen).
            return
        if mid == self._token0_mid:
            field = "funding_paid_token0"
        elif mid == self._token1_mid:
            field = "funding_paid_token1"
        else:
            return  # not a leg we're hedging

        # Filter by op start.
        try:
            op_row = await self._db.get_operation(op_id)
            op_started_at = float((op_row or {}).get("started_at") or 0)
        except Exception:
            op_started_at = 0.0
        if ts < op_started_at:
            return

        # Dedup.
        if funding_id in self._seen_funding_ids:
            return
        self._seen_funding_ids.add(funding_id)

        delta = -change
        try:
            await self._db.add_to_operation_accumulator(op_id, field, delta)
        except Exception as e:
            logger.warning(
                f"funding accumulator write failed (op={op_id}, field={field}, "
                f"delta={delta}): {e}"
            )
            self._seen_funding_ids.discard(funding_id)  # allow retry

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

        # Predictive grid v2 (spec 2026-05-12): se o fill corresponde a uma
        # stop order da grade, dispatch _on_grid_fill pra repor o próximo tick.
        # No-op silencioso fora do modo predictive (flag off OU cloid não-grid).
        if self._settings.predictive_grid_v2 and fill.order_id:
            try:
                row = await self._db.get_grid_order(fill.order_id)
                if row is not None and row.get("is_stop_order"):
                    await self._on_grid_fill(
                        cloid=fill.order_id,
                        fill_price=fill.price,
                        fill_size=fill.size,
                        side=fill.side,
                    )
            except Exception as e:
                logger.warning(
                    f"_on_grid_fill dispatch failed for cloid={fill.order_id}: {e}",
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
