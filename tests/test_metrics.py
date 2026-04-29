from engine import metrics


def test_metrics_module_exposes_expected_symbols():
    """All counters/gauges/histograms documented in the spec are present."""
    expected = [
        "fills_total", "alerts_total", "operations_total", "aggressive_corrections_total",
        "margin_ratio", "pool_value_usd", "hedge_position_size", "grid_orders_open",
        "operation_state", "out_of_range",
        "loop_duration",
    ]
    for name in expected:
        assert hasattr(metrics, name), f"missing metric: {name}"


def test_fills_total_is_counter_with_labels():
    from prometheus_client import Counter
    assert isinstance(metrics.fills_total, Counter)
    metrics.fills_total.labels(liquidity="maker", side="sell").inc()
    metrics.fills_total.labels(liquidity="taker", side="buy").inc()


def test_loop_duration_histogram_buckets():
    from prometheus_client import Histogram
    assert isinstance(metrics.loop_duration, Histogram)
    metrics.loop_duration.labels(step="chain_read").observe(0.123)
    metrics.loop_duration.labels(step="total").observe(0.5)


def test_render_metrics_returns_text():
    """render_metrics() returns Prometheus exposition text."""
    metrics.margin_ratio.set(1.25)
    body = metrics.render_metrics()
    assert b"bot_margin_ratio" in body
    assert b"1.25" in body


def test_render_content_type():
    """render_content_type() returns the Prometheus mimetype."""
    ct = metrics.render_content_type()
    assert "text/plain" in ct
    assert "version=" in ct
