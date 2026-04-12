from __future__ import annotations
import time
from dataclasses import dataclass, field, asdict


@dataclass
class StateHub:
    # Pool
    pool_value_usd: float = 0.0
    pool_deposited_usd: float = 0.0
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
    max_exposure_pct: float = 0.05
    repost_depth: int = 3

    # Metrics
    total_maker_fills: int = 0
    total_taker_fills: int = 0
    total_maker_volume: float = 0.0
    total_taker_volume: float = 0.0
    total_fees_paid: float = 0.0
    total_fees_earned: float = 0.0

    # System
    connected_exchange: bool = False
    connected_chain: bool = False
    safe_mode: bool = False
    last_update: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        self.last_update = time.time()
        return asdict(self)
