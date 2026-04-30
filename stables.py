"""Constants for pair classification and dYdX symbol mapping.

STABLECOINS_ARBITRUM: addresses (checksum) of recognized stables on Arbitrum.
DYDX_TOKEN_TO_PERP: mapping from token0 symbol to dYdX perp ticker.
"""
from __future__ import annotations

# Stablecoins on Arbitrum (checksum addresses).
# When token1 ∈ this set, the pair is classified as USD-Pair (selectable).
# Otherwise → Cross-Pair (display-only, Phase 3.x scope).
STABLECOINS_ARBITRUM: set[str] = {
    "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC native
    "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9",  # USDT
    "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8",  # USDC.e (legacy bridged)
    "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1",  # DAI
}

# Token symbol → dYdX perp ticker. Maps wrapped/canonical names to
# the perp market on the dYdX indexer.
DYDX_TOKEN_TO_PERP: dict[str, str] = {
    "WETH": "ETH-USD",
    "ETH":  "ETH-USD",
    "WBTC": "BTC-USD",
    "BTC":  "BTC-USD",
    "ARB":  "ARB-USD",
    "LINK": "LINK-USD",
    "SOL":  "SOL-USD",
    "AVAX": "AVAX-USD",
    "MATIC": "MATIC-USD",
    "OP":   "OP-USD",
    "GMX":  "GMX-USD",
    "DOGE": "DOGE-USD",
    "ADA":  "ADA-USD",
    "ATOM": "ATOM-USD",
    "BNB":  "BNB-USD",
    "LTC":  "LTC-USD",
    "XRP":  "XRP-USD",
    "TRX":  "TRX-USD",
    "PEPE": "PEPE-USD",
    "SHIB": "SHIB-USD",
    # Add more as dYdX expands. Filter at runtime against actual indexer
    # market list — this map just states "this symbol *might* have a perp".
}


def is_stable(token_address: str) -> bool:
    """Returns True if the address (case-insensitive) is a recognized stable."""
    if not token_address:
        return False
    target = token_address.lower()
    return any(s.lower() == target for s in STABLECOINS_ARBITRUM)


def dydx_perp_for(token_symbol: str) -> str | None:
    """Returns the dYdX perp ticker for a token symbol, or None if unmapped.
    Case-insensitive.
    """
    if not token_symbol:
        return None
    return DYDX_TOKEN_TO_PERP.get(token_symbol.upper())
