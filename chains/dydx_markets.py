"""Fetcher for dYdX indexer perpetualMarkets endpoint with DB cache."""
from __future__ import annotations
import logging
import time
import httpx

logger = logging.getLogger(__name__)

DYDX_INDEXER_BASE = "https://indexer.dydx.trade/v4"
MARKETS_ENDPOINT = f"{DYDX_INDEXER_BASE}/perpetualMarkets"


class DydxMarketsFetcher:
    """Fetches dYdX perp markets list and persists in DB cache.

    The indexer returns all markets with their status (ACTIVE, PAUSED, etc).
    We cache everything but only consider ACTIVE for filtering.
    """

    def __init__(self, *, db):
        self._db = db

    async def refresh(self) -> int:
        """Force re-fetch from indexer. Replaces cache. Returns number of markets stored.

        Raises if HTTP fails (so caller can show error to user).
        """
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(MARKETS_ENDPOINT)
            resp.raise_for_status()
            payload = resp.json()

        markets = payload.get("markets", {})
        await self._db.clear_dydx_cache()
        now = time.time()
        for ticker, info in markets.items():
            await self._db.upsert_dydx_market(
                ticker=ticker,
                status=info.get("status", "UNKNOWN"),
                fetched_at=now,
            )
        logger.info(f"dYdX markets refresh: {len(markets)} markets cached")
        return len(markets)

    async def get_active_tickers(self) -> set[str]:
        """Returns set of currently-cached ACTIVE tickers."""
        return await self._db.get_active_dydx_tickers()
