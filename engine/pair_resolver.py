"""Pair resolver: format cached Beefy pairs for the UI.

Reads from db.list_cached_pairs(), separates USD-Pairs vs Cross-Pairs,
adds UI-friendly fields (formatted symbols, pct fee, range visualization),
flags non-selectable pairs with reason.
"""
from __future__ import annotations

# Currently-supported decimals combos. (18, 6) covers WETH/USDC and similar
# USD-pairs; (18, 18) covers cross-pairs like ARB/WETH.
# WBTC (8 dec) and other exotic tokens are excluded until math is generalized.
SUPPORTED_DECIMALS_PAIR = {(18, 6), (18, 18)}


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
    perp_token1 = raw.get("dydx_perp_token1")

    # Determine selectability + reason
    if is_usd:
        if decimals_combo not in SUPPORTED_DECIMALS_PAIR:
            selectable = False
            reason = (
                f"Decimals {decimals_combo} not supported in MVP "
                f"(only {sorted(SUPPORTED_DECIMALS_PAIR)})"
            )
        else:
            selectable = True
            reason = None
    else:
        # Cross-pair (token1 not stable) — needs token1 perp for dual-leg hedge
        if not perp_token1:
            selectable = False
            reason = "Cross-pair: token1 sem perp dYdX ativo"
        elif decimals_combo not in SUPPORTED_DECIMALS_PAIR:
            selectable = False
            reason = f"Decimals {decimals_combo} not supported in MVP"
        else:
            selectable = True
            reason = None

    # Convert Uniswap V3 pool_fee parameter (pip-millionths, e.g. 500) to a
    # fraction (e.g. 0.0005 = 0.05%). The router takes the integer; for display
    # we want the percent. NOTE: Uniswap's fee tiers are in millionths, NOT
    # basis points — 500 = 0.05% (5 bps), not 5%. A previous version of this
    # code divided by 10000 and produced "5.00%" displays for the 0.05% pool.
    pool_fee_frac = (raw.get("pool_fee") or 0) / 1_000_000.0

    # Beefy CLMs sit on top of multiple V3 DEXes (Uniswap, Sushi, Camelot,
    # PancakeSwap). The underlying DEX comes from `manager` (= tokenProviderId
    # in Beefy /cow-vaults), which we capitalize for display. "Beefy" is shown
    # as the strategy platform — the LP itself is on the underlying DEX.
    dex_raw = (raw.get("manager") or "").strip()
    dex_label_map = {
        "uniswap": "Uniswap V3",
        "sushi": "SushiSwap V3",
        "camelot": "Camelot V3",
        "pancakeswap": "PancakeSwap V3",
    }
    dex = dex_label_map.get(dex_raw.lower(), dex_raw.title() if dex_raw else "—")

    # When pool_fee parses to 0 (e.g., feeTier="Dynamic" for Camelot), surface
    # that distinction so the UI shows "Dynamic" instead of "0.00%".
    pool_fee_label = "Dynamic" if pool_fee_frac == 0 else f"{pool_fee_frac * 100:.2f}%"

    return {
        "vault_id": raw.get("vault_id"),
        "pair": f"{raw.get('token0_symbol', '?')}-{raw.get('token1_symbol', '?')}",
        "token0_symbol": raw.get("token0_symbol"),
        "token1_symbol": raw.get("token1_symbol"),
        "token0_address": raw.get("token0_address"),
        "token1_address": raw.get("token1_address"),
        "token0_decimals": raw.get("token0_decimals"),
        "token1_decimals": raw.get("token1_decimals"),
        "manager": "Beefy",
        "dex": dex,
        "pool_fee_pct": pool_fee_frac,
        "pool_fee_label": pool_fee_label,
        "pool_address": raw.get("pool_address"),
        "tvl_usd": raw.get("tvl_usd"),
        "apy_30d": raw.get("apy_30d"),
        "tick_lower": raw.get("tick_lower"),
        "tick_upper": raw.get("tick_upper"),
        "token0_logo_url": raw.get("token0_logo_url"),
        "token1_logo_url": raw.get("token1_logo_url"),
        "dydx_perp": raw.get("dydx_perp"),
        "dydx_perp_token1": raw.get("dydx_perp_token1"),
        "selectable": selectable,
        "reason": reason,
    }
