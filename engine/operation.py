from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class OperationState(str, Enum):
    NONE = "none"
    STARTING = "starting"
    ACTIVE = "active"
    STOPPING = "stopping"
    CLOSED = "closed"
    FAILED = "failed"


_VALID_TRANSITIONS: dict[OperationState, set[OperationState]] = {
    OperationState.NONE: {OperationState.STARTING},
    OperationState.STARTING: {OperationState.ACTIVE, OperationState.FAILED},
    OperationState.ACTIVE: {OperationState.STOPPING, OperationState.FAILED},
    OperationState.STOPPING: {OperationState.CLOSED, OperationState.FAILED},
    OperationState.CLOSED: set(),
    OperationState.FAILED: set(),
}


def can_transition(from_state: OperationState, to_state: OperationState) -> bool:
    return to_state in _VALID_TRANSITIONS.get(from_state, set())


@dataclass
class Operation:
    id: int
    started_at: float
    state: OperationState
    baseline_eth_price: float
    baseline_pool_value_usd: float
    baseline_amount0: float
    baseline_amount1: float
    baseline_collateral: float
    ended_at: float | None = None
    perp_fees_paid: float = 0.0
    funding_paid: float = 0.0
    lp_fees_earned: float = 0.0
    bootstrap_slippage: float = 0.0
    final_net_pnl: float | None = None
    close_reason: str | None = None

    def is_active(self) -> bool:
        return self.state in (
            OperationState.STARTING, OperationState.ACTIVE, OperationState.STOPPING,
        )

    @classmethod
    def from_db_row(cls, row: dict) -> "Operation":
        return cls(
            id=row["id"],
            started_at=row["started_at"],
            ended_at=row.get("ended_at"),
            state=OperationState(row["status"]),
            baseline_eth_price=row["baseline_eth_price"],
            baseline_pool_value_usd=row["baseline_pool_value_usd"],
            baseline_amount0=row["baseline_amount0"],
            baseline_amount1=row["baseline_amount1"],
            baseline_collateral=row["baseline_collateral"],
            perp_fees_paid=row.get("perp_fees_paid", 0.0) or 0.0,
            funding_paid=row.get("funding_paid", 0.0) or 0.0,
            lp_fees_earned=row.get("lp_fees_earned", 0.0) or 0.0,
            bootstrap_slippage=row.get("bootstrap_slippage", 0.0) or 0.0,
            final_net_pnl=row.get("final_net_pnl"),
            close_reason=row.get("close_reason"),
        )
