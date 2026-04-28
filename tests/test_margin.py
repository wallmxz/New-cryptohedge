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
    assert classify_margin(0.9) == "info"      # 0.8 <= r < 1.0
    assert classify_margin(0.7) == "warning"   # 0.6 <= r < 0.8
    assert classify_margin(0.5) == "urgent"    # 0.4 <= r < 0.6
    assert classify_margin(0.3) == "critical"  # 0.2 <= r < 0.4
    assert classify_margin(0.1) == "emergency" # < 0.2
