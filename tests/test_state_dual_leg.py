"""StateHub: dict-based hedge tracking + legacy property aggregates."""
from state import StateHub


def test_hedge_positions_dict_default_empty():
    s = StateHub()
    assert s.hedge_positions == {}
    assert s.hedge_unrealized_pnls == {}
    assert s.hedge_realized_pnls == {}
    assert s.funding_totals == {}


def test_legacy_hedge_position_returns_first_or_none():
    s = StateHub()
    assert s.hedge_position is None

    s.hedge_positions["ETH-USD"] = {"side": "short", "size": 0.1, "entry": 4000.0}
    assert s.hedge_position == {"side": "short", "size": 0.1, "entry": 4000.0}


def test_legacy_aggregates_sum_per_leg_values():
    s = StateHub()
    s.hedge_unrealized_pnls = {"ARB-USD": 2.0, "ETH-USD": -3.0}
    s.hedge_realized_pnls = {"ARB-USD": 1.0, "ETH-USD": 5.0}
    s.funding_totals = {"ARB-USD": 0.5, "ETH-USD": 0.7}

    assert s.hedge_unrealized_pnl == -1.0
    assert s.hedge_realized_pnl == 6.0
    assert s.funding_total == 1.2


def test_to_dict_includes_per_leg_dicts():
    s = StateHub()
    s.hedge_positions = {"ARB-USD": {"side": "short", "size": 100.0, "entry": 1.5}}
    snap = s.to_dict()
    assert "hedge_positions" in snap
    assert snap["hedge_positions"]["ARB-USD"]["size"] == 100.0
