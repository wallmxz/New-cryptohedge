from __future__ import annotations
import time
from dataclasses import dataclass, field, asdict


@dataclass
class StateHub:
    # Pool
    pool_value_usd: float = 0.0
    pool_tokens: dict = field(default_factory=dict)
    cow_balance: float = 0.0
    cow_total_supply: float = 0.0
    vault_balances: tuple = (0.0, 0.0)

    # Hedge
    hedge_position: dict | None = None
    hedge_unrealized_pnl: float = 0.0
    hedge_realized_pnl: float = 0.0
    funding_total: float = 0.0

    # Orderbook
    best_bid: float = 0.0
    best_ask: float = 0.0
    my_order: dict | None = None
    my_order_depth: int = 0

    # Config
    hedge_ratio: float = 0.95

    # Metrics
    total_maker_fills: int = 0
    total_taker_fills: int = 0
    total_maker_volume: float = 0.0
    total_taker_volume: float = 0.0
    total_fees_paid: float = 0.0
    total_fees_earned: float = 0.0

    # Grid state
    range_lower: float = 0.0
    range_upper: float = 0.0
    liquidity_l: float = 0.0
    current_grid: list = field(default_factory=list)  # list[GridLevel]

    # Margin
    dydx_collateral: float = 0.0
    margin_ratio: float = 999.0  # margin_ratio = collateral / required_collateral. See engine/margin.py for thresholds.

    # Out-of-range flag
    out_of_range: bool = False

    # System
    connected_exchange: bool = False
    connected_chain: bool = False
    safe_mode: bool = False
    last_update: float = field(default_factory=time.time)

    # Operation lifecycle
    current_operation_id: int | None = None
    operation_state: str = "none"  # none/starting/active/stopping/closed/failed
    operation_pnl_breakdown: dict = field(default_factory=dict)

    # Observability — populated by engine each iteration
    last_iter_timings: dict = field(default_factory=dict)  # {"chain_read": 250.5, "total": 442.1, ...} ms

    # Phase 2.0 on-chain execution
    wallet_eth_balance: float = 0.0
    bootstrap_progress: str = ""  # human-readable string for UI ("Swapping...", "Depositing...")

    def to_dict(self) -> dict:
        self.last_update = time.time()
        return asdict(self)
