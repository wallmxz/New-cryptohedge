from __future__ import annotations
import time
from dataclasses import dataclass, field, asdict


@dataclass
class StateHub:
    # Pool
    pool_value_usd: float = 0.0
    pool_tokens: dict = field(default_factory=dict)

    # Hedge — dict per symbol (cross-pair has 2 entries)
    hedge_positions: dict = field(default_factory=dict)
    hedge_unrealized_pnls: dict = field(default_factory=dict)
    hedge_realized_pnls: dict = field(default_factory=dict)
    funding_totals: dict = field(default_factory=dict)

    # Orderbook (legacy fields kept for dashboard partials that haven't been
    # rewritten for the grid model; engine never writes them — they always
    # render as defaults).
    best_bid: float = 0.0
    best_ask: float = 0.0
    my_order: dict | None = None

    # Config
    hedge_ratio: float = 0.95

    # Metrics
    total_maker_fills: int = 0
    total_taker_fills: int = 0
    total_maker_volume: float = 0.0
    total_taker_volume: float = 0.0
    total_fees_paid: float = 0.0
    total_fees_earned: float = 0.0  # rendered in pnl.html; engine does not write

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

    # Predictive grid status (predictive grid spec 2026-05-08).
    # Values: "idle", "active", "warmup", "no_grid",
    #         "fallback: <reason>". Surfaces in dashboard.
    predictive_status: str = "idle"

    # Live USD prices for the active pair's tokens (from exchange oracle —
    # WS midpoint on Lighter). The dashboard uses these to USD-format
    # the wallet residual balance, the LP curve preview, etc. Without
    # them the UI used to hardcode `* 3000` (assuming ETH=$3000), which
    # was off by ~30% as of 2026-05.
    token0_usd_price: float = 0.0
    token1_usd_price: float = 0.0

    @property
    def hedge_position(self) -> dict | None:
        """Legacy compat: returns first hedge position (single-leg) or None."""
        if not self.hedge_positions:
            return None
        return next(iter(self.hedge_positions.values()))

    @property
    def hedge_unrealized_pnl(self) -> float:
        return sum(self.hedge_unrealized_pnls.values())

    @property
    def hedge_realized_pnl(self) -> float:
        return sum(self.hedge_realized_pnls.values())

    @property
    def funding_total(self) -> float:
        return sum(self.funding_totals.values())

    def to_dict(self) -> dict:
        self.last_update = time.time()
        snap = asdict(self)
        # Backwards-compat for UI/SSE consumers that read singular hedge_*
        # field names. These are properties on the dataclass, so asdict()
        # doesn't capture them; we add them manually here.
        snap["hedge_position"] = self.hedge_position
        snap["hedge_unrealized_pnl"] = self.hedge_unrealized_pnl
        snap["hedge_realized_pnl"] = self.hedge_realized_pnl
        snap["funding_total"] = self.funding_total
        return snap
