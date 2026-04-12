from __future__ import annotations
from dataclasses import dataclass, asdict


@dataclass
class PnLBreakdown:
    pool_pnl: float
    hedge_pnl: float
    funding_pnl: float
    fees_paid: float
    net_pnl: float

    def to_dict(self) -> dict:
        return asdict(self)


def calc_pnl(
    *, pool_value_usd: float, pool_deposited_usd: float,
    hedge_realized_pnl: float, hedge_unrealized_pnl: float,
    funding_total: float, total_fees_paid: float,
) -> PnLBreakdown:
    pool_pnl = pool_value_usd - pool_deposited_usd
    hedge_pnl = hedge_realized_pnl + hedge_unrealized_pnl
    net_pnl = pool_pnl + hedge_pnl + funding_total - total_fees_paid
    return PnLBreakdown(
        pool_pnl=pool_pnl,
        hedge_pnl=hedge_pnl,
        funding_pnl=funding_total,
        fees_paid=total_fees_paid,
        net_pnl=net_pnl,
    )
