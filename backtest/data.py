# backtest/data.py
"""Historical data fetchers for backtest. Caches results to SQLite."""
from __future__ import annotations
import json
import logging
import httpx
from backtest.cache import Cache

logger = logging.getLogger(__name__)

COINBASE_BASE = "https://api.exchange.coinbase.com"


class DataFetcher:
    """Fetches historical data with caching. APIs hit only on cache miss."""

    def __init__(self, cache: Cache):
        self._cache = cache

    async def fetch_eth_prices(
        self, *, start: float, end: float, interval: int = 300,
        product_id: str = "ETH-USD",
    ) -> list[tuple[float, float]]:
        """Fetch ETH/USD candles between start..end (unix seconds). Returns sorted (ts, close_price)."""
        cache_key = f"eth_prices:{int(start)}:{int(end)}:{interval}"
        cached = await self._cache.get(cache_key)
        if cached is not None:
            data = json.loads(cached)
            return [(float(ts), float(p)) for ts, p in data]

        # Coinbase Exchange API: /products/<id>/candles
        # Returns: [[time, low, high, open, close, volume], ...] (descending by time)
        # granularity in seconds: 60, 300, 900, 3600, 21600, 86400
        url = f"{COINBASE_BASE}/products/{product_id}/candles"
        params = {"start": int(start), "end": int(end), "granularity": interval}

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.get(url, params=params)
            resp.raise_for_status()
            candles = resp.json()

        # Sort ascending by timestamp; use close price
        records = sorted(
            [(float(c[0]), float(c[4])) for c in candles],
            key=lambda r: r[0],
        )
        await self._cache.set(cache_key, json.dumps(records))
        return records
