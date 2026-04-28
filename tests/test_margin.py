from engine.margin import compute_margin_ratio, classify_margin


def test_margin_ratio_healthy():
    """Collateral 200 with required 100 -> ratio 2.0."""
    r = compute_margin_ratio(collateral=200.0, required=100.0)
    assert r == 2.0


def test_margin_ratio_zero_when_required_zero():
    """No position -> ratio is infinity (we use 999 sentinel)."""
    r = compute_margin_ratio(collateral=100.0, required=0.0)
    assert r >= 999


def test_classify_margin_healthy_warning_critical():
    assert classify_margin(2.0) == "healthy"
    assert classify_margin(0.85) == "info"
    assert classify_margin(0.55) == "warning"
    assert classify_margin(0.35) == "urgent"
    assert classify_margin(0.15) == "critical"
