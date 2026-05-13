"""Prometheus metrics registry for AutoMoney bot.

Uses prometheus_client's default global registry. Helpers `render_metrics()` and
`render_content_type()` produce the response body and MIME type for the
/metrics HTTP endpoint.
"""
from __future__ import annotations
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST


# Counters --------------------------------------------------------------

fills_total = Counter(
    "bot_fills_total",
    "Total fills observed by the engine.",
    ["liquidity", "side"],
)

alerts_total = Counter(
    "bot_alerts_total",
    "Webhook alerts fired by the margin monitor.",
    ["level"],
)

operations_total = Counter(
    "bot_operations_total",
    "Operation lifecycle events.",
    ["status"],  # started, closed, failed
)

aggressive_corrections_total = Counter(
    "bot_aggressive_corrections_total",
    "Number of aggressive (taker) corrections fired by the engine.",
)

# Gauges ----------------------------------------------------------------

margin_ratio = Gauge(
    "bot_margin_ratio",
    "Current margin ratio (collateral / required). 999 means no position.",
)

pool_value_usd = Gauge(
    "bot_pool_value_usd",
    "Current LP pool value in USD.",
)

hedge_position_size = Gauge(
    "bot_hedge_position_size",
    "Current short position size in base units (e.g., WETH).",
)

grid_orders_open = Gauge(
    "bot_grid_orders_open",
    "Currently-open grid orders on the exchange.",
)

operation_state = Gauge(
    "bot_operation_state",
    "1 if an operation is active, 0 otherwise.",
)

out_of_range = Gauge(
    "bot_out_of_range",
    "1 if pool price is outside its range, 0 otherwise.",
)

# Histograms ------------------------------------------------------------

loop_duration = Histogram(
    "bot_loop_duration_seconds",
    "Duration of the main engine loop, broken down by step.",
    ["step"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
)


# Predictive grid v2 (spec 2026-05-12) ----------------------------------

grid_stops_placed_total = Counter(
    "bot_grid_stops_placed_total",
    "Total stop-limit orders postadas pelo predictive grid v2.",
)

grid_stops_filled_total = Counter(
    "bot_grid_stops_filled_total",
    "Total fills das stop-limit orders da grade predictive.",
)

grid_stops_cancelled_total = Counter(
    "bot_grid_stops_cancelled_total",
    "Cancelamentos de stops (por rebuild ou teardown).",
)

grid_rebuild_total = Counter(
    "bot_grid_rebuild_total",
    "Rebuilds da grade predictive por motivo.",
    ["reason"],  # fill, drift, range_change, range_exit
)

beefy_range_change_total = Counter(
    "bot_beefy_range_change_total",
    "Quantas vezes Beefy reposicionou o CLM (tick_lower/upper ou L mudou).",
)

grid_fill_latency_ms = Histogram(
    "bot_grid_fill_latency_ms",
    "Tempo entre trigger e fill da stop-limit (cenário B = miss-temporário).",
    buckets=[100, 500, 1000, 5000, 10000, 30000, 60000, 120000, 300000],
)

grid_replication_error_pct = Gauge(
    "bot_grid_replication_error_pct",
    "|sum(posted_target) - hedge_target| / hedge_target (drift da grade vs LP).",
)

grid_levels_active = Gauge(
    "bot_grid_levels_active",
    "Stops ativos no Lighter (varia conforme range Beefy).",
)

mark_vs_pool_drift_bps = Gauge(
    "bot_mark_vs_pool_drift_bps",
    "|markPrice Lighter - poolPrice Uniswap| em bps (informativo).",
)


# Helpers ---------------------------------------------------------------

def render_metrics() -> bytes:
    """Return the current Prometheus exposition text as bytes."""
    return generate_latest()


def render_content_type() -> str:
    """Return the MIME type for the Prometheus exposition format."""
    return CONTENT_TYPE_LATEST
