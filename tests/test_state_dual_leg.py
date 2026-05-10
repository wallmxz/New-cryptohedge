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


def test_to_dict_preserves_legacy_singular_keys():
    """SSE snapshot must keep the legacy hedge_position / hedge_unrealized_pnl
    / hedge_realized_pnl / funding_total keys for UI compat."""
    s = StateHub()
    s.hedge_positions = {"ETH-USD": {"side": "short", "size": 0.05, "entry": 4000.0}}
    s.hedge_unrealized_pnls = {"ETH-USD": -2.5}
    s.hedge_realized_pnls = {"ETH-USD": 3.0}
    s.funding_totals = {"ETH-USD": 0.5}

    snap = s.to_dict()

    # New dict keys still there
    assert "hedge_positions" in snap
    assert snap["hedge_positions"]["ETH-USD"]["size"] == 0.05

    # Legacy singular keys also present (UI compat)
    assert snap["hedge_position"] == {"side": "short", "size": 0.05, "entry": 4000.0}
    assert snap["hedge_unrealized_pnl"] == -2.5
    assert snap["hedge_realized_pnl"] == 3.0
    assert snap["funding_total"] == 0.5
