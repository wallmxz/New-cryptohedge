import pytest
from unittest.mock import MagicMock
from engine import GridMakerEngine


def _make_engine():
    settings = MagicMock()
    settings.dydx_symbol_token0 = "ARB-USD"
    settings.dydx_symbol_token1 = "ETH-USD"
    return GridMakerEngine(
        settings=settings, hub=MagicMock(), db=MagicMock(), exchange=None,
    )


def test_engine_has_event_driven_state_vars_init_empty():
    engine = _make_engine()
    assert engine._last_known_position is None
    assert engine._local_grid == {}
    assert engine._last_safety_reconcile_at == 0.0
