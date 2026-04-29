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


# Helpers ---------------------------------------------------------------

def render_metrics() -> bytes:
    """Return the current Prometheus exposition text as bytes."""
    return generate_latest()


def render_content_type() -> str:
    """Return the MIME type for the Prometheus exposition format."""
    return CONTENT_TYPE_LATEST
