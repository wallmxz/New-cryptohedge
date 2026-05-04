"""Backtest event-driven simulator.

Drives the real GridMakerEngine through historical data via mock exchange/chain.
Supports both single-leg (legacy) and dual-leg (cross-pair) modes.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from unittest.mock import MagicMock

from state import StateHub
from engine import GridMakerEngine
from backtest.exchange_mock import MockExchangeAdapter
from backtest.chain_mock import MockPoolReader, MockBeefyReader

logger = logging.getLogger(__name__)


@dataclass
class SimConfig:
    vault_address: str
    pool_address: str
    start_ts: float
    end_ts: float
    capital_lp: float = 300.0
    capital_dydx: float = 130.0
    hedge_ratio: float = 1.0
    threshold_aggressive: float = 0.01
    max_open_orders: int = 200
    tick_seconds: int = 300  # 5 min
    dydx_symbol_token0: str = "ETH-USD"
    dydx_symbol_token1: str = ""  # "" = single-leg; non-empty = dual-leg


class Simulator:
    """Runs the real GridMakerEngine over historical data.

    Open the operation as ACTIVE at t0; end-of-data is the implicit close.

    Modes:
    - Single-leg (legacy): pass `eth_prices` + `funding`. token1 is treated as
      a stable USD asset; only one perp leg is hedged on dydx_symbol_token0.
    - Dual-leg (cross-pair): pass `token0_prices` + `token1_prices` + per-leg
      funding lists. Both tokens are volatile, so the pool's price `p` is
      derived as `P0/E` each tick; the Beefy mock is configured for dynamic
      V3 amounts, and both perp legs are simulated.
    """

    def __init__(
        self, *,
        config: SimConfig,
        # legacy single-leg args:
        eth_prices: list[tuple[float, float]] | None = None,
        funding: list[tuple[float, float]] | None = None,
        # dual-leg args (new):
        token0_prices: list[tuple[float, float]] | None = None,
        token1_prices: list[tuple[float, float]] | None = None,
        funding_token0: list[tuple[float, float]] | None = None,
        funding_token1: list[tuple[float, float]] | None = None,
        apr_history: list[tuple[float, float]] = (),
        range_events: list[dict] = (),
        static_range: dict | None = None,
    ):
        self._config = config
        self._is_dual_leg = bool(config.dydx_symbol_token1)

        if self._is_dual_leg:
            assert token0_prices is not None and token1_prices is not None, \
                "dual-leg requires token0_prices and token1_prices"
            self._token0_prices = sorted(token0_prices, key=lambda x: x[0])
            self._token1_prices = sorted(token1_prices, key=lambda x: x[0])
            self._funding_t0 = sorted(funding_token0 or [], key=lambda x: x[0])
            self._funding_t1 = sorted(funding_token1 or [], key=lambda x: x[0])
        else:
            assert eth_prices is not None, "single-leg requires eth_prices"
            self._token1_prices = sorted(eth_prices, key=lambda x: x[0])
            self._token0_prices = None
            self._funding_t0 = sorted(funding or [], key=lambda x: x[0])
            self._funding_t1 = []

        # Backwards-compat aliases for the single-leg path: this preserves the
        # existing references in `_run_single_leg` (extracted verbatim from the
        # old main loop, which read `self._eth_prices` and `self._funding`).
        self._eth_prices = self._token1_prices  # alias used by single-leg path
        self._funding = self._funding_t0        # alias used by single-leg path

        self._apr_history = sorted(apr_history, key=lambda x: x[0])
        self._range_events = sorted(range_events, key=lambda x: x["ts"]) if range_events else []
        self._static_range = static_range

        # Output state
        self._fills_maker = 0
        self._fills_taker = 0
        self._lp_fees_earned = 0.0
        self._range_resets = 0
        self._out_of_range_seconds = 0.0
        self._pnl_series: list[tuple[float, float]] = []  # (ts, net_pnl_so_far)

    async def run(self) -> dict:
        # Build mocks (shared between single-leg and dual-leg).
        if self._is_dual_leg:
            symbols = [
                self._config.dydx_symbol_token0,
                self._config.dydx_symbol_token1,
            ]
            exchange = MockExchangeAdapter(
                symbols=symbols,
                min_notional=0.001,
            )
        else:
            exchange = MockExchangeAdapter(
                symbol=self._config.dydx_symbol_token0,
                min_notional=0.001,
            )
        await exchange.connect()
        exchange._collateral = self._config.capital_dydx

        pool = MockPoolReader()
        beefy = MockBeefyReader()
        if self._is_dual_leg:
            beefy.configure(
                p_a=self._static_range["p_a"],
                p_b=self._static_range["p_b"],
                L=self._static_range["L"],
                share=self._static_range["share"],
                tick_lower=self._static_range["tick_lower"],
                tick_upper=self._static_range["tick_upper"],
            )
        else:
            beefy.set_position(**self._static_range)

        # State hub: open operation as ACTIVE at t0
        state = StateHub(hedge_ratio=self._config.hedge_ratio)
        state.operation_state = "active"
        state.current_operation_id = 1  # synthetic op id
        state.dydx_collateral = self._config.capital_dydx

        settings = MagicMock()
        # Legacy alias (some callsites still reference this)
        settings.dydx_symbol = self._config.dydx_symbol_token0
        # Engine reads dydx_symbol_token0/token1 explicitly; MagicMock auto-creates
        # truthy attributes by default, so set them explicitly to avoid false
        # dual-leg detection in single-leg mode.
        settings.dydx_symbol_token0 = self._config.dydx_symbol_token0
        settings.dydx_symbol_token1 = self._config.dydx_symbol_token1
        settings.alert_webhook_url = ""
        settings.threshold_aggressive = self._config.threshold_aggressive
        settings.max_open_orders = self._config.max_open_orders
        settings.pool_token0_symbol = "WETH"
        settings.pool_token1_symbol = "USDC"
        # Token addresses / decimals (engine reads these for some paths)
        settings.token0_address = "0xtoken0"
        settings.token1_address = "0xtoken1"
        if self._is_dual_leg:
            settings.token0_decimals = 18
            settings.token1_decimals = 18
        else:
            settings.token0_decimals = 18
            settings.token1_decimals = 6

        # Mock DB: in-memory dicts shared across the run. Engine reads/writes
        # these via the closures wired below.
        active_grid_orders: list[dict] = []

        # Establish baseline pool value snapshot for the operation row.
        if self._is_dual_leg:
            # Dual-leg: derive p_now from token0[0] / token1[0].
            P0_0 = self._token0_prices[0][1] if self._token0_prices else 1.0
            E_0 = self._token1_prices[0][1] if self._token1_prices else 1.0
            p_now_0 = P0_0 / E_0
            # Dynamic Beefy: pool composition follows the V3 curve at p_now_0.
            from engine.curve import compute_x, compute_y
            p_a = self._static_range["p_a"]
            p_b = self._static_range["p_b"]
            L = self._static_range["L"]
            share = self._static_range["share"]
            if p_now_0 <= p_a:
                amount0 = compute_x(L, p_a, p_b)
                amount1 = 0.0
            elif p_now_0 >= p_b:
                amount0 = 0.0
                amount1 = compute_y(L, p_b, p_a)
            else:
                amount0 = compute_x(L, p_now_0, p_b)
                amount1 = compute_y(L, p_now_0, p_a)
            baseline_amount0 = amount0 * share
            baseline_amount1 = amount1 * share
            baseline_pool_value = baseline_amount0 * P0_0 + baseline_amount1 * E_0
            baseline_eth_price = P0_0
        else:
            baseline_eth_price = self._eth_prices[0][1] if self._eth_prices else 3000.0
            baseline_amount0 = self._static_range["amount0"] * self._static_range["share"]
            baseline_amount1 = self._static_range["amount1"] * self._static_range["share"]
            baseline_pool_value = baseline_amount0 * baseline_eth_price + baseline_amount1

        op_row = {
            "id": 1,
            "started_at": self._config.start_ts,
            "ended_at": None,
            "status": "active",
            "baseline_eth_price": baseline_eth_price,
            "baseline_pool_value_usd": baseline_pool_value,
            "baseline_amount0": baseline_amount0,
            "baseline_amount1": baseline_amount1,
            "baseline_collateral": self._config.capital_dydx,
            "perp_fees_paid": 0.0,
            "funding_paid": 0.0,
            "lp_fees_earned": 0.0,
            "bootstrap_slippage": 0.0,
            "final_net_pnl": None,
            "close_reason": None,
        }

        async def get_active_grid_orders():
            return list(active_grid_orders)

        async def insert_grid_order(*, cloid, side, target_price, size, placed_at, operation_id=None):
            active_grid_orders.append({
                "cloid": cloid,
                "side": side,
                "target_price": target_price,
                "size": size,
                "placed_at": placed_at,
                "operation_id": operation_id,
            })

        async def mark_grid_order_cancelled(cloid, ts):
            for r in active_grid_orders[:]:
                if r["cloid"] == cloid or str(r["cloid"]) == str(cloid):
                    active_grid_orders.remove(r)

        fill_id_seq = [0]
        async def insert_fill(**kw):
            fill_id_seq[0] += 1
            return fill_id_seq[0]

        async def mark_grid_order_filled(cloid, fill_id):
            for r in active_grid_orders[:]:
                if r["cloid"] == cloid or str(r["cloid"]) == str(cloid):
                    active_grid_orders.remove(r)

        async def insert_order_log(**kw):
            return None

        async def get_active_operation():
            return dict(op_row)

        async def get_operation(op_id):
            return dict(op_row)

        async def add_to_operation_accumulator(op_id, field, delta):
            if field in op_row and op_row[field] is not None:
                op_row[field] = op_row[field] + delta

        db = MagicMock()
        db.get_active_grid_orders = get_active_grid_orders
        db.insert_grid_order = insert_grid_order
        db.mark_grid_order_cancelled = mark_grid_order_cancelled
        db.insert_fill = insert_fill
        db.mark_grid_order_filled = mark_grid_order_filled
        db.insert_order_log = insert_order_log
        db.get_active_operation = get_active_operation
        db.get_operation = get_operation
        db.add_to_operation_accumulator = add_to_operation_accumulator

        engine = GridMakerEngine(
            settings=settings, hub=state, db=db,
            exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        )

        # Wrap engine._on_fill so we count maker/taker before delegating.
        original_on_fill = engine._on_fill
        async def _on_fill_capture(fill):
            if fill.liquidity == "maker":
                self._fills_maker += 1
            else:
                self._fills_taker += 1
            await original_on_fill(fill)

        # Subscribe per-symbol so each leg's fills are observed in dual-leg.
        if self._is_dual_leg:
            await exchange.subscribe_fills(self._config.dydx_symbol_token0, _on_fill_capture)
            await exchange.subscribe_fills(self._config.dydx_symbol_token1, _on_fill_capture)
        else:
            await exchange.subscribe_fills(self._config.dydx_symbol_token0, _on_fill_capture)

        # Dispatch by mode
        if self._is_dual_leg:
            await self._run_dual_leg(engine, exchange, pool, beefy, state, op_row)
        else:
            await self._run_single_leg(engine, exchange, pool, state, op_row)

        # Build final result
        final_net = self._pnl_series[-1][1] if self._pnl_series else 0.0
        max_drawdown = 0.0
        peak = 0.0
        for _, p in self._pnl_series:
            peak = max(peak, p)
            max_drawdown = min(max_drawdown, p - peak)

        result = {
            "net_pnl": round(final_net, 4),
            "fills_maker": self._fills_maker,
            "fills_taker": self._fills_taker,
            "lp_fees_earned": round(self._lp_fees_earned, 4),
            "range_resets": self._range_resets,
            "out_of_range_seconds": self._out_of_range_seconds,
            "max_drawdown": round(max_drawdown, 4),
            "duration_seconds": int(self._config.end_ts - self._config.start_ts),
            "pnl_series": self._pnl_series,
        }

        # Always include exchange_stats. In single-leg mode we report just the
        # one perp; in dual-leg mode both legs.
        stats: dict = {}
        if self._is_dual_leg:
            symbols = [
                self._config.dydx_symbol_token0,
                self._config.dydx_symbol_token1,
            ]
        else:
            symbols = [self._config.dydx_symbol_token0]
        for sym in symbols:
            pos = await exchange.get_position(sym)
            stats[sym] = {
                "position_size": pos.size if pos else 0.0,
                "side": pos.side if pos else None,
            }
        result["exchange_stats"] = stats

        return result

    async def _run_single_leg(self, engine, exchange, pool, state, op_row):
        """Walk eth_prices timeline, drive the engine each tick (legacy mode)."""
        prev_ts = self._config.start_ts
        next_funding_idx = 0
        next_apr_idx = 0
        current_apr = self._apr_history[0][1] if self._apr_history else 0.30

        for ts, price in self._eth_prices:
            if ts < self._config.start_ts:
                continue
            if ts > self._config.end_ts:
                break

            # Update mock chain state
            pool.set_price(price)

            # Drive exchange fills based on price step
            await exchange.advance_to_price(price, ts=ts)

            # Apply funding payments due in [prev_ts, ts]
            while (
                next_funding_idx < len(self._funding)
                and self._funding[next_funding_idx][0] <= ts
            ):
                f_ts, f_rate = self._funding[next_funding_idx]
                if f_ts >= prev_ts:
                    exchange.apply_funding(f_rate, f_ts)
                next_funding_idx += 1

            # Update APR if a newer sample is in effect
            while (
                next_apr_idx + 1 < len(self._apr_history)
                and self._apr_history[next_apr_idx + 1][0] <= ts
            ):
                next_apr_idx += 1
                current_apr = self._apr_history[next_apr_idx][1]

            # Accrue LP fees pro-rata for the tick interval
            interval_seconds = max(0.0, ts - prev_ts)
            year_seconds = 365.0 * 86400
            lp_fee_for_interval = (
                current_apr * self._config.capital_lp * interval_seconds / year_seconds
            )
            self._lp_fees_earned += lp_fee_for_interval
            op_row["lp_fees_earned"] = self._lp_fees_earned

            # Track out-of-range time
            if state.out_of_range:
                self._out_of_range_seconds += interval_seconds

            # Advance engine
            try:
                await engine._iterate()
            except Exception as e:
                logger.error(f"Engine iteration error at ts={ts}: {e}")

            # Track PnL series
            net = self._compute_net_pnl(exchange, price, state)
            self._pnl_series.append((ts, net))

            prev_ts = ts

    async def _run_dual_leg(self, engine, exchange, pool, beefy, state, op_row):
        """Cross-pair dual-leg main loop.

        Canonical clock: token0 timeline. token1 (ETH) is interpolated by
        last-sample-<=-ts. Pool price `p = P0 / E` is recomputed each tick and
        pushed into both `pool.set_price` and `beefy.set_p` so the Beefy mock
        re-derives the V3 amounts from the curve dynamically.
        """
        prev_ts = self._config.start_ts
        idx_t1 = 0
        idx_funding_t0 = 0
        idx_funding_t1 = 0
        idx_apr = 0
        current_apr = self._apr_history[0][1] if self._apr_history else 0.30

        for ts, P0 in self._token0_prices:
            if ts < self._config.start_ts:
                continue
            if ts > self._config.end_ts:
                break

            # Find current token1 (ETH) price: last sample <= ts
            while (
                idx_t1 + 1 < len(self._token1_prices)
                and self._token1_prices[idx_t1 + 1][0] <= ts
            ):
                idx_t1 += 1
            E = self._token1_prices[idx_t1][1] if self._token1_prices else 1.0
            if E <= 0:
                E = 1.0
            p_now = P0 / E

            # Update mock chain state — both pool's `p` and Beefy's V3 anchor.
            pool.set_price(p_now)
            beefy.set_p(p_now)

            # Drive per-leg fills based on each leg's price.
            await exchange.advance_to_prices(
                {
                    self._config.dydx_symbol_token0: P0,
                    self._config.dydx_symbol_token1: E,
                },
                ts=ts,
            )

            # Apply funding payments due in [prev_ts, ts] — per leg.
            while (
                idx_funding_t0 < len(self._funding_t0)
                and self._funding_t0[idx_funding_t0][0] <= ts
            ):
                f_ts, f_rate = self._funding_t0[idx_funding_t0]
                if f_ts >= prev_ts:
                    exchange.apply_funding(
                        f_rate, f_ts, symbol=self._config.dydx_symbol_token0
                    )
                idx_funding_t0 += 1
            while (
                idx_funding_t1 < len(self._funding_t1)
                and self._funding_t1[idx_funding_t1][0] <= ts
            ):
                f_ts, f_rate = self._funding_t1[idx_funding_t1]
                if f_ts >= prev_ts:
                    exchange.apply_funding(
                        f_rate, f_ts, symbol=self._config.dydx_symbol_token1
                    )
                idx_funding_t1 += 1

            # Update APR if a newer sample is in effect
            while (
                idx_apr + 1 < len(self._apr_history)
                and self._apr_history[idx_apr + 1][0] <= ts
            ):
                idx_apr += 1
                current_apr = self._apr_history[idx_apr][1]

            # Accrue LP fees pro-rata for the tick interval
            interval_seconds = max(0.0, ts - prev_ts)
            year_seconds = 365.0 * 86400
            lp_fee_for_interval = (
                current_apr * self._config.capital_lp * interval_seconds / year_seconds
            )
            self._lp_fees_earned += lp_fee_for_interval
            op_row["lp_fees_earned"] = self._lp_fees_earned

            # Track out-of-range time
            if state.out_of_range:
                self._out_of_range_seconds += interval_seconds

            # Advance engine
            try:
                await engine._iterate()
            except Exception as e:
                logger.error(f"Engine iteration error at ts={ts}: {e}")

            # Track PnL series — use P0 as the anchor USD price for the legacy
            # collateral-delta fallback (engine breakdown is preferred when set).
            net = self._compute_net_pnl(exchange, P0, state)
            self._pnl_series.append((ts, net))

            prev_ts = ts

    def _compute_net_pnl(
        self, exchange: MockExchangeAdapter, price: float, state
    ) -> float:
        """Net-PnL estimate using the engine's PnL breakdown when available.

        The engine populates ``state.operation_pnl_breakdown`` each iteration via
        ``compute_operation_pnl`` (LP fees, Beefy perf fee, IL natural, hedge
        realized + unrealized, funding, perp fees, bootstrap slippage). Use that
        as the source of truth so the simulator and engine stay aligned. Fall
        back to the crude collateral-delta calc only on the very first tick,
        before the engine has had a chance to populate the breakdown.
        """
        breakdown = state.operation_pnl_breakdown
        if breakdown and "net_pnl" in breakdown:
            return float(breakdown["net_pnl"])
        return (exchange._collateral - self._config.capital_dydx) + self._lp_fees_earned
