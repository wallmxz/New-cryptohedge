# backtest/data.py
"""Historical data fetchers for backtest. Caches results to SQLite."""
from __future__ import annotations
import json
import logging
from datetime import datetime
import httpx
from backtest.cache import Cache

logger = logging.getLogger(__name__)

COINBASE_BASE = "https://api.exchange.coinbase.com"
DYDX_INDEXER_BASE = "https://indexer.dydx.trade/v4"


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

    async def fetch_dydx_funding(
        self, *, symbol: str, start: float, end: float,
    ) -> list[tuple[float, float]]:
        """Fetch dYdX historical funding rates for `symbol`.

        Returns sorted list of (unix_ts, rate_per_period). Rate is signed:
        positive = longs pay shorts. Period is hourly on dYdX v4.
        """
        cache_key = f"dydx_funding:{symbol}:{int(start)}:{int(end)}"
        cached = await self._cache.get(cache_key)
        if cached is not None:
            data = json.loads(cached)
            return [(float(ts), float(rate)) for ts, rate in data]

        url = f"{DYDX_INDEXER_BASE}/historicalFunding/{symbol}"
        # Indexer paginates with effectiveBeforeOrAt; loop until covered
        records: list[tuple[float, float]] = []
        seen: set[float] = set()
        cursor_iso = datetime.utcfromtimestamp(end).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        async with httpx.AsyncClient(timeout=30.0) as client:
            while True:
                params = {"effectiveBeforeOrAt": cursor_iso, "limit": 100}
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                payload = resp.json()
                page = payload.get("historicalFunding", [])
                if not page:
                    break
                new_in_page = 0
                for item in page:
                    ts = datetime.strptime(
                        item["effectiveAt"].replace("Z", ""), "%Y-%m-%dT%H:%M:%S"
                    ).timestamp()
                    if ts < start:
                        break
                    if ts in seen:
                        continue
                    seen.add(ts)
                    records.append((ts, float(item["rate"])))
                    new_in_page += 1
                if new_in_page == 0:
                    break
                # Advance cursor to oldest seen ts minus 1 (paginate older)
                last_ts = min(seen)
                if last_ts <= start:
                    break
                cursor_iso = datetime.utcfromtimestamp(last_ts - 1).strftime("%Y-%m-%dT%H:%M:%S.000Z")

        records.sort(key=lambda r: r[0])
        await self._cache.set(cache_key, json.dumps(records))
        return records
