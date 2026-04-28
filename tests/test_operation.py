import pytest
from engine.operation import Operation, OperationState, can_transition


def test_operation_initial_state():
    op = Operation(
        id=1, started_at=1000.0, state=OperationState.STARTING,
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0, baseline_collateral=130.0,
    )
    assert op.state == OperationState.STARTING
    assert op.is_active()


def test_can_transition_valid():
    assert can_transition(OperationState.NONE, OperationState.STARTING)
    assert can_transition(OperationState.STARTING, OperationState.ACTIVE)
    assert can_transition(OperationState.ACTIVE, OperationState.STOPPING)
    assert can_transition(OperationState.STOPPING, OperationState.CLOSED)


def test_can_transition_invalid():
    assert not can_transition(OperationState.NONE, OperationState.ACTIVE)
    assert not can_transition(OperationState.CLOSED, OperationState.ACTIVE)
    assert not can_transition(OperationState.STARTING, OperationState.NONE)


def test_failed_transition_from_any_active():
    """Any non-terminal state can transition to FAILED."""
    assert can_transition(OperationState.STARTING, OperationState.FAILED)
    assert can_transition(OperationState.ACTIVE, OperationState.FAILED)
    assert can_transition(OperationState.STOPPING, OperationState.FAILED)


def test_is_active_includes_starting_active_stopping():
    for st in (OperationState.STARTING, OperationState.ACTIVE, OperationState.STOPPING):
        op = Operation(
            id=1, started_at=1000.0, state=st,
            baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
            baseline_amount0=0.05, baseline_amount1=150.0, baseline_collateral=130.0,
        )
        assert op.is_active()


def test_is_active_excludes_terminal():
    for st in (OperationState.NONE, OperationState.CLOSED, OperationState.FAILED):
        op = Operation(
            id=1, started_at=1000.0, state=st,
            baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
            baseline_amount0=0.05, baseline_amount1=150.0, baseline_collateral=130.0,
        )
        assert not op.is_active()


def test_from_db_row():
    row = {
        "id": 5, "started_at": 1000.0, "ended_at": None,
        "status": "active",
        "baseline_eth_price": 3000.0, "baseline_pool_value_usd": 300.0,
        "baseline_amount0": 0.05, "baseline_amount1": 150.0,
        "baseline_collateral": 130.0,
        "perp_fees_paid": 1.5, "funding_paid": 0.3,
        "lp_fees_earned": 2.1, "bootstrap_slippage": 0.07,
        "final_net_pnl": None, "close_reason": None,
    }
    op = Operation.from_db_row(row)
    assert op.id == 5
    assert op.state == OperationState.ACTIVE
    assert op.perp_fees_paid == 1.5
