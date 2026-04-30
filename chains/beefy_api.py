"""Fetcher for Beefy CLM data + APY + TVL with DB cache.

Uses these public endpoints:
- https://api.beefy.finance/cows         (CLM list with token info)
- https://api.beefy.finance/tvl           (per-chain per-vault TVL)
- https://api.beefy.finance/apy/breakdown (per-vault APY breakdown)
"""
from __future__ import annotations
import logging
import time
import httpx
from stables import is_stable, dydx_perp_for

logger = logging.getLogger(__name__)

BEEFY_API_BASE = "https://api.beefy.finance"
TARGET_CHAIN = "arbitrum"


class BeefyApiFetcher:
    """Fetches Beefy CLMs + APY + TVL, joins them, classifies, caches in DB."""

    def __init__(self, *, db):
        self._db = db

    async def refresh(self, *, active_dydx_tickers: set[str]) -> int:
        """Force re-fetch + classify + cache. Returns number of CLMs cached.

        Filters:
        - chain == 'arbitrum'
        - token0 symbol has dYdX perp AND that perp is in active_dydx_tickers

        Note: cross-pairs (token1 not stable) ARE included in cache; UI
        filter handles selectability.
        """
        async with httpx.AsyncClient(timeout=60.0) as client:
            cows_resp, tvl_resp, apy_resp = await self._fetch_all(client)

        cows_data = cows_resp.json()
        cows = cows_data if isinstance(cows_data, list) else []
        tvl_data = tvl_resp.json()
        tvl = tvl_data if isinstance(tvl_data, dict) else {}
        apy_data = apy_resp.json()
        apy = apy_data if isinstance(apy_data, dict) else {}

        await self._db.clear_beefy_cache()
        now = time.time()
        cached_count = 0

        for clm in cows:
            if clm.get("chain") != TARGET_CHAIN:
                continue
            try:
                pair = self._extract_pair(clm, tvl, apy, active_dydx_tickers, now)
            except (KeyError, IndexError, ValueError) as e:
                logger.debug(f"Skipping malformed CLM {clm.get('id')}: {e}")
                continue
            if pair is None:
                continue
            await self._db.upsert_beefy_pair(pair=pair)
            cached_count += 1

        logger.info(f"Beefy refresh: cached {cached_count} CLMs (chain={TARGET_CHAIN})")
        return cached_count

    async def _fetch_all(self, client):
        cows_resp = await client.get(f"{BEEFY_API_BASE}/cows")
        cows_resp.raise_for_status()
        tvl_resp = await client.get(f"{BEEFY_API_BASE}/tvl")
        tvl_resp.raise_for_status()
        apy_resp = await client.get(f"{BEEFY_API_BASE}/apy/breakdown")
        apy_resp.raise_for_status()
        return cows_resp, tvl_resp, apy_resp

    def _extract_pair(
        self, clm: dict, tvl_data: dict, apy_data: dict,
        active_dydx_tickers: set[str], now: float,
    ) -> dict | None:
        """Build a pair dict from raw CLM data. Returns None if should skip."""
        vault_id = clm.get("earnContractAddress") or ""
        if not vault_id:
            return None

        tokens = clm.get("tokens") or []
        if len(tokens) < 2:
            return None

        token0 = tokens[0]
        token1 = tokens[1]
        token0_symbol = (token0.get("symbol") or "").upper()
        token1_address = token1.get("address") or ""

        # Filter: token0 must have dYdX perp AND it must be active
        dydx_perp = dydx_perp_for(token0_symbol)
        if dydx_perp is None or dydx_perp not in active_dydx_tickers:
            return None

        is_usd = is_stable(token1_address)

        # Resolve TVL
        chain_tvls = tvl_data.get(TARGET_CHAIN) or {}
        tvl_usd = chain_tvls.get(clm.get("id"))

        # Resolve APY
        apy_block = apy_data.get(clm.get("id")) or {}
        apy_30d = apy_block.get("vaultAprDaily30d") or apy_block.get("vaultApr")

        return {
            "vault_id": vault_id,
            "chain": TARGET_CHAIN,
            "pool_address": clm.get("lpAddress") or clm.get("tokenAddress") or "",
            "token0_address": token0.get("address") or "",
            "token0_symbol": token0_symbol,
            "token0_decimals": int(token0.get("decimals") or 18),
            "token1_address": token1_address,
            "token1_symbol": (token1.get("symbol") or "").upper(),
            "token1_decimals": int(token1.get("decimals") or 6),
            "pool_fee": int(clm.get("feeTier") or 0),
            "manager": clm.get("strategyTypeId"),
            "tick_lower": clm.get("tickLower"),
            "tick_upper": clm.get("tickUpper"),
            "tvl_usd": tvl_usd,
            "apy_30d": apy_30d,
            "is_usd_pair": is_usd,
            "dydx_perp": dydx_perp,
            "token0_logo_url": f"{BEEFY_API_BASE}/token/{TARGET_CHAIN}/{token0_symbol}",
            "token1_logo_url": f"{BEEFY_API_BASE}/token/{TARGET_CHAIN}/{(token1.get('symbol') or '').upper()}",
            "fetched_at": now,
        }
