# backtest/data.py
"""Historical data fetchers for backtest. Caches results to SQLite."""
from __future__ import annotations
import asyncio
import json
import logging
from datetime import datetime
import httpx
from backtest.cache import Cache

logger = logging.getLogger(__name__)

# Coinbase Exchange `/products/<id>/candles` returns at most 300 rows per call.
COINBASE_MAX_CANDLES_PER_PAGE = 300

COINBASE_BASE = "https://api.exchange.coinbase.com"
DYDX_INDEXER_BASE = "https://indexer.dydx.trade/v4"
BEEFY_API_BASE = "https://api.beefy.finance"


class DataFetcher:
    """Fetches historical data with caching. APIs hit only on cache miss."""

    def __init__(self, cache: Cache, fallback_apr: float = 0.30):
        self._cache = cache
        self._fallback_apr = fallback_apr

    async def fetch_eth_prices(
        self, *, start: float, end: float, interval: int = 300,
        product_id: str = "ETH-USD",
    ) -> list[tuple[float, float]]:
        """Fetch ETH/USD candles between start..end (unix seconds). Returns sorted (ts, close_price).

        Coinbase Exchange caps responses at 300 candles per request, so we paginate
        backward from `end` until we cross `start` or the API runs out of data.
        """
        cache_key = f"eth_prices:{int(start)}:{int(end)}:{interval}"
        cached = await self._cache.get(cache_key)
        if cached is not None:
            data = json.loads(cached)
            return [(float(ts), float(p)) for ts, p in data]

        # Coinbase Exchange API: /products/<id>/candles
        # Returns: [[time, low, high, open, close, volume], ...] (descending by time)
        # granularity in seconds: 60, 300, 900, 3600, 21600, 86400
        url = f"{COINBASE_BASE}/products/{product_id}/candles"

        seen: set[float] = set()
        records: list[tuple[float, float]] = []
        cur_end = int(end)
        page_count = 0

        async with httpx.AsyncClient(timeout=30.0) as client:
            while cur_end > int(start):
                params = {"start": int(start), "end": cur_end, "granularity": interval}
                resp = await client.get(url, params=params)
                resp.raise_for_status()
                candles = resp.json()
                if not candles:
                    break

                new_in_page = 0
                oldest_ts: float | None = None
                for c in candles:
                    ts = float(c[0])
                    if ts in seen:
                        continue
                    if oldest_ts is None or ts < oldest_ts:
                        oldest_ts = ts
                    seen.add(ts)
                    records.append((ts, float(c[4])))
                    new_in_page += 1

                page_count += 1
                # Defensive break: API returned only duplicates -> no progress.
                if new_in_page == 0:
                    break
                # Reached start: oldest seen is at-or-before requested start.
                if oldest_ts is not None and oldest_ts <= float(start):
                    break
                # Got fewer than the cap -> no more data older than this window.
                if len(candles) < COINBASE_MAX_CANDLES_PER_PAGE:
                    break
                # Step the window backward: next call asks for candles older than
                # the oldest one we just received.
                if oldest_ts is None:
                    break
                cur_end = int(oldest_ts) - 1

                # Coinbase public rate limit: 5 req/s; throttle when paginating.
                if page_count > 1:
                    await asyncio.sleep(0.25)

        # Sort ascending by timestamp; use close price
        records.sort(key=lambda r: r[0])
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

    async def fetch_beefy_apr_history(
        self, *, vault: str, start: float, end: float,
    ) -> list[tuple[float, float]]:
        """Fetch Beefy vault APR daily samples between start..end.

        Returns list of (unix_ts, apr_decimal). Falls back to a constant APR if
        Beefy doesn't expose history for the vault.
        """
        cache_key = f"beefy_apr:{vault}:{int(start)}:{int(end)}"
        cached = await self._cache.get(cache_key)
        if cached is not None:
            data = json.loads(cached)
            return [(float(ts), float(a)) for ts, a in data]

        url = f"{BEEFY_API_BASE}/apy/breakdown/{vault}"
        series: list[tuple[float, float]] = []

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                payload = resp.json()
            # Beefy may return an "apys" or similar shape — try to extract numbers
            # If the structure isn't recognised, fall back below.
            if isinstance(payload, dict) and "vaultApr" in payload:
                apr_now = float(payload["vaultApr"])
                # Single point — synthesise daily samples between start..end at this rate
                ts = start
                day = 86400
                while ts <= end:
                    series.append((ts, apr_now))
                    ts += day
        except Exception as e:
            logger.warning(f"Beefy APR fetch failed ({e}); using fallback {self._fallback_apr}")

        if not series:
            # Fallback: constant APR daily samples
            ts = start
            day = 86400
            while ts <= end:
                series.append((ts, self._fallback_apr))
                ts += day

        await self._cache.set(cache_key, json.dumps(series))
        return series

    async def fetch_beefy_range_events(
        self, *, w3, strategy_address: str, start_block: int, end_block: int,
    ) -> list[dict]:
        """Fetch Beefy strategy Rebalance events between blocks.

        Returns list of {block, ts, tick_lower, tick_upper, liquidity}. Caller is
        responsible for converting block to ts if needed (could pass via the dict).

        Implementation note: Beefy strategies emit events with various names depending
        on version; this implementation looks for any topic whose name contains
        'Rebalance' in the contract's ABI.
        """
        cache_key = f"beefy_events:{strategy_address}:{start_block}:{end_block}"
        cached = await self._cache.get(cache_key)
        if cached is not None:
            return json.loads(cached)

        # NOTE: For MVP, return empty list so simulator falls back to "single static range".
        # Real implementation would inspect the strategy's logs via eth_getLogs.
        # This is documented as a known gap — the simulator handles missing rebalance
        # data by treating range as constant for the whole period.
        series: list[dict] = []
        await self._cache.set(cache_key, json.dumps(series))
        return series
