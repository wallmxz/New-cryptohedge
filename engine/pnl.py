"""Per-leg PnL breakdown for cross-pair operations.

Single-leg (token1 stable): pass current_eth_price + hedge_*_since_baseline.
Cross-pair (both volatile): pass current_token0_usd_price + current_token1_usd_price
  + hedge_*_per_symbol dicts.

Aggregates (hedge_pnl, perp_fees_paid, funding) are always present and equal
the sum of per-leg components. Per-leg fields exist only in the breakdown
when called with the cross-pair signature.
"""
from __future__ import annotations
from engine.operation import Operation


BEEFY_PERF_FEE_RATE = 0.10


def compute_operation_pnl(
    op: Operation,
    *,
    current_pool_value_usd: float,
    # cross-pair signature (preferred):
    current_token0_usd_price: float | None = None,
    current_token1_usd_price: float | None = None,
    hedge_realized_per_symbol: dict[str, float] | None = None,
    hedge_unrealized_per_symbol: dict[str, float] | None = None,
    # legacy single-leg signature (kept for backwards compat):
    current_eth_price: float | None = None,
    hedge_realized_since_baseline: float | None = None,
    hedge_unrealized_since_baseline: float | None = None,
    # Authoritative hedge_pnl override (used when the venue exposes
    # cumulative trade_pnl since op start — survives uvicorn restarts).
    hedge_pnl_aggregate_override: float | None = None,
    # Current unrealized PnL on the open position (from Lighter
    # `position.unrealized_pnl`). When provided AND override is set,
    # breakdown exposes:
    #   hedge_pnl_realized   = override - unrealized
    #   hedge_pnl_unrealized = unrealized
    #   hedge_pnl            = override (total = realized + unrealized)
    # User can compare hedge_pnl_unrealized directly against Lighter UI's
    # "Unrealized PnL" field (spec 2026-05-14).
    hedge_unrealized_override: float | None = None,
    # Funding override (token0_paid, token1_paid) — caller computed funding
    # for a user-selected window via get_funding_total_since. When provided,
    # bypasses op.funding_paid_token0/1 from the DB.
    funding_override: tuple[float, float] | None = None,
) -> dict:
    """Returns the live PnL breakdown for an operation.

    For cross-pair (dual-leg) ops, pass the cross-pair signature.
    For single-leg (legacy) ops, pass current_eth_price + hedge_*_since_baseline.
    """
    is_cross_pair = (
        current_token0_usd_price is not None
        and current_token1_usd_price is not None
    )

    # Resolve current prices for the IL calc.
    if is_cross_pair:
        p0_now = current_token0_usd_price
        p1_now = current_token1_usd_price
    else:
        # Single-leg: token0 is volatile (USD price = current_eth_price);
        # token1 is USDC (= $1).
        if current_eth_price is None:
            raise ValueError(
                "compute_operation_pnl needs either cross-pair signature "
                "(current_token0_usd_price + current_token1_usd_price) or "
                "legacy signature (current_eth_price)."
            )
        p0_now = current_eth_price
        p1_now = 1.0

    # Pool $ — user-facing "how much did my LP value change since op start".
    # Priority order for the cost basis:
    #   1. baseline_deposit_usd (set via POST /operations/<id>/baseline) —
    #      what the user explicitly invested at op start
    #   2. baseline_pool_value_usd (snapshot taken automatically by
    #      start_operation / open_shorts_for_existing_position) — the LP
    #      value at the moment the op was opened
    #   3. HODL divergence (LP - HODL@p_now) as last resort, just so the
    #      number isn't None
    #
    # Validated live 2026-05-14 op #29: baseline_deposit_usd was None
    # (hedge-existing path doesn't set it), so pool_dollar fell back to
    # the HODL formula which only captures the V3 vs hold delta (IL
    # natural) — for the user this was confusing: LP value actually
    # dropped from $199.76 to $198.59 (= -$1.17), but Pool $ showed -$0.05.
    # Switching priority to use baseline_pool_value_usd surfaces the
    # intuitive "money in LP now vs money in LP at start" view.
    #
    # `il_natural` (below) stays on the HODL formula for callers who
    # want the technical IL metric.
    if op.baseline_deposit_usd is not None and op.baseline_deposit_usd > 0:
        pool_dollar = current_pool_value_usd - op.baseline_deposit_usd
    elif op.baseline_pool_value_usd is not None and op.baseline_pool_value_usd > 0:
        pool_dollar = current_pool_value_usd - op.baseline_pool_value_usd
    else:
        hodl_value = op.baseline_amount0 * p0_now + op.baseline_amount1 * p1_now
        pool_dollar = current_pool_value_usd - hodl_value

    # IL natural (alias): always the HODL divergence — the V3 vs hold
    # comparison that captures impermanent loss specifically. Kept
    # separate from `pool_dollar` since 2026-05-14 (they used to be
    # identical; now pool_dollar is value-change-since-start and
    # il_natural is the technical IL).
    hodl_value = op.baseline_amount0 * p0_now + op.baseline_amount1 * p1_now
    il_natural = current_pool_value_usd - hodl_value

    # Hedge PnL.
    if hedge_pnl_aggregate_override is not None:
        # Authoritative venue-side cumulative trade_pnl since op start
        # (e.g., LighterAdapter.get_trade_pnl_since). Per-leg attribution
        # isn't available at this level, so the full value lives on
        # token0 and token1 stays 0 — the aggregate is what users see.
        hedge_pnl_t0 = hedge_pnl_aggregate_override
        hedge_pnl_t1 = 0.0
    elif is_cross_pair:
        rps = hedge_realized_per_symbol or {}
        ups = hedge_unrealized_per_symbol or {}
        # Symbol order is whatever's in the dicts; we pick keys deterministically
        # by sorted order so token0 vs token1 attribution is stable across calls.
        keys = sorted(set(rps) | set(ups))
        token0_key = keys[0] if keys else None
        token1_key = keys[1] if len(keys) > 1 else None

        hedge_pnl_t0 = (
            (rps.get(token0_key, 0.0) + ups.get(token0_key, 0.0))
            if token0_key else 0.0
        )
        hedge_pnl_t1 = (
            (rps.get(token1_key, 0.0) + ups.get(token1_key, 0.0))
            if token1_key else 0.0
        )
    else:
        hedge_pnl_t0 = (
            (hedge_realized_since_baseline or 0.0)
            + (hedge_unrealized_since_baseline or 0.0)
        )
        hedge_pnl_t1 = 0.0

    hedge_pnl = hedge_pnl_t0 + hedge_pnl_t1

    # Funding — convention: stored as "paid by us" so we negate to get the
    # signed amount in the breakdown (positive = received).
    if funding_override is not None:
        # Override path: caller (engine) computed funding for a
        # user-selected window via get_funding_total_since.
        # Override values are in "paid" convention (positive = paid);
        # display inverts to "received" convention for the breakdown.
        funding_t0 = -funding_override[0]
        funding_t1 = -funding_override[1]
    elif is_cross_pair:
        # Default path: cumulative since op.started_at from DB column
        # (populated by the funding poller).
        funding_t0 = -op.funding_paid_token0
        funding_t1 = -op.funding_paid_token1
    else:
        # Single-leg (USD-pair): poller writes to `funding_paid_token0`
        # (same column used by cross-pair token0 leg). The legacy
        # `funding_paid` field is no longer written to since the per-leg
        # split was introduced. Read from the active column.
        # Validated live 2026-05-14 op #29 (ARB/USDC.e): poller had
        # accumulated $0.042 in funding_paid_token0 but dashboard showed
        # $0.00 because this branch read the empty legacy field.
        funding_t0 = -op.funding_paid_token0
        funding_t1 = 0.0
    funding = funding_t0 + funding_t1

    # Perp fees — same shape as funding.
    if is_cross_pair:
        perp_fees_t0 = op.perp_fees_paid_token0
        perp_fees_t1 = op.perp_fees_paid_token1
    else:
        perp_fees_t0 = op.perp_fees_paid
        perp_fees_t1 = 0.0
    perp_fees = perp_fees_t0 + perp_fees_t1

    beefy_perf = -BEEFY_PERF_FEE_RATE * op.lp_fees_earned

    breakdown: dict = {
        "lp_fees_earned": op.lp_fees_earned,
        "beefy_perf_fee": beefy_perf,
        "pool_dollar": round(pool_dollar, 4),
        "baseline_deposit_usd": op.baseline_deposit_usd,
        # User-selected Hedge PnL window (2026-05-09). None → engine
        # uses op.started_at as the since_ts for get_trade_pnl_since.
        "pnl_window_since_ts": op.pnl_window_since_ts,
        # Alias for back-compat with any external consumer of the
        # breakdown (analytics scripts, older test fixtures).
        "il_natural": round(il_natural, 4),
        "hedge_pnl": hedge_pnl,
        "hedge_pnl_token0": hedge_pnl_t0,
        "hedge_pnl_token1": hedge_pnl_t1,
        # Decomposed (spec 2026-05-14): when the venue provides current
        # position's unrealized PnL, split hedge_pnl into realized + unreal
        # so dashboard can show "what's closed" vs "what's open".
        "hedge_pnl_unrealized": (
            hedge_unrealized_override
            if hedge_unrealized_override is not None
            else None
        ),
        "hedge_pnl_realized": (
            (hedge_pnl - hedge_unrealized_override)
            if hedge_unrealized_override is not None
            else None
        ),
        "funding": funding,
        "funding_token0": funding_t0,
        "funding_token1": funding_t1,
        "perp_fees_paid": -perp_fees,
        "perp_fees_paid_token0": -perp_fees_t0,
        "perp_fees_paid_token1": -perp_fees_t1,
        "bootstrap_slippage": -op.bootstrap_slippage,
    }
    # net_pnl sums only the AGGREGATE fields (not per-leg, not duplicates).
    # Excludes:
    #   - il_natural: now SEPARATE from pool_dollar (technical IL metric)
    #   - hedge_pnl_unrealized/realized: decomposition of hedge_pnl, would
    #     double-count if summed alongside hedge_pnl.
    #   - baseline_deposit_usd, pnl_window_since_ts: metadata (sometimes None)
    _excluded_from_net = {
        "baseline_deposit_usd", "pnl_window_since_ts",
        "il_natural",  # not a separate P&L line — pool_dollar is the canonical
        "hedge_pnl_unrealized", "hedge_pnl_realized",  # decomposition of hedge_pnl
    }
    breakdown["net_pnl"] = sum(
        v for k, v in breakdown.items()
        if not (k.endswith("_token0") or k.endswith("_token1"))
        and k not in _excluded_from_net
        and v is not None  # None values (e.g., unrealized when no override) skip
    )
    return breakdown
