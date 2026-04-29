"""Backtest event-driven simulator.

Drives the real GridMakerEngine through historical data via mock exchange/chain.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from unittest.mock import MagicMock, AsyncMock

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


class Simulator:
    """Runs the real GridMakerEngine over historical data.

    Open the operation as ACTIVE at t0; end-of-data is the implicit close.
    """

    def __init__(
        self, *,
        config: SimConfig,
        eth_prices: list[tuple[float, float]],
        funding: list[tuple[float, float]],
        apr_history: list[tuple[float, float]],
        range_events: list[dict],
        static_range: dict,
    ):
        self._config = config
        self._eth_prices = sorted(eth_prices, key=lambda x: x[0])
        self._funding = sorted(funding, key=lambda x: x[0])
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
        # Build mocks
        exchange = MockExchangeAdapter(
            symbol="ETH-USD",
            min_notional=0.001,
        )
        await exchange.connect()
        exchange._collateral = self._config.capital_dydx

        pool = MockPoolReader()
        beefy = MockBeefyReader()
        beefy.set_position(**self._static_range)

        # State hub: open operation as ACTIVE at t0
        state = StateHub(hedge_ratio=self._config.hedge_ratio)
        state.operation_state = "active"
        state.current_operation_id = 1  # synthetic op id
        state.dydx_collateral = self._config.capital_dydx

        settings = MagicMock()
        settings.dydx_symbol = "ETH-USD"
        settings.alert_webhook_url = ""
        settings.threshold_aggressive = self._config.threshold_aggressive
        settings.max_open_orders = self._config.max_open_orders
        settings.pool_token0_symbol = "WETH"
        settings.pool_token1_symbol = "USDC"

        # Mock DB: in-memory dicts shared across the run. Engine reads/writes
        # these via the closures wired below.
        active_grid_orders: list[dict] = []
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
        await exchange.subscribe_fills("ETH-USD", _on_fill_capture)

        # Main loop: walk price timeline
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
            net = self._compute_net_pnl(exchange, price)
            self._pnl_series.append((ts, net))

            prev_ts = ts

        # Build final result
        final_net = self._pnl_series[-1][1] if self._pnl_series else 0.0
        max_drawdown = 0.0
        peak = 0.0
        for _, p in self._pnl_series:
            peak = max(peak, p)
            max_drawdown = min(max_drawdown, p - peak)

        return {
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

    def _compute_net_pnl(self, exchange: MockExchangeAdapter, price: float) -> float:
        """Crude net-PnL estimate: collateral delta + LP fees earned so far.

        This excludes IL natural and unrealized hedge PnL — refine in T9 if needed.
        """
        return (exchange._collateral - self._config.capital_dydx) + self._lp_fees_earned
