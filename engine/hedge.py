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
    *, token_exposure_base: float,
    hedge_ratio: float, current_hedge_size: float,
    max_exposure_pct: float, safe_mode: bool = False,
) -> HedgeDecision:
    # token_exposure_base: amount of the hedged token currently held in the pool
    # (base units, e.g. ARB). target_hedge and current_hedge_size must be in the
    # same base units so delta is unit-consistent.
    if safe_mode or token_exposure_base <= 0:
        return HedgeDecision(action="HOLD", side=None, delta=0.0, exposure_pct=0.0, target_hedge=0.0)

    target_hedge = token_exposure_base * hedge_ratio
    delta = target_hedge - current_hedge_size
    exposure_pct = abs(delta) / token_exposure_base

    if abs(delta) < 1e-6:
        return HedgeDecision(action="HOLD", side=None, delta=0.0, exposure_pct=exposure_pct, target_hedge=target_hedge)

    side = "sell" if delta > 0 else "buy"
    action = "MAKER" if exposure_pct <= max_exposure_pct else "AGGRESSIVE"

    return HedgeDecision(action=action, side=side, delta=abs(delta), exposure_pct=exposure_pct, target_hedge=target_hedge)
