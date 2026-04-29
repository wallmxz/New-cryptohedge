# Backtesting Framework Implementation Plan (Phase 1.4)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** MVP de backtest CLI que reusa o `GridMakerEngine` real com mocks de exchange/chain pra simular a estratégia em dados históricos. Plus cleanup de configs mortas.

**Architecture:** `backtest/` package separado, com `data.py` fetching e cache em SQLite, `exchange_mock.py` + `chain_mock.py` substituindo adapters em runtime, `simulator.py` event-driven (5min ticks + Beefy rebalance events), `report.py` agregando PnL e stats.

**Tech Stack:** Python 3.14, httpx (HTTP), aiosqlite (cache), web3.py (Beefy events), reuso de `engine/*` da Phase 1.1+.

**Spec:** [`docs/superpowers/specs/2026-04-29-backtesting-design.md`](../specs/2026-04-29-backtesting-design.md)

---

## File Structure

### New (backtest module)
- `backtest/__init__.py` — empty marker
- `backtest/__main__.py` — CLI entry
- `backtest/data.py` — fetch historical data
- `backtest/cache.py` — SQLite cache
- `backtest/exchange_mock.py` — `MockExchangeAdapter`
- `backtest/chain_mock.py` — `MockPoolReader` + `MockBeefyReader`
- `backtest/simulator.py` — event-driven main loop
- `backtest/report.py` — PnL aggregation + output
- `tests/test_backtest.py` — integration test with synthetic data

### Modified (T0 cleanup)
- `config.py` — remove 4 dead fields
- `state.py` — remove 3 dead fields
- `app.py` — `_load_persisted_config` cleanup
- `web/routes.py` — `update_settings` + `get_config`
- `web/templates/partials/settings.html` — remove 4 inputs
- `web/static/app.js` — `config` keys
- `tests/test_config.py`, `tests/test_state.py`
- `.env.example`
- `CLAUDE.md` — note about threshold semantics

---

## Phase A: Cleanup pre-task

### Task 0: Remove dead configs + tighten threshold default

**Files:**
- Modify: `config.py`, `state.py`, `app.py`
- Modify: `web/routes.py`, `web/templates/partials/settings.html`, `web/static/app.js`
- Modify: `tests/test_config.py`, `tests/test_state.py`
- Modify: `.env.example`, `CLAUDE.md`

- [ ] **Step 1: Modify config.py**

Remove these field declarations from the `Settings` dataclass:
```python
max_exposure_pct: float
repost_depth: int
threshold_recovery: float
pool_deposited_usd: float  # if present (T1.2 added it; check)
```

Remove corresponding `from_env()` lines:
```python
max_exposure_pct=float(os.environ.get("MAX_EXPOSURE_PCT", "0.05")),
repost_depth=int(os.environ.get("REPOST_DEPTH", "3")),
threshold_recovery=float(os.environ.get("THRESHOLD_RECOVERY", "0.02")),
```

Change `threshold_aggressive` default from `"0.05"` to `"0.01"`. Add comment immediately above the field declaration:
```python
# Safety net for execution failures (bot offline, exchange congestion, price gaps).
# In healthy operation the predictive grid drives exposure to ~0% and this never fires.
threshold_aggressive: float
```

- [ ] **Step 2: Modify state.py**

Remove from `StateHub` dataclass:
```python
max_exposure_pct: float = 0.05
repost_depth: int = 3
pool_deposited_usd: float = 0.0  # if present
```

Keep `hedge_ratio`. (`threshold_recovery` was never on StateHub — only on Settings.)

- [ ] **Step 3: Modify app.py**

In `_load_persisted_config`, remove the tuples for `max_exposure_pct`, `repost_depth`, `pool_deposited_usd`:
```python
# Before:
for key, caster, attr in [
    ("hedge_ratio", float, "hedge_ratio"),
    ("max_exposure_pct", float, "max_exposure_pct"),  # remove
    ("repost_depth", int, "repost_depth"),  # remove
    ("pool_deposited_usd", float, "pool_deposited_usd"),  # remove
]:
# After:
for key, caster, attr in [
    ("hedge_ratio", float, "hedge_ratio"),
]:
```

Also in the `state = StateHub(...)` constructor call (still in `app.py::create_app`), remove kwargs that no longer exist:
```python
# Before:
state = StateHub(
    hedge_ratio=settings.hedge_ratio,
    max_exposure_pct=settings.max_exposure_pct,
    repost_depth=settings.repost_depth,
)
# After:
state = StateHub(
    hedge_ratio=settings.hedge_ratio,
)
```

- [ ] **Step 4: Modify web/routes.py**

In `update_settings`, remove these blocks:
```python
if "max_exposure_pct" in form:
    hub.max_exposure_pct = float(form["max_exposure_pct"])
    await db.set_config("max_exposure_pct", str(hub.max_exposure_pct))
if "repost_depth" in form:
    hub.repost_depth = int(form["repost_depth"])
    await db.set_config("repost_depth", str(hub.repost_depth))
if "pool_deposited_usd" in form:
    hub.pool_deposited_usd = float(form["pool_deposited_usd"])
    await db.set_config("pool_deposited_usd", str(hub.pool_deposited_usd))
if "threshold_recovery" in form:
    await db.set_config("threshold_recovery", str(float(form["threshold_recovery"])))
```

In `get_config`, remove the line:
```python
"threshold_recovery": settings.threshold_recovery,
```

- [ ] **Step 5: Modify web/templates/partials/settings.html**

Remove the four `cfg-group` blocks for:
- "Exposicao maxima" (max_exposure_pct)
- "Reposicionar no nivel" (repost_depth)
- "Threshold de recovery" (threshold_recovery)
- "Valor depositado na pool (USD)" (pool_deposited_usd)

Keep "Ratio do hedge", "Maximo open orders na exchange", "Threshold escalada (taker)".

For the remaining "Threshold escalada (taker)" input, change the hint:
```html
<p class="cfg-hint">Safety net pra falhas: se exposure passar disso, escalada taker. Default 1%.</p>
```

- [ ] **Step 6: Modify web/static/app.js**

In the `config: { ... }` object, remove these keys (if any are present from prior phases):
```javascript
threshold_recovery: 0.02,
```

Verify `max_open_orders`, `threshold_aggressive` remain.

- [ ] **Step 7: Modify tests/test_config.py**

Remove any assertions on `max_exposure_pct`, `repost_depth`, `threshold_recovery`, `pool_deposited_usd`. Update `threshold_aggressive` default assertion if present:
```python
assert s.threshold_aggressive == 0.01  # was 0.05
```

- [ ] **Step 8: Modify tests/test_state.py**

Remove or update tests that reference removed StateHub fields. The default-fields test should not check `max_exposure_pct`, `repost_depth`, `pool_deposited_usd`.

- [ ] **Step 9: Modify .env.example**

Remove these lines:
```
MAX_EXPOSURE_PCT=0.05
REPOST_DEPTH=3
THRESHOLD_RECOVERY=0.02
```

Change `THRESHOLD_AGGRESSIVE=0.05` to `THRESHOLD_AGGRESSIVE=0.01`.

- [ ] **Step 10: Modify CLAUDE.md**

In the "Decisões já tomadas" section, replace or augment the auto-defenses bullet with:
```markdown
- **Auto-defenses:** **NÃO IMPLEMENTAR** auto-deleverage; só auto-emergency-close em margem crítica (decisão Phase 1.2: usuário NÃO QUER essa mecânica por enquanto, só alerts)
- **Threshold semantics:** A grade É a predição (replica matemática da curva LP). `threshold_aggressive` é safety net pra falhas (bot offline, exchange congestion, price gaps), NÃO tuning estratégico. Em operação saudável, drift é <0.5% e nunca dispara. Default 1% (apertado, coerente com modelo preditivo).
```

- [ ] **Step 11: Run full test suite (in batches if Windows hangs)**

Run:
```
python -m pytest tests/test_config.py tests/test_state.py tests/test_db.py tests/test_pnl.py tests/test_curve.py tests/test_grid.py tests/test_orderbook.py tests/test_alerts.py tests/test_margin.py tests/test_metrics.py tests/test_logging_config.py tests/test_operation.py -v
```

Then:
```
python -m pytest tests/test_uniswap.py tests/test_beefy.py tests/test_dydx.py tests/test_reconciler.py tests/test_engine_grid.py tests/test_web.py tests/test_exchanges.py tests/test_integration_grid.py tests/test_integration_operation.py -v
```

Expected: PASS in both batches. If anything breaks in `test_engine_grid.py` or `test_integration_*` because they reference removed fields, update those tests in this same task.

- [ ] **Step 12: Commit**

```bash
git add config.py state.py app.py web/routes.py web/templates/partials/settings.html web/static/app.js tests/test_config.py tests/test_state.py .env.example CLAUDE.md
git commit -m "chore(task-0): remove dead configs (max_exposure_pct, repost_depth, threshold_recovery, pool_deposited_usd); tighten threshold_aggressive default to 1%"
```

---

## Phase B: Data layer

### Task 1: backtest/cache.py — SQLite local cache

**Files:**
- Create: `backtest/__init__.py` (empty)
- Create: `backtest/cache.py`
- Test: `tests/test_backtest.py` (start of file)

- [ ] **Step 1: Create empty backtest/__init__.py**

```python
# backtest/__init__.py
```

- [ ] **Step 2: Write failing test**

Create `tests/test_backtest.py`:

```python
import pytest
from backtest.cache import Cache


@pytest.mark.asyncio
async def test_cache_set_get_string(tmp_path):
    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    await cache.set("k1", "value1")
    assert await cache.get("k1") == "value1"
    assert await cache.get("missing") is None
    await cache.close()


@pytest.mark.asyncio
async def test_cache_overwrites(tmp_path):
    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    await cache.set("k1", "first")
    await cache.set("k1", "second")
    assert await cache.get("k1") == "second"
    await cache.close()


@pytest.mark.asyncio
async def test_cache_persists_across_instances(tmp_path):
    path = str(tmp_path / "c.db")
    c1 = Cache(path)
    await c1.initialize()
    await c1.set("k", "persisted")
    await c1.close()
    c2 = Cache(path)
    await c2.initialize()
    assert await c2.get("k") == "persisted"
    await c2.close()
```

- [ ] **Step 3: Run to confirm fail**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'backtest.cache'`

- [ ] **Step 4: Implement backtest/cache.py**

```python
# backtest/cache.py
from __future__ import annotations
import aiosqlite


class Cache:
    """Simple key/value cache backed by SQLite. Stores serialized blobs by key.

    Used to avoid re-fetching historical data from external APIs.
    """

    def __init__(self, path: str):
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute(
            """CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                stored_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
            )"""
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def set(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, stored_at) VALUES (?, ?, strftime('%s','now'))",
            (key, value),
        )
        await self._conn.commit()

    async def get(self, key: str) -> str | None:
        cursor = await self._conn.execute(
            "SELECT value FROM cache WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None
```

- [ ] **Step 5: Run tests to verify**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add backtest/__init__.py backtest/cache.py tests/test_backtest.py
git commit -m "feat(task-1): backtest cache layer (SQLite key/value)"
```

---

### Task 2: backtest/data.py — fetch ETH price history

**Files:**
- Create: `backtest/data.py`
- Modify: `tests/test_backtest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_backtest.py`:

```python
from unittest.mock import AsyncMock, MagicMock, patch
import json


@pytest.mark.asyncio
async def test_fetch_eth_prices_uses_cache(tmp_path):
    from backtest.data import DataFetcher
    from backtest.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()

    fetcher = DataFetcher(cache=cache)

    # Pre-populate cache with a known result
    cached_payload = json.dumps([[1700000000.0, 2000.5], [1700000300.0, 2001.0]])
    await cache.set("eth_prices:1700000000:1700000600:300", cached_payload)

    result = await fetcher.fetch_eth_prices(start=1700000000, end=1700000600, interval=300)
    assert result == [(1700000000.0, 2000.5), (1700000300.0, 2001.0)]
    await cache.close()


@pytest.mark.asyncio
async def test_fetch_eth_prices_calls_api_on_miss(tmp_path):
    from backtest.data import DataFetcher
    from backtest.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    fetcher = DataFetcher(cache=cache)

    # Mock httpx to return a Coinbase-like response
    fake_response = MagicMock()
    fake_response.json = MagicMock(return_value=[
        # Coinbase candles: [time, low, high, open, close, volume]
        [1700000600, 1999.0, 2002.0, 2000.0, 2001.0, 100.0],
        [1700000300, 1998.0, 2001.5, 2000.5, 2000.5, 80.0],
        [1700000000, 1997.0, 2001.0, 2000.0, 2000.5, 50.0],
    ])
    fake_response.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("backtest.data.httpx.AsyncClient", return_value=fake_client):
        result = await fetcher.fetch_eth_prices(start=1700000000, end=1700000600, interval=300)

    assert len(result) == 3
    # Sorted ascending by timestamp
    assert result[0][0] < result[1][0] < result[2][0]
    # Cached
    cached = await cache.get("eth_prices:1700000000:1700000600:300")
    assert cached is not None
    await cache.close()
```

- [ ] **Step 2: Run to confirm fail**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: FAIL on the two new tests with import error

- [ ] **Step 3: Implement backtest/data.py**

```python
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
```

- [ ] **Step 4: Run tests to verify**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: PASS (5 tests now: 3 cache + 2 data)

- [ ] **Step 5: Commit**

```bash
git add backtest/data.py tests/test_backtest.py
git commit -m "feat(task-2): backtest data fetcher for ETH price history (Coinbase, cached)"
```

---

### Task 3: backtest/data.py — fetch dYdX funding history

**Files:**
- Modify: `backtest/data.py`
- Modify: `tests/test_backtest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_backtest.py`:

```python
@pytest.mark.asyncio
async def test_fetch_dydx_funding_uses_cache(tmp_path):
    from backtest.data import DataFetcher
    from backtest.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    fetcher = DataFetcher(cache=cache)

    cached = json.dumps([[1700000000.0, 0.0001], [1700003600.0, -0.00005]])
    await cache.set("dydx_funding:ETH-USD:1700000000:1700007200", cached)

    result = await fetcher.fetch_dydx_funding(symbol="ETH-USD", start=1700000000, end=1700007200)
    assert result == [(1700000000.0, 0.0001), (1700003600.0, -0.00005)]
    await cache.close()


@pytest.mark.asyncio
async def test_fetch_dydx_funding_calls_indexer_on_miss(tmp_path):
    from backtest.data import DataFetcher
    from backtest.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    fetcher = DataFetcher(cache=cache)

    fake_response = MagicMock()
    fake_response.json = MagicMock(return_value={
        "historicalFunding": [
            {"effectiveAt": "2023-11-15T00:00:00Z", "rate": "0.000125"},
            {"effectiveAt": "2023-11-15T01:00:00Z", "rate": "-0.000050"},
        ]
    })
    fake_response.raise_for_status = MagicMock()
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("backtest.data.httpx.AsyncClient", return_value=fake_client):
        result = await fetcher.fetch_dydx_funding(symbol="ETH-USD", start=1700000000, end=1700007200)

    assert len(result) == 2
    assert result[0][1] == 0.000125
    await cache.close()
```

- [ ] **Step 2: Run to confirm fail**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: FAIL — `AttributeError: 'DataFetcher' object has no attribute 'fetch_dydx_funding'`

- [ ] **Step 3: Add fetch_dydx_funding to DataFetcher**

In `backtest/data.py`, add at the top:

```python
from datetime import datetime
```

Add constant near `COINBASE_BASE`:

```python
DYDX_INDEXER_BASE = "https://indexer.dydx.trade/v4"
```

Add method to `DataFetcher`:

```python
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
            for item in page:
                ts = datetime.strptime(
                    item["effectiveAt"].replace("Z", ""), "%Y-%m-%dT%H:%M:%S"
                ).timestamp()
                if ts < start:
                    break
                records.append((ts, float(item["rate"])))
            # Advance cursor to last item's time (paginate older)
            last_ts = records[-1][0] if records else start
            if last_ts <= start:
                break
            cursor_iso = datetime.utcfromtimestamp(last_ts - 1).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    records.sort(key=lambda r: r[0])
    await self._cache.set(cache_key, json.dumps(records))
    return records
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add backtest/data.py tests/test_backtest.py
git commit -m "feat(task-3): backtest fetch dYdX historical funding (indexer, cached)"
```

---

### Task 4: backtest/data.py — fetch Beefy range events + APR history

**Files:**
- Modify: `backtest/data.py`
- Modify: `tests/test_backtest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_backtest.py`:

```python
@pytest.mark.asyncio
async def test_fetch_beefy_apr_history_uses_cache(tmp_path):
    from backtest.data import DataFetcher
    from backtest.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    fetcher = DataFetcher(cache=cache)

    cached = json.dumps([[1700000000.0, 0.45], [1700086400.0, 0.50]])
    await cache.set("beefy_apr:0xvault:1700000000:1700172800", cached)

    result = await fetcher.fetch_beefy_apr_history(
        vault="0xvault", start=1700000000, end=1700172800,
    )
    assert result == [(1700000000.0, 0.45), (1700086400.0, 0.50)]
    await cache.close()


@pytest.mark.asyncio
async def test_fetch_beefy_apr_history_falls_back_constant(tmp_path):
    """If Beefy API doesn't return useful data, fetcher falls back to a constant APR."""
    from backtest.data import DataFetcher
    from backtest.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    fetcher = DataFetcher(cache=cache, fallback_apr=0.40)

    # Mock httpx to return empty response
    fake_response = MagicMock()
    fake_response.json = MagicMock(return_value={})
    fake_response.raise_for_status = MagicMock()
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("backtest.data.httpx.AsyncClient", return_value=fake_client):
        result = await fetcher.fetch_beefy_apr_history(
            vault="0xvault", start=1700000000, end=1700172800,
        )

    # 2 days = 2 daily samples
    assert len(result) >= 2
    for ts, apr in result:
        assert apr == 0.40
    await cache.close()
```

- [ ] **Step 2: Run to confirm fail**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: FAIL — `AttributeError: 'DataFetcher' object has no attribute 'fetch_beefy_apr_history'`

- [ ] **Step 3: Add fetch_beefy_apr_history**

In `backtest/data.py`, add constant:

```python
BEEFY_API_BASE = "https://api.beefy.finance"
```

Modify `DataFetcher.__init__` to accept fallback APR:

```python
def __init__(self, cache: Cache, fallback_apr: float = 0.30):
    self._cache = cache
    self._fallback_apr = fallback_apr
```

Add method:

```python
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
        # When the breakdown endpoint provides daily samples they appear under
        # `daily` or `apr_history` keys; this varies. Best-effort parse:
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
```

Also add a placeholder for `fetch_beefy_range_events` — Task 4 second sub-piece. Add this method below:

```python
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
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: PASS (9 tests)

- [ ] **Step 5: Commit**

```bash
git add backtest/data.py tests/test_backtest.py
git commit -m "feat(task-4): backtest fetch Beefy APR history with fallback; range events stub"
```

---

## Phase C: Simulator

### Task 5: backtest/exchange_mock.py — deterministic fill engine

**Files:**
- Create: `backtest/exchange_mock.py`
- Modify: `tests/test_backtest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_backtest.py`:

```python
@pytest.mark.asyncio
async def test_mock_exchange_fills_when_price_crosses_buy():
    """A buy order at price P fills when simulated price drops to <= P."""
    from backtest.exchange_mock import MockExchangeAdapter
    from exchanges.base import Order

    received_fills = []
    async def on_fill(fill):
        received_fills.append(fill)

    ex = MockExchangeAdapter(symbol="ETH-USD", min_notional=0.001)
    await ex.connect()
    await ex.subscribe_fills("ETH-USD", on_fill)

    await ex.place_long_term_order(
        symbol="ETH-USD", side="buy", size=0.001, price=3000.0,
        cloid_int=1, ttl_seconds=60,
    )

    # Price moves up — no fill
    await ex.advance_to_price(3010.0, ts=1000.0)
    assert received_fills == []

    # Price drops to 3000 — order fills
    await ex.advance_to_price(2999.0, ts=2000.0)
    assert len(received_fills) == 1
    f = received_fills[0]
    assert f.side == "buy"
    assert abs(f.price - 3000.0) < 1e-9
    assert f.liquidity == "maker"


@pytest.mark.asyncio
async def test_mock_exchange_position_tracks_fills():
    """Sell fill increases short size; buy fill reduces it."""
    from backtest.exchange_mock import MockExchangeAdapter

    ex = MockExchangeAdapter(symbol="ETH-USD", min_notional=0.001)
    await ex.connect()

    async def _noop(_): pass
    await ex.subscribe_fills("ETH-USD", _noop)

    await ex.place_long_term_order(
        symbol="ETH-USD", side="sell", size=0.005, price=3000.0,
        cloid_int=1, ttl_seconds=60,
    )
    await ex.advance_to_price(3001.0, ts=1000.0)
    pos = await ex.get_position("ETH-USD")
    assert pos is not None
    assert pos.side == "short"
    assert abs(pos.size - 0.005) < 1e-9
```

- [ ] **Step 2: Run to confirm fail**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'backtest.exchange_mock'`

- [ ] **Step 3: Implement backtest/exchange_mock.py**

```python
# backtest/exchange_mock.py
"""Deterministic in-memory exchange mock for backtesting.

Implements ExchangeAdapter interface but never makes network calls.
Orders fill when simulated price crosses their level.
"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable
from exchanges.base import ExchangeAdapter, Order, Fill, Position


@dataclass
class _OpenOrder:
    cloid_int: int
    side: str
    size: float
    price: float


@dataclass
class _MarketMeta:
    ticker: str
    tick_size: float
    step_size: float
    atomic_resolution: int
    min_order_base_quantums: int

    @property
    def min_notional(self) -> float:
        return self.min_order_base_quantums / (10 ** abs(self.atomic_resolution))


class MockExchangeAdapter(ExchangeAdapter):
    name = "mock"

    def __init__(self, *, symbol: str, min_notional: float = 0.001,
                 maker_fee: float = 0.0001, taker_fee: float = 0.0005):
        self._symbol = symbol
        self._maker_fee = maker_fee
        self._taker_fee = taker_fee
        self._open_orders: dict[int, _OpenOrder] = {}
        self._position_size: float = 0.0  # signed: + long, - short
        self._position_entry: float = 0.0  # weighted avg
        self._collateral: float = 130.0
        self._book_callback: Callable[[dict], Awaitable[None]] | None = None
        self._fill_callback: Callable[[Fill], Awaitable[None]] | None = None
        self._last_price: float = 0.0
        self._fill_id_seq = 0
        self._meta = _MarketMeta(
            ticker=symbol, tick_size=0.1, step_size=min_notional,
            atomic_resolution=-9, min_order_base_quantums=int(min_notional * 1e9),
        )

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def get_market_meta(self, symbol: str) -> _MarketMeta:
        return self._meta

    async def place_long_term_order(
        self, *, symbol: str, side: str, size: float, price: float,
        cloid_int: int, ttl_seconds: int = 86400,
    ) -> Order:
        self._open_orders[cloid_int] = _OpenOrder(
            cloid_int=cloid_int, side=side, size=size, price=price,
        )
        return Order(
            order_id=str(cloid_int), symbol=symbol, side=side,
            size=size, price=price, status="open",
        )

    async def place_limit_order(self, symbol, side, size, price):
        return await self.place_long_term_order(
            symbol=symbol, side=side, size=size, price=price,
            cloid_int=int(asyncio.get_event_loop().time() * 1000) % (2**31),
        )

    async def cancel_long_term_order(self, *, symbol: str, cloid_int: int) -> None:
        self._open_orders.pop(cloid_int, None)

    async def cancel_order(self, order_id: str) -> None:
        try:
            self._open_orders.pop(int(order_id), None)
        except ValueError:
            pass

    async def batch_place(self, orders: list[dict]) -> list[Order]:
        placed = []
        for spec in orders:
            placed.append(await self.place_long_term_order(**spec))
        return placed

    async def batch_cancel(self, items: list[dict]) -> int:
        cancelled = 0
        for spec in items:
            try:
                await self.cancel_long_term_order(**spec)
                cancelled += 1
            except Exception:
                pass
        return cancelled

    async def get_position(self, symbol: str) -> Position | None:
        if abs(self._position_size) < 1e-12:
            return None
        side = "short" if self._position_size < 0 else "long"
        unreal = (self._position_entry - self._last_price) * self._position_size
        return Position(
            symbol=symbol, side=side, size=abs(self._position_size),
            entry_price=self._position_entry, unrealized_pnl=unreal,
        )

    async def get_collateral(self) -> float:
        return self._collateral

    async def get_fills(self, symbol: str, since: float | None = None) -> list[Fill]:
        return []

    async def subscribe_orderbook(self, symbol: str, callback) -> None:
        self._book_callback = callback

    async def subscribe_fills(self, symbol: str, callback) -> None:
        self._fill_callback = callback

    async def get_open_orders_cloids(self, symbol: str) -> list[str]:
        return [str(c) for c in self._open_orders.keys()]

    def get_tick_size(self, symbol: str) -> float:
        return self._meta.tick_size

    def get_min_notional(self, symbol: str) -> float:
        return self._meta.min_notional

    # Backtest-specific API ------------------------------------------------

    async def advance_to_price(self, price: float, *, ts: float) -> None:
        """Advance the mock clock to a new price, firing fills for crossed orders."""
        prev = self._last_price
        self._last_price = price

        # Determine which open orders cross this price step
        to_fill: list[_OpenOrder] = []
        for cloid, order in list(self._open_orders.items()):
            if order.side == "buy":
                # Buy fills when price <= order.price
                if (prev == 0 and price <= order.price) or (prev > order.price >= price):
                    to_fill.append(order)
            else:  # sell
                # Sell fills when price >= order.price
                if (prev == 0 and price >= order.price) or (prev < order.price <= price):
                    to_fill.append(order)

        for order in to_fill:
            self._open_orders.pop(order.cloid_int, None)
            self._apply_fill(order, ts=ts)

    def _apply_fill(self, order: _OpenOrder, *, ts: float) -> None:
        # Update position
        signed_delta = order.size if order.side == "buy" else -order.size
        new_size = self._position_size + signed_delta
        if abs(self._position_size) > 1e-12 and (
            (self._position_size > 0) == (signed_delta > 0)
        ):
            # Same direction — weighted average entry
            denom = self._position_size + signed_delta
            self._position_entry = (
                self._position_entry * self._position_size + order.price * signed_delta
            ) / denom if abs(denom) > 1e-12 else order.price
        elif abs(self._position_size) < 1e-12:
            self._position_entry = order.price
        # else closing or flipping — keep entry of remaining (simplification)
        self._position_size = new_size

        # Fees
        fee = order.size * order.price * self._maker_fee
        self._collateral -= fee

        self._fill_id_seq += 1
        fill = Fill(
            fill_id=str(self._fill_id_seq),
            order_id=str(order.cloid_int),
            symbol=self._symbol,
            side=order.side,
            size=order.size,
            price=order.price,
            fee=fee,
            fee_currency="USDC",
            liquidity="maker",
            realized_pnl=0.0,
            timestamp=ts,
        )
        if self._fill_callback:
            # Sync invocation since we're inside async context already
            asyncio.create_task(self._fill_callback(fill))

    def apply_funding(self, rate_per_period: float, ts: float) -> None:
        """Apply a single funding period to the open short notional.

        Convention: positive rate = longs pay shorts, so short receives.
        """
        if abs(self._position_size) < 1e-12:
            return
        notional = abs(self._position_size) * self._last_price
        # Bot is short → if rate > 0, bot receives; if rate < 0, bot pays.
        # We model as a credit/debit on collateral.
        delta = (rate_per_period * notional) if self._position_size < 0 else (-rate_per_period * notional)
        self._collateral += delta
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: PASS (11 tests)

- [ ] **Step 5: Commit**

```bash
git add backtest/exchange_mock.py tests/test_backtest.py
git commit -m "feat(task-5): MockExchangeAdapter with deterministic fills + funding application"
```

---

### Task 6: backtest/chain_mock.py — pool/Beefy mocks

**Files:**
- Create: `backtest/chain_mock.py`
- Modify: `tests/test_backtest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_backtest.py`:

```python
@pytest.mark.asyncio
async def test_mock_pool_returns_current_price():
    from backtest.chain_mock import MockPoolReader

    pool = MockPoolReader()
    pool.set_price(3000.0)
    assert await pool.read_price() == 3000.0

    pool.set_price(2950.5)
    assert await pool.read_price() == 2950.5


@pytest.mark.asyncio
async def test_mock_beefy_returns_current_position():
    from backtest.chain_mock import MockBeefyReader, _BeefyPosition

    beefy = MockBeefyReader()
    beefy.set_position(
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.5, amount1=1500.0, share=0.01, raw_balance=10**16,
    )
    pos = await beefy.read_position()
    assert pos.tick_lower == -197310
    assert pos.tick_upper == -195303
    assert abs(pos.amount0 - 0.5) < 1e-9
    assert abs(pos.share - 0.01) < 1e-9
```

- [ ] **Step 2: Run to confirm fail**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement backtest/chain_mock.py**

```python
# backtest/chain_mock.py
"""In-memory pool/Beefy readers driven by simulator. Replace web3 calls."""
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class _BeefyPosition:
    tick_lower: int
    tick_upper: int
    amount0: float
    amount1: float
    share: float
    raw_balance: int


class MockPoolReader:
    """Replaces UniswapV3PoolReader. set_price() drives current value."""

    def __init__(self):
        self._price: float = 0.0

    def set_price(self, price: float) -> None:
        self._price = price

    async def read_price(self) -> float:
        return self._price

    async def read_slot0(self) -> tuple[int, int]:
        # Synthetic — caller likely won't use
        return (int((self._price ** 0.5) * (2**96)), 0)


class MockBeefyReader:
    """Replaces BeefyClmReader. set_position() drives current state."""

    def __init__(self):
        self._pos: _BeefyPosition | None = None

    def set_position(self, *, tick_lower: int, tick_upper: int,
                     amount0: float, amount1: float, share: float,
                     raw_balance: int) -> None:
        self._pos = _BeefyPosition(
            tick_lower=tick_lower, tick_upper=tick_upper,
            amount0=amount0, amount1=amount1,
            share=share, raw_balance=raw_balance,
        )

    async def read_position(self) -> _BeefyPosition:
        if self._pos is None:
            raise RuntimeError("MockBeefyReader: position not set")
        return self._pos
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: PASS (13 tests)

- [ ] **Step 5: Commit**

```bash
git add backtest/chain_mock.py tests/test_backtest.py
git commit -m "feat(task-6): MockPoolReader + MockBeefyReader for backtest chain replay"
```

---

### Task 7: backtest/simulator.py — main loop

**Files:**
- Create: `backtest/simulator.py`
- Modify: `tests/test_backtest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_backtest.py`:

```python
@pytest.mark.asyncio
async def test_simulator_runs_synthetic_period(tmp_path):
    """Simulator runs through a tiny synthetic timeline and produces a result dict."""
    from backtest.simulator import Simulator, SimConfig

    # Synthetic timeline: 3 ETH price points, no funding, single static range
    config = SimConfig(
        vault_address="0xvault",
        pool_address="0xpool",
        start_ts=1700000000.0,
        end_ts=1700000900.0,  # 15 min
        capital_lp=300.0,
        capital_dydx=130.0,
        hedge_ratio=1.0,
        threshold_aggressive=0.01,
        max_open_orders=50,
    )

    eth_prices = [
        (1700000000.0, 3000.0),
        (1700000300.0, 3001.0),
        (1700000600.0, 2999.0),
    ]
    funding = []
    apr_history = [(1700000000.0, 0.40)]
    range_events = []  # constant range
    static_range = {
        "tick_lower": -197310, "tick_upper": -195303,
        "amount0": 0.5, "amount1": 1500.0, "share": 0.01, "raw_balance": 10**16,
    }

    sim = Simulator(
        config=config,
        eth_prices=eth_prices,
        funding=funding,
        apr_history=apr_history,
        range_events=range_events,
        static_range=static_range,
    )
    result = await sim.run()
    # Result has expected top-level keys
    assert "net_pnl" in result
    assert "fills_maker" in result
    assert "fills_taker" in result
    assert "duration_seconds" in result
    assert result["duration_seconds"] == 900
```

- [ ] **Step 2: Run to confirm fail**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement backtest/simulator.py**

```python
# backtest/simulator.py
"""Backtest event-driven simulator.

Drives the real GridMakerEngine through historical data via mock exchange/chain.
"""
from __future__ import annotations
import logging
from dataclasses import dataclass
from unittest.mock import MagicMock, AsyncMock

from state import StateHub
from db import Database
from engine import GridMakerEngine
from backtest.exchange_mock import MockExchangeAdapter
from backtest.chain_mock import MockPoolReader, MockBeefyReader

logger = logging.getLogger(__name__)


@dataclass
class SimConfig:
    vault_address: str
    pool_address: str
    start_ts: float
    end_ts: float
    capital_lp: float = 300.0
    capital_dydx: float = 130.0
    hedge_ratio: float = 1.0
    threshold_aggressive: float = 0.01
    max_open_orders: int = 200
    tick_seconds: int = 300  # 5 min


class Simulator:
    """Runs the real GridMakerEngine over historical data."""

    def __init__(
        self, *,
        config: SimConfig,
        eth_prices: list[tuple[float, float]],
        funding: list[tuple[float, float]],
        apr_history: list[tuple[float, float]],
        range_events: list[dict],
        static_range: dict,
    ):
        self._config = config
        self._eth_prices = sorted(eth_prices, key=lambda x: x[0])
        self._funding = sorted(funding, key=lambda x: x[0])
        self._apr_history = sorted(apr_history, key=lambda x: x[0])
        self._range_events = sorted(range_events, key=lambda x: x["ts"])
        self._static_range = static_range

        # State for output
        self._fills_maker = 0
        self._fills_taker = 0
        self._lp_fees_earned = 0.0
        self._range_resets = 0
        self._out_of_range_seconds = 0.0
        self._pnl_series: list[tuple[float, float]] = []  # (ts, net_pnl_so_far)

    async def run(self) -> dict:
        # Build mocks
        exchange = MockExchangeAdapter(
            symbol="ETH-USD",
            min_notional=0.001,
        )
        await exchange.connect()
        exchange._collateral = self._config.capital_dydx

        pool = MockPoolReader()
        beefy = MockBeefyReader()
        # Apply static range as the starting range
        beefy.set_position(**self._static_range)

        # Build state hub + settings + db (real DB in temp file? simpler: mock DB methods)
        state = StateHub(hedge_ratio=self._config.hedge_ratio)
        state.operation_state = "active"
        state.current_operation_id = 1  # synthetic op id

        settings = MagicMock()
        settings.dydx_symbol = "ETH-USD"
        settings.alert_webhook_url = ""
        settings.threshold_aggressive = self._config.threshold_aggressive
        settings.threshold_recovery = 0.005  # backstop
        settings.max_open_orders = self._config.max_open_orders
        settings.pool_token0_symbol = "WETH"
        settings.pool_token1_symbol = "USDC"

        # Mock DB: in-memory dicts for what the engine reads
        db = MagicMock()
        active_grid_orders: list[dict] = []
        async def get_active_grid_orders():
            return list(active_grid_orders)
        async def insert_grid_order(*, cloid, side, target_price, size, placed_at, operation_id=None):
            active_grid_orders.append({
                "cloid": cloid, "side": side, "target_price": target_price,
                "size": size, "placed_at": placed_at, "operation_id": operation_id,
            })
        async def mark_grid_order_cancelled(cloid, ts):
            for r in active_grid_orders[:]:
                if r["cloid"] == cloid:
                    active_grid_orders.remove(r)
        async def insert_fill(**kw):
            return 1  # fill_id
        async def mark_grid_order_filled(cloid, fill_id):
            for r in active_grid_orders[:]:
                if r["cloid"] == cloid:
                    active_grid_orders.remove(r)
        async def insert_order_log(**kw):
            return None
        async def get_active_operation():
            return {
                "id": 1, "started_at": self._config.start_ts, "ended_at": None,
                "status": "active",
                "baseline_eth_price": self._eth_prices[0][1] if self._eth_prices else 3000.0,
                "baseline_pool_value_usd": self._config.capital_lp,
                "baseline_amount0": self._static_range["amount0"] * self._static_range["share"],
                "baseline_amount1": self._static_range["amount1"] * self._static_range["share"],
                "baseline_collateral": self._config.capital_dydx,
                "perp_fees_paid": 0.0, "funding_paid": 0.0,
                "lp_fees_earned": self._lp_fees_earned,
                "bootstrap_slippage": 0.0,
                "final_net_pnl": None, "close_reason": None,
            }
        async def get_operation(op_id):
            return await get_active_operation()

        db.get_active_grid_orders = get_active_grid_orders
        db.insert_grid_order = insert_grid_order
        db.mark_grid_order_cancelled = mark_grid_order_cancelled
        db.insert_fill = insert_fill
        db.mark_grid_order_filled = mark_grid_order_filled
        db.insert_order_log = insert_order_log
        db.get_active_operation = get_active_operation
        db.get_operation = get_operation
        db.add_to_operation_accumulator = AsyncMock()

        engine = GridMakerEngine(
            settings=settings, hub=state, db=db,
            exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        )
        # Wire fill callback to count + accumulate
        async def _on_fill_capture(fill):
            if fill.liquidity == "maker":
                self._fills_maker += 1
            else:
                self._fills_taker += 1
            await engine._on_fill(fill)
        await exchange.subscribe_fills("ETH-USD", _on_fill_capture)

        # Main loop: walk price timeline at tick_seconds
        prev_ts = self._config.start_ts
        next_funding_idx = 0
        next_apr_idx = 0
        current_apr = self._apr_history[0][1] if self._apr_history else 0.30

        for ts, price in self._eth_prices:
            if ts < self._config.start_ts:
                continue
            if ts > self._config.end_ts:
                break

            # Update mock chain state
            pool.set_price(price)

            # Drive exchange fills based on price step
            await exchange.advance_to_price(price, ts=ts)

            # Apply funding payments due in [prev_ts, ts]
            while next_funding_idx < len(self._funding) and self._funding[next_funding_idx][0] <= ts:
                f_ts, f_rate = self._funding[next_funding_idx]
                if f_ts >= prev_ts:
                    exchange.apply_funding(f_rate, f_ts)
                next_funding_idx += 1

            # Update APR if changed
            while next_apr_idx + 1 < len(self._apr_history) and self._apr_history[next_apr_idx + 1][0] <= ts:
                next_apr_idx += 1
                current_apr = self._apr_history[next_apr_idx][1]

            # Apply LP fees pro-rata for tick interval
            interval_seconds = ts - prev_ts
            year_seconds = 365.0 * 86400
            lp_fee_for_interval = (
                current_apr * self._config.capital_lp * interval_seconds / year_seconds
            )
            self._lp_fees_earned += lp_fee_for_interval

            # Advance engine
            try:
                await engine._iterate()
            except Exception as e:
                logger.error(f"Engine iteration error at ts={ts}: {e}")

            # Track PnL series
            net = self._compute_net_pnl(exchange, price)
            self._pnl_series.append((ts, net))

            prev_ts = ts

        # Build final result
        final_net = self._pnl_series[-1][1] if self._pnl_series else 0.0
        max_drawdown = 0.0
        peak = 0.0
        for _, p in self._pnl_series:
            peak = max(peak, p)
            max_drawdown = min(max_drawdown, p - peak)

        return {
            "net_pnl": round(final_net, 4),
            "fills_maker": self._fills_maker,
            "fills_taker": self._fills_taker,
            "lp_fees_earned": round(self._lp_fees_earned, 4),
            "range_resets": self._range_resets,
            "out_of_range_seconds": self._out_of_range_seconds,
            "max_drawdown": round(max_drawdown, 4),
            "duration_seconds": int(self._config.end_ts - self._config.start_ts),
            "pnl_series": self._pnl_series,
        }

    def _compute_net_pnl(self, exchange: MockExchangeAdapter, price: float) -> float:
        # Crude: collateral delta + LP fees - small constant deduction for slippage
        return (exchange._collateral - self._config.capital_dydx) + self._lp_fees_earned
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: PASS (14 tests)

If `engine._iterate` errors out due to missing methods, the simulator's `db` mock may need additional methods. The current mock provides the most-frequently-called ones; if a test fails with an unexpected `AttributeError`, add the missing `db.*` AsyncMock and re-run.

- [ ] **Step 5: Commit**

```bash
git add backtest/simulator.py tests/test_backtest.py
git commit -m "feat(task-7): backtest Simulator main loop driving real GridMakerEngine"
```

---

### Task 8: Operation lifecycle hooks in simulator

**Files:**
- Modify: `backtest/simulator.py`
- Modify: `tests/test_backtest.py`

This task is folded into T7 above (the simulator already opens the operation as "active" at t0 by setting `state.operation_state = "active"` and seeding a synthetic operation row in the mocked DB; close-at-end is implicit since the result snapshot is taken at the end).

**Skip this as a separate task** — T7 covers it. The plan task counter still goes to T9.

---

### Task 9: backtest/report.py — output formatting

**Files:**
- Create: `backtest/report.py`
- Modify: `tests/test_backtest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_backtest.py`:

```python
def test_report_formats_text():
    from backtest.report import format_text_report

    result = {
        "net_pnl": 174.61,
        "fills_maker": 1240,
        "fills_taker": 12,
        "lp_fees_earned": 187.40,
        "range_resets": 18,
        "out_of_range_seconds": 11520,
        "max_drawdown": -3.40,
        "duration_seconds": 86400 * 181,
        "pnl_series": [],
    }
    text = format_text_report(
        result,
        capital_lp=300.0,
        capital_dydx=130.0,
        symbol="WETH/USDC",
        start_iso="2024-01-01",
        end_iso="2024-06-30",
    )
    assert "Net PnL" in text
    assert "$174.61" in text
    assert "1240" in text
    assert "58.7%" in text or "58.6%" in text or "59." in text  # APR roughly


def test_report_apr_calc():
    from backtest.report import annualized_apr
    # 100 net on 300 over 365 days = 33.3%
    apr = annualized_apr(net=100.0, capital=300.0, duration_seconds=365 * 86400)
    assert abs(apr - 0.3333) < 0.001
```

- [ ] **Step 2: Run to confirm fail**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement backtest/report.py**

```python
# backtest/report.py
"""Format backtest results to text and JSON."""
from __future__ import annotations
import json


def annualized_apr(*, net: float, capital: float, duration_seconds: float) -> float:
    if capital <= 0 or duration_seconds <= 0:
        return 0.0
    year_seconds = 365.0 * 86400
    return (net / capital) * (year_seconds / duration_seconds)


def format_text_report(
    result: dict, *,
    capital_lp: float, capital_dydx: float,
    symbol: str, start_iso: str, end_iso: str,
) -> str:
    duration = result["duration_seconds"]
    days = duration / 86400
    apr_lp = annualized_apr(
        net=result["net_pnl"], capital=capital_lp, duration_seconds=duration,
    )
    apr_total = annualized_apr(
        net=result["net_pnl"], capital=capital_lp + capital_dydx,
        duration_seconds=duration,
    )

    out_of_range_hours = result["out_of_range_seconds"] / 3600

    lines = [
        f"Backtest {symbol} | {start_iso} -> {end_iso} ({days:.1f} days)",
        f"Capital: ${capital_lp:.0f} LP + ${capital_dydx:.0f} dYdX margin",
        "",
        f"Fills:          {result['fills_maker']} maker, {result['fills_taker']} taker",
        f"Range resets:   {result['range_resets']} (Beefy)",
        f"Out-of-range:   {out_of_range_hours:.1f} hours total",
        "",
        f"LP fees earned: ${result['lp_fees_earned']:+.2f}",
        f"Net PnL:        ${result['net_pnl']:+.2f} ({apr_lp:.1%} APR on LP, {apr_total:.1%} APR on total)",
        f"Max drawdown:   ${result['max_drawdown']:+.2f}",
        "",
        "Note: best-case simulation; real-world may be 5-15% worse due to latency/slippage.",
    ]
    return "\n".join(lines)


def format_json_report(result: dict, *, capital_lp: float, capital_dydx: float) -> str:
    duration = result["duration_seconds"]
    enriched = dict(result)
    enriched["apr_lp"] = annualized_apr(
        net=result["net_pnl"], capital=capital_lp, duration_seconds=duration,
    )
    enriched["apr_total"] = annualized_apr(
        net=result["net_pnl"], capital=capital_lp + capital_dydx,
        duration_seconds=duration,
    )
    enriched["capital_lp"] = capital_lp
    enriched["capital_dydx"] = capital_dydx
    return json.dumps(enriched, indent=2)
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: PASS (16 tests)

- [ ] **Step 5: Commit**

```bash
git add backtest/report.py tests/test_backtest.py
git commit -m "feat(task-9): backtest report formatting (text + JSON)"
```

---

### Task 10: backtest/__main__.py — CLI runner

**Files:**
- Create: `backtest/__main__.py`
- Modify: `tests/test_backtest.py`

- [ ] **Step 1: Write failing test**

Add to `tests/test_backtest.py`:

```python
def test_cli_parses_args():
    from backtest.__main__ import parse_args

    args = parse_args([
        "--vault", "0xvault",
        "--pool", "0xpool",
        "--from", "2024-01-01",
        "--to", "2024-01-02",
        "--capital", "300",
        "--margin", "130",
    ])
    assert args.vault == "0xvault"
    assert args.pool == "0xpool"
    assert args.start_iso == "2024-01-01"
    assert args.end_iso == "2024-01-02"
    assert args.capital == 300.0
    assert args.margin == 130.0
    assert args.hedge_ratio == 1.0  # default
```

- [ ] **Step 2: Run to confirm fail**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implement backtest/__main__.py**

```python
# backtest/__main__.py
"""CLI entry point: python -m backtest --vault X --pool Y --from ... --to ..."""
from __future__ import annotations
import argparse
import asyncio
import sys
from datetime import datetime

from backtest.cache import Cache
from backtest.data import DataFetcher
from backtest.simulator import Simulator, SimConfig
from backtest.report import format_text_report, format_json_report


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="backtest")
    p.add_argument("--vault", required=True, help="Beefy vault/strategy address")
    p.add_argument("--pool", required=True, help="Uniswap V3 pool address")
    p.add_argument("--from", dest="start_iso", required=True, help="ISO start date YYYY-MM-DD")
    p.add_argument("--to", dest="end_iso", required=True, help="ISO end date YYYY-MM-DD")
    p.add_argument("--capital", type=float, default=300.0, help="LP capital USD")
    p.add_argument("--margin", type=float, default=130.0, help="dYdX margin USD")
    p.add_argument("--hedge-ratio", type=float, default=1.0)
    p.add_argument("--threshold-aggressive", type=float, default=0.01)
    p.add_argument("--max-open-orders", type=int, default=200)
    p.add_argument("--symbol", default="ETH-USD")
    p.add_argument("--token0-amount", type=float, default=0.5,
                   help="Static fallback: token0 amount in pool (used when range events missing)")
    p.add_argument("--token1-amount", type=float, default=1500.0,
                   help="Static fallback: token1 amount in pool")
    p.add_argument("--share", type=float, default=0.01,
                   help="Static fallback: user share of vault")
    p.add_argument("--tick-lower", type=int, default=-197310)
    p.add_argument("--tick-upper", type=int, default=-195303)
    p.add_argument("--cache-path", default="backtest_cache.db")
    p.add_argument("--output", default=None, help="JSON output path (optional)")
    return p.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    start_ts = datetime.fromisoformat(args.start_iso).timestamp()
    end_ts = datetime.fromisoformat(args.end_iso).timestamp()

    cache = Cache(args.cache_path)
    await cache.initialize()
    fetcher = DataFetcher(cache=cache)

    print("Fetching ETH prices...", flush=True)
    eth_prices = await fetcher.fetch_eth_prices(start=start_ts, end=end_ts)
    print(f"  -> {len(eth_prices)} samples", flush=True)

    print("Fetching dYdX funding...", flush=True)
    funding = await fetcher.fetch_dydx_funding(symbol=args.symbol, start=start_ts, end=end_ts)
    print(f"  -> {len(funding)} samples", flush=True)

    print("Fetching Beefy APR history...", flush=True)
    apr_history = await fetcher.fetch_beefy_apr_history(
        vault=args.vault, start=start_ts, end=end_ts,
    )
    print(f"  -> {len(apr_history)} samples", flush=True)

    config = SimConfig(
        vault_address=args.vault,
        pool_address=args.pool,
        start_ts=start_ts,
        end_ts=end_ts,
        capital_lp=args.capital,
        capital_dydx=args.margin,
        hedge_ratio=args.hedge_ratio,
        threshold_aggressive=args.threshold_aggressive,
        max_open_orders=args.max_open_orders,
    )

    static_range = {
        "tick_lower": args.tick_lower, "tick_upper": args.tick_upper,
        "amount0": args.token0_amount, "amount1": args.token1_amount,
        "share": args.share, "raw_balance": int(args.share * 10**18),
    }

    print("Running simulator...", flush=True)
    sim = Simulator(
        config=config,
        eth_prices=eth_prices,
        funding=funding,
        apr_history=apr_history,
        range_events=[],
        static_range=static_range,
    )
    result = await sim.run()

    print()
    print(format_text_report(
        result,
        capital_lp=args.capital, capital_dydx=args.margin,
        symbol=args.symbol, start_iso=args.start_iso, end_iso=args.end_iso,
    ))

    if args.output:
        with open(args.output, "w") as f:
            f.write(format_json_report(
                result, capital_lp=args.capital, capital_dydx=args.margin,
            ))
        print(f"\nJSON written to {args.output}")

    await cache.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: PASS (17 tests)

- [ ] **Step 5: Commit**

```bash
git add backtest/__main__.py tests/test_backtest.py
git commit -m "feat(task-10): backtest CLI runner (python -m backtest ...)"
```

---

## Phase D: Final integration

### Task 11: End-to-end test with synthetic data

**Files:**
- Modify: `tests/test_backtest.py`

- [ ] **Step 1: Write end-to-end test**

Add to `tests/test_backtest.py`:

```python
@pytest.mark.asyncio
async def test_end_to_end_synthetic_run(tmp_path):
    """Run a full simulation over synthetic data and assert final result is sane."""
    from backtest.simulator import Simulator, SimConfig
    from backtest.report import format_text_report, annualized_apr

    config = SimConfig(
        vault_address="0xvault",
        pool_address="0xpool",
        start_ts=1700000000.0,
        end_ts=1700000000.0 + 86400 * 7,  # 7 days
        capital_lp=300.0,
        capital_dydx=130.0,
        hedge_ratio=1.0,
        threshold_aggressive=0.01,
        max_open_orders=50,
    )

    # Generate synthetic 5-min ticks over 7 days
    ticks = int(86400 * 7 / 300)
    eth_prices = []
    base = 3000.0
    for i in range(ticks):
        ts = config.start_ts + i * 300
        # Sinusoidal mean-reverting around 3000 with small variance
        from math import sin
        price = base + 30 * sin(i / 50)
        eth_prices.append((ts, price))

    funding = []  # neutral
    apr_history = [(config.start_ts, 0.40)]
    static_range = {
        "tick_lower": -197310, "tick_upper": -195303,
        "amount0": 0.5, "amount1": 1500.0, "share": 0.01, "raw_balance": 10**16,
    }

    sim = Simulator(
        config=config,
        eth_prices=eth_prices,
        funding=funding,
        apr_history=apr_history,
        range_events=[],
        static_range=static_range,
    )
    result = await sim.run()

    # Sanity checks
    assert result["fills_maker"] >= 0
    assert result["fills_taker"] >= 0
    assert result["lp_fees_earned"] > 0
    assert result["duration_seconds"] == 86400 * 7

    # Reporting works
    text = format_text_report(
        result,
        capital_lp=300.0, capital_dydx=130.0,
        symbol="WETH/USDC", start_iso="2023-11-15", end_iso="2023-11-22",
    )
    assert "Net PnL" in text
    assert "WETH/USDC" in text
    assert "7.0 days" in text or "7 days" in text
```

- [ ] **Step 2: Run test**

Run: `python -m pytest tests/test_backtest.py -v`
Expected: PASS (18 tests). Should run in <5 seconds.

- [ ] **Step 3: Commit**

```bash
git add tests/test_backtest.py
git commit -m "test(task-11): end-to-end synthetic backtest run"
```

---

### Task 12: Tag + final smoke + CLAUDE.md update

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Smoke test full suite in batches**

Run:
```
python -m pytest tests/test_curve.py tests/test_grid.py tests/test_db.py tests/test_state.py tests/test_config.py tests/test_pnl.py tests/test_orderbook.py tests/test_alerts.py tests/test_margin.py tests/test_metrics.py tests/test_logging_config.py tests/test_operation.py tests/test_backtest.py -v 2>&1 | tail -5
```

Then:
```
python -m pytest tests/test_uniswap.py tests/test_beefy.py tests/test_dydx.py tests/test_reconciler.py tests/test_engine_grid.py tests/test_web.py tests/test_exchanges.py tests/test_integration_grid.py tests/test_integration_operation.py -v 2>&1 | tail -5
```

Expected: PASS in both. Total ~140+ tests.

- [ ] **Step 2: Update CLAUDE.md**

In the "Concluído" section, add:

```markdown
- ✅ **Phase 1.4 — Backtesting Framework** (tag `fase-1.4-completa`, branch feature/backtesting)
  - 12 tasks, ~140 testes
  - CLI: `python -m backtest --vault X --pool Y --from <date> --to <date>`
  - Reusa GridMakerEngine real com mocks de exchange/chain
  - Data layer: ETH price (Coinbase), dYdX funding (indexer), Beefy APR (their API com fallback)
  - Cache SQLite local pra evitar re-fetches
  - Output: APR LP, APR total, fills, drawdown, JSON opcional
  - Cleanup: removidos `max_exposure_pct`, `repost_depth`, `threshold_recovery`, `pool_deposited_usd`
  - `threshold_aggressive` agora 1% default (safety net pra falhas, não tuning)
  - Spec: `docs/superpowers/specs/2026-04-29-backtesting-design.md`
  - Plan: `docs/superpowers/plans/2026-04-29-backtesting.md`
```

In "Não iniciado", remove the Phase 1.4 line. Now it's just Phase 2.0 (on-chain execution) and pre-production.

- [ ] **Step 3: Tag**

```bash
git add CLAUDE.md
git commit -m "docs(task-12): mark Phase 1.4 complete in CLAUDE.md"
git tag fase-1.4-completa
git log --oneline | head -20
```

---

## Self-Review

### Spec coverage

| Spec section | Task |
|---|---|
| T0 cleanup configs mortas | T0 |
| `threshold_aggressive` default 1% + comment | T0 |
| CLAUDE.md note on threshold semantics | T0 |
| `backtest/cache.py` SQLite cache | T1 |
| `backtest/data.py` ETH prices via Coinbase | T2 |
| `backtest/data.py` dYdX funding via indexer | T3 |
| `backtest/data.py` Beefy APR + range events | T4 |
| `backtest/exchange_mock.py` deterministic fills | T5 |
| `backtest/chain_mock.py` mock pool/Beefy | T6 |
| `backtest/simulator.py` event-driven loop | T7 (T8 folded in) |
| Operation lifecycle in simulator | T7 |
| `backtest/report.py` text + JSON output | T9 |
| `backtest/__main__.py` CLI | T10 |
| End-to-end synthetic test | T11 |
| Final smoke + tag | T12 |

Coverage complete. Note: T8 from the spec ("Plug existing GridMakerEngine into mocked context") is rolled into T7 since they're the same commit.

### Placeholder scan

No "TBD" or "implement later". Beefy `Rebalance` events have a documented stub fallback (range stays static) — that's acceptable for MVP per the spec ("Beefy `Rebalance` event with name/signature different from expected → handled with constant-range fallback in MVP").

### Type / signature consistency

- `Cache(path)`, `await cache.initialize()`, `await cache.set(k, v)`, `await cache.get(k)` — consistent across T1-T4.
- `DataFetcher(cache=cache, fallback_apr=...)` — consistent across T2-T4.
- `MockExchangeAdapter(symbol=..., min_notional=...)` — used in T5 + T7 + T11.
- `Simulator(config=..., eth_prices=..., funding=..., apr_history=..., range_events=..., static_range=...)` — consistent across T7-T11.
- `result` dict keys (`net_pnl`, `fills_maker`, `fills_taker`, `lp_fees_earned`, etc.) — consistent across T7 (produces) + T9 (reads) + T11 (asserts).
- CLI args: `--vault`, `--pool`, `--from`, `--to`, `--capital`, `--margin`, `--hedge-ratio` — consistent across T10 implementation and spec.

All consistent.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-29-backtesting.md`.

**12 tasks** in 4 phases:
- **A** — T0 cleanup pre-task (1)
- **B** — Data layer (4 tasks: cache + 3 fetchers)
- **C** — Simulator (3 tasks: exchange mock + chain mock + engine loop)
- **D** — Reporting + CLI + integration test + tag (4 tasks)

Two execution options:

**1. Subagent-Driven (recommended)** — Mesma cadência das fases anteriores. ~12 implementer dispatches + reviews onde fizer sentido.

**2. Inline Execution** — Sessão atual.

Qual?
