import time
from state import StateHub


def test_statehub_defaults():
    s = StateHub()
    assert s.pool_value_usd == 0.0
    assert s.hedge_position is None
    assert s.hedge_ratio == 0.95
    assert s.max_exposure_pct == 0.05
    assert s.safe_mode is False
    assert s.total_maker_fills == 0
    assert s.my_order is None


def test_statehub_exposure_calculation():
    s = StateHub()
    s.pool_value_usd = 200.0
    s.hedge_ratio = 0.95
    target = s.pool_value_usd * 0.5 * s.hedge_ratio
    assert target == 95.0


def test_statehub_snapshot():
    s = StateHub()
    s.pool_value_usd = 204.0
    s.pool_deposited_usd = 200.0
    s.hedge_unrealized_pnl = -3.80
    s.hedge_realized_pnl = 0.0
    s.funding_total = 0.15
    s.total_fees_paid = 0.30
    s.best_bid = 1.06
    s.best_ask = 1.0601

    snap = s.to_dict()
    assert snap["pool_value_usd"] == 204.0
    assert snap["best_bid"] == 1.06
    assert "last_update" in snap


def test_statehub_grid_fields_default():
    from state import StateHub
    s = StateHub()
    assert s.range_lower == 0.0
    assert s.range_upper == 0.0
    assert s.liquidity_l == 0.0
    assert s.current_grid == []
    assert isinstance(s.current_grid, list)
    assert s.dydx_collateral == 0.0
    assert s.margin_ratio == 999.0  # sentinel: no position yet
    assert s.out_of_range is False
