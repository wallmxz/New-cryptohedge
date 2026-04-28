from __future__ import annotations
from dataclasses import dataclass, asdict

from engine.operation import Operation


BEEFY_PERF_FEE_RATE = 0.10  # Beefy takes ~10% of fees


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


def compute_operation_pnl(
    op: Operation,
    *,
    current_pool_value_usd: float,
    current_eth_price: float,
    hedge_realized_since_baseline: float,
    hedge_unrealized_since_baseline: float,
) -> dict:
    """Returns the live PnL breakdown for an active operation.

    Sign convention: positive = profit, negative = loss.
    funding: positive if bot received (longs paid), negative if bot paid.
    op.funding_paid stores it as "paid by us" so we negate to get the breakdown.
    """
    hodl_value = op.baseline_amount0 * current_eth_price + op.baseline_amount1
    # IL natural is the loss vs HODL — express as gain/loss vs baseline pool
    il_natural = current_pool_value_usd - hodl_value

    hedge_pnl = hedge_realized_since_baseline + hedge_unrealized_since_baseline

    beefy_perf = -BEEFY_PERF_FEE_RATE * op.lp_fees_earned

    breakdown = {
        "lp_fees_earned": op.lp_fees_earned,
        "beefy_perf_fee": beefy_perf,
        "il_natural": il_natural,
        "hedge_pnl": hedge_pnl,
        "funding": -op.funding_paid,  # negate: stored as paid, breakdown shows received
        "perp_fees_paid": -op.perp_fees_paid,
        "bootstrap_slippage": -op.bootstrap_slippage,
    }
    breakdown["net_pnl"] = sum(breakdown.values())
    return breakdown
