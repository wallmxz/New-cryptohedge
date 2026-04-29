"""Pair resolver: format cached Beefy pairs for the UI.

Reads from db.list_cached_pairs(), separates USD-Pairs vs Cross-Pairs,
adds UI-friendly fields (formatted symbols, pct fee, range visualization),
flags non-selectable pairs with reason.
"""
from __future__ import annotations

# Currently-supported decimals combo for USD-Pairs.
# WBTC (8 dec) and exotic tokens are excluded until math is generalized.
SUPPORTED_DECIMALS_PAIR = {(18, 6)}


async def build_pair_list(*, db) -> dict:
    """Returns {usd_pairs, cross_pairs, selected_vault_id, last_refresh_ts}.

    Reads from cache only; does not trigger any HTTP fetch.
    """
    cached = await db.list_cached_pairs()
    selected = await db.get_selected_vault_id()
    usd, cross = [], []
    last_refresh = 0
    for raw in cached:
        formatted = format_pair_for_ui(raw)
        if raw.get("is_usd_pair"):
            usd.append(formatted)
        else:
            cross.append(formatted)
        if (raw.get("fetched_at") or 0) > last_refresh:
            last_refresh = raw["fetched_at"]
    return {
        "usd_pairs": usd,
        "cross_pairs": cross,
        "selected_vault_id": selected,
        "last_refresh_ts": last_refresh,
    }


def format_pair_for_ui(raw: dict) -> dict:
    """Convert a raw cached pair dict to UI shape with selectability + reason."""
    is_usd = bool(raw.get("is_usd_pair"))
    decimals_combo = (raw.get("token0_decimals", 0), raw.get("token1_decimals", 0))

    # Determine selectability + reason
    if not is_usd:
        selectable = False
        reason = "Phase 3.x — cross-pair requires dual-leg hedge"
    elif decimals_combo not in SUPPORTED_DECIMALS_PAIR:
        selectable = False
        reason = f"Decimals {decimals_combo} not supported in MVP (only (18,6))"
    else:
        selectable = True
        reason = None

    # Convert pool_fee from bps to pct (e.g., 500 -> 0.05)
    pool_fee_pct = (raw.get("pool_fee") or 0) / 10000.0

    return {
        "vault_id": raw.get("vault_id"),
        "pair": f"{raw.get('token0_symbol', '?')}-{raw.get('token1_symbol', '?')}",
        "token0_symbol": raw.get("token0_symbol"),
        "token1_symbol": raw.get("token1_symbol"),
        "token0_address": raw.get("token0_address"),
        "token1_address": raw.get("token1_address"),
        "token0_decimals": raw.get("token0_decimals"),
        "token1_decimals": raw.get("token1_decimals"),
        "manager": raw.get("manager") or "—",
        "dex": "Uniswap V3",  # only DEX supported today
        "pool_fee_pct": pool_fee_pct,
        "pool_address": raw.get("pool_address"),
        "tvl_usd": raw.get("tvl_usd"),
        "apy_30d": raw.get("apy_30d"),
        "tick_lower": raw.get("tick_lower"),
        "tick_upper": raw.get("tick_upper"),
        "token0_logo_url": raw.get("token0_logo_url"),
        "token1_logo_url": raw.get("token1_logo_url"),
        "dydx_perp": raw.get("dydx_perp"),
        "selectable": selectable,
        "reason": reason,
    }
