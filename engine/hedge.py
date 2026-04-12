from __future__ import annotations
from dataclasses import dataclass


@dataclass
class HedgeDecision:
    action: str
    side: str | None
    delta: float
    exposure_pct: float
    target_hedge: float


def compute_hedge_action(
    *, pool_value_usd: float, token_exposure_ratio: float,
    hedge_ratio: float, current_hedge_size: float,
    max_exposure_pct: float, safe_mode: bool = False,
) -> HedgeDecision:
    if safe_mode or pool_value_usd <= 0:
        return HedgeDecision(action="HOLD", side=None, delta=0.0, exposure_pct=0.0, target_hedge=0.0)

    target_hedge = pool_value_usd * token_exposure_ratio * hedge_ratio
    delta = target_hedge - current_hedge_size
    exposure_pct = abs(delta) / pool_value_usd if pool_value_usd > 0 else 0.0

    if abs(delta) < 0.01:
        return HedgeDecision(action="HOLD", side=None, delta=0.0, exposure_pct=exposure_pct, target_hedge=target_hedge)

    side = "sell" if delta > 0 else "buy"
    action = "MAKER" if exposure_pct <= max_exposure_pct else "AGGRESSIVE"

    return HedgeDecision(action=action, side=side, delta=abs(delta), exposure_pct=exposure_pct, target_hedge=target_hedge)
