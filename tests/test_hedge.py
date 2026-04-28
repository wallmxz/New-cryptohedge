from engine.hedge import HedgeDecision, compute_hedge_action


def test_no_hedge_needed():
    result = compute_hedge_action(
        token_exposure_base=100.0,
        hedge_ratio=0.95, current_hedge_size=95.0, max_exposure_pct=0.05,
    )
    assert result.action == "HOLD"
    assert result.delta == 0.0


def test_small_exposure_maker_mode():
    result = compute_hedge_action(
        token_exposure_base=100.0,
        hedge_ratio=0.95, current_hedge_size=91.0, max_exposure_pct=0.05,
    )
    assert result.action == "MAKER"
    assert result.side == "sell"
    assert abs(result.delta - 4.0) < 0.01
    assert result.exposure_pct <= 0.05


def test_large_exposure_aggressive_mode():
    result = compute_hedge_action(
        token_exposure_base=100.0,
        hedge_ratio=0.95, current_hedge_size=70.0, max_exposure_pct=0.05,
    )
    assert result.action == "AGGRESSIVE"
    assert result.side == "sell"
    assert abs(result.delta - 25.0) < 0.01


def test_overhedged_needs_buy():
    result = compute_hedge_action(
        token_exposure_base=100.0,
        hedge_ratio=0.95, current_hedge_size=110.0, max_exposure_pct=0.05,
    )
    assert result.side == "buy"
    assert abs(result.delta - 15.0) < 0.01


def test_zero_exposure_hold():
    result = compute_hedge_action(
        token_exposure_base=0.0,
        hedge_ratio=0.95, current_hedge_size=0.0, max_exposure_pct=0.05,
    )
    assert result.action == "HOLD"


def test_safe_mode_always_hold():
    result = compute_hedge_action(
        token_exposure_base=100.0,
        hedge_ratio=0.95, current_hedge_size=0.0, max_exposure_pct=0.05,
        safe_mode=True,
    )
    assert result.action == "HOLD"
