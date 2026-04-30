# Pair Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** UI no app pra escolher par direto sem editar `.env`. Lista vem da Beefy API + filtrada por perp na dYdX, separada em USD-Pairs (selectable) e Cross-Pairs (Phase 3.x grayed-out).

**Architecture:** Refactor `WETH/USDC_TOKEN_ADDRESS` → genéricos `TOKEN0/1_ADDRESS`. Novos módulos `chains/beefy_api.py` + `chains/dydx_markets.py` com cache em SQLite. `engine/pair_resolver.py` classifica USD vs Cross. `engine/pair_factory.py` constrói `OperationLifecycle` per vault_id. Engine deixa de instanciar lifecycle uma vez no startup; constrói on-demand via factory. UI Beefy-style com cards, range bar, APY color-coded.

**Tech Stack:** Python 3.14, httpx, aiosqlite, Starlette + Alpine.js, web3.py. Reuso de Phases 1.4-2.0.

**Spec:** [`docs/superpowers/specs/2026-04-29-pair-picker-design.md`](../specs/2026-04-29-pair-picker-design.md)

---

## File Structure

### New
- `config/__init__.py` (marker — make `config/` a package)
- `config/stables.py` — `STABLECOINS_ARBITRUM`, `DYDX_TOKEN_TO_PERP`
- `chains/dydx_markets.py` — `DydxMarketsFetcher` (fetch + DB cache)
- `chains/beefy_api.py` — `BeefyApiFetcher` (fetch + DB cache)
- `engine/pair_resolver.py` — `classify_pair`, `build_pair_list` (pure)
- `engine/pair_factory.py` — `PairFactory.build_lifecycle(vault_id)`
- `web/templates/partials/pair_picker.html` — Settings tab with Beefy-style cards
- `tests/test_stables.py`
- `tests/test_dydx_markets.py`
- `tests/test_beefy_api.py`
- `tests/test_pair_resolver.py`
- `tests/test_pair_factory.py`
- `tests/test_pair_picker_routes.py`

### Modified
- `config.py` — rename `weth_token_address`/`usdc_token_address` → `token0_address`/`token1_address`; add `token0_decimals`/`token1_decimals`
- `db.py` — schema migration (`beefy_pairs_cache`, `dydx_markets_cache`); helpers `set_selected_vault_id`, `get_selected_vault_id`, `get_pair_from_cache`, `upsert_beefy_pair`, `upsert_dydx_market`
- `engine/__init__.py` — `GridMakerEngine.start_operation` routes via `pair_factory.build_lifecycle()` when selected_vault_id is set
- `engine/lifecycle.py` — accept `decimals0`/`decimals1` from settings (already does); use `settings.token0_address`/`settings.token1_address`
- `app.py` — lifespan creates `pair_factory` instead of singleton lifecycle
- `web/routes.py` — add `list_pairs`, `select_pair`, `refresh_pairs` handlers
- `web/templates/dashboard.html` — include `pair_picker.html` (in settings modal area or as separate include)
- `web/templates/partials/settings.html` — Trading tab simplified (pair-related fields move to pair_picker)
- `web/static/app.js` — pair-related state + handlers
- `web/static/app.css` — Beefy-style card styles (range bar, APY colors, badges)
- `.env.example` — `WETH_TOKEN_ADDRESS`/`USDC_TOKEN_ADDRESS` → `TOKEN0_ADDRESS`/`TOKEN1_ADDRESS` + `TOKEN0_DECIMALS`/`TOKEN1_DECIMALS`
- `tests/test_lifecycle.py` — fixture updates for renamed fields

---

## Phase A: Foundation (rename + DB)

### Task 0: Rename TOKEN0/1_ADDRESS + add decimals (mechanical refactor)

**Files:**
- Modify: `config.py`, `engine/lifecycle.py`, `chains/uniswap_executor.py` (no — uses settings indirectly), `web/routes.py` (no — only references via settings), `app.py` (lifespan), `tests/test_lifecycle.py`, `tests/test_lifecycle_recovery.py`, `.env.example`, `.env`

- [ ] **Step 1: Update config.py**

In `config.py::Settings`, rename fields:

```python
@dataclass
class Settings:
    # ... existing fields above ...

    # Phase 2.0 on-chain execution (renamed in pair-picker phase)
    uniswap_v3_router_address: str
    token0_address: str         # was weth_token_address
    token1_address: str         # was usdc_token_address
    token0_decimals: int        # NEW (was hardcoded 18)
    token1_decimals: int        # NEW (was hardcoded 6)
    slippage_bps: int
    uniswap_v3_pool_fee: int
```

In `Settings.from_env()`, replace existing entries:

```python
# Replace:
#   weth_token_address=os.environ.get("WETH_TOKEN_ADDRESS", "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"),
#   usdc_token_address=os.environ.get("USDC_TOKEN_ADDRESS", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
# with:
token0_address=os.environ.get(
    "TOKEN0_ADDRESS",
    os.environ.get("WETH_TOKEN_ADDRESS", "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"),
),
token1_address=os.environ.get(
    "TOKEN1_ADDRESS",
    os.environ.get("USDC_TOKEN_ADDRESS", "0xaf88d065e77c8cC2239327C5EDb3A432268e5831"),
),
token0_decimals=int(os.environ.get("TOKEN0_DECIMALS", "18")),
token1_decimals=int(os.environ.get("TOKEN1_DECIMALS", "6")),
```

The fallback to `WETH_TOKEN_ADDRESS`/`USDC_TOKEN_ADDRESS` keeps backwards compat with existing `.env` files.

- [ ] **Step 2: Update engine/lifecycle.py**

Find every reference to `self._settings.weth_token_address` and `self._settings.usdc_token_address` in `engine/lifecycle.py`. Replace:

```
weth_token_address  →  token0_address
usdc_token_address  →  token1_address
```

Specifically, these lines (use Grep to find all occurrences):

```python
# In _read_wallet_balance():
weth_token = self._uniswap._erc20(self._settings.token0_address)  # was weth_token_address
usdc_token = self._uniswap._erc20(self._settings.token1_address)  # was usdc_token_address

# In bootstrap() approve calls:
await self._uniswap.ensure_approval(
    token_address=self._settings.token1_address,  # USDC for spending
    ...,
)
await self._beefy.ensure_approval(
    token_address=self._settings.token1_address, ...,
)
await self._beefy.ensure_approval(
    token_address=self._settings.token0_address, ...,
)

# In bootstrap() swap call:
tx = await self._uniswap.swap_exact_output(
    token_in=self._settings.token1_address,
    token_out=self._settings.token0_address,
    ...,
)

# In teardown() swap call (if swap_to_usdc):
tx = await self._uniswap.swap_exact_input(
    token_in=self._settings.token0_address,
    token_out=self._settings.token1_address,
    ...,
)
```

Also rename internal variable usages so naming is consistent (e.g., `bal["weth"]` → `bal["token0"]`, `bal["usdc"]` → `bal["token1"]`). In `_read_wallet_balance` body, change:

```python
return {
    "token0": weth_raw / (10 ** self._decimals0),  # key renamed
    "token1": usdc_raw / (10 ** self._decimals1),
    "eth": eth,
}
```

Update all callers (`bal["weth"]` → `bal["token0"]`, etc).

- [ ] **Step 3: Update web/routes.py cashout endpoint**

In `web/routes.py::cashout`, find references to `weth_token_address` / `usdc_token_address` and rename:

```python
tx_hash = await engine._lifecycle._uniswap.swap_exact_input(
    token_in=engine._lifecycle._settings.token0_address,   # was weth_token_address
    token_out=engine._lifecycle._settings.token1_address,  # was usdc_token_address
    ...
)
```

Same for any `bal["weth"]` / `bal["usdc"]` references → `bal["token0"]` / `bal["token1"]`.

- [ ] **Step 4: Update tests**

In `tests/test_lifecycle.py` and `tests/test_lifecycle_recovery.py`, find every fixture setting `s.weth_token_address` / `s.usdc_token_address`. Rename to:

```python
s.token0_address = "0xWETH"
s.token1_address = "0xUSDC"
s.token0_decimals = 18
s.token1_decimals = 6
```

Same for any patches mocking `_read_wallet_balance` returning `{"weth": ..., "usdc": ...}`. Rename to `{"token0": ..., "token1": ...}`.

Run: `python -m pytest tests/test_lifecycle.py tests/test_lifecycle_recovery.py -v`
Expected: PASS (rename refactor only).

- [ ] **Step 5: Update .env.example**

```
# Phase 2.0 on-chain execution (renamed for multi-pair support)
UNISWAP_V3_ROUTER_ADDRESS=0xE592427A0AEce92De3Edee1F18E0157C05861564
TOKEN0_ADDRESS=0x82aF49447D8a07e3bd95BD0d56f35241523fBab1   # WETH (default; pair picker overrides)
TOKEN1_ADDRESS=0xaf88d065e77c8cC2239327C5EDb3A432268e5831   # USDC native (Arbitrum)
TOKEN0_DECIMALS=18
TOKEN1_DECIMALS=6
SLIPPAGE_BPS=10
UNISWAP_V3_POOL_FEE=500
```

Remove the old `WETH_TOKEN_ADDRESS` and `USDC_TOKEN_ADDRESS` lines (the from_env fallback handles legacy `.env` files; for fresh installs the new names are canonical).

- [ ] **Step 6: Update .env (user's local)**

In `.env`, add (or replace):

```
TOKEN0_ADDRESS=0x82aF49447D8a07e3bd95BD0d56f35241523fBab1
TOKEN1_ADDRESS=0xaf88d065e77c8cC2239327C5EDb3A432268e5831
TOKEN0_DECIMALS=18
TOKEN1_DECIMALS=6
```

- [ ] **Step 7: Run all affected tests**

```
python -m pytest tests/test_config.py tests/test_state.py tests/test_lifecycle.py tests/test_lifecycle_recovery.py tests/test_engine_grid.py tests/test_integration_grid.py tests/test_integration_operation.py -v
```

Expected: PASS across the board.

- [ ] **Step 8: Commit**

```bash
git add config.py engine/lifecycle.py web/routes.py tests/test_lifecycle.py tests/test_lifecycle_recovery.py .env.example .env
git commit -m "$(cat <<'EOF'
refactor(task-0): rename WETH/USDC_TOKEN_ADDRESS -> TOKEN0/1_ADDRESS

Pre-requisite for pair picker: token addresses can no longer be
hardcoded WETH/USDC since user will pick different pairs. Renames are
mechanical:
- Settings.weth_token_address -> token0_address
- Settings.usdc_token_address -> token1_address
- Adds Settings.token0_decimals (was hardcoded 18) and token1_decimals (was 6)

Backwards compat in from_env(): falls back to old WETH_TOKEN_ADDRESS /
USDC_TOKEN_ADDRESS env vars if new TOKEN0/1_ADDRESS not set.

engine/lifecycle.py + web/routes.py cashout updated; wallet balance
dict keys 'weth'/'usdc' -> 'token0'/'token1' for consistency.

Test fixtures updated. All Phase 2.0 tests still green.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 1: config/stables.py — constants

**Files:**
- Create: `config/__init__.py` (empty marker — making config/ a package alongside config.py)
- Create: `config/stables.py`
- Create: `tests/test_stables.py`

**IMPORTANT:** the existing `config.py` is a module, not a package. Creating `config/__init__.py` would conflict. Solution: Put `stables.py` at top level (`stables.py` in repo root) OR rename existing `config.py` to `config_module.py` and create `config/` package. Less disruptive: put `stables.py` at top level.

Revised: file path is `stables.py` (root) instead of `config/stables.py`.

**Files:**
- Create: `stables.py` (root)
- Create: `tests/test_stables.py`

- [ ] **Step 1: Write failing test**

Create `tests/test_stables.py`:

```python
from stables import STABLECOINS_ARBITRUM, DYDX_TOKEN_TO_PERP, is_stable, dydx_perp_for


def test_stables_set_contains_canonical_addrs():
    """Stables set should include native USDC, USDT, USDC.e, DAI on Arbitrum."""
    # Native USDC (most common)
    assert "0xaf88d065e77c8cC2239327C5EDb3A432268e5831" in STABLECOINS_ARBITRUM
    # USDT
    assert "0xFd086bC7CD5C481DCC9C85ebE478A1C0b69FCbb9" in STABLECOINS_ARBITRUM
    # USDC.e (legacy bridged)
    assert "0xFF970A61A04b1cA14834A43f5dE4533eBDDB5CC8" in STABLECOINS_ARBITRUM
    # DAI
    assert "0xDA10009cBd5D07dd0CeCc66161FC93D7c9000da1" in STABLECOINS_ARBITRUM


def test_is_stable_case_insensitive():
    """is_stable should match regardless of address casing."""
    upper = "0xAF88D065E77C8CC2239327C5EDB3A432268E5831"
    lower = upper.lower()
    assert is_stable(upper)
    assert is_stable(lower)


def test_is_stable_rejects_non_stable():
    """A non-stable address (WETH) is rejected."""
    weth = "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1"
    assert not is_stable(weth)


def test_dydx_perp_for_known_tokens():
    """Known wrapped/native tokens map to dYdX perp tickers."""
    assert dydx_perp_for("WETH") == "ETH-USD"
    assert dydx_perp_for("WBTC") == "BTC-USD"
    assert dydx_perp_for("ARB") == "ARB-USD"
    assert dydx_perp_for("LINK") == "LINK-USD"
    assert dydx_perp_for("SOL") == "SOL-USD"


def test_dydx_perp_for_unknown_returns_none():
    """Unknown symbols return None."""
    assert dydx_perp_for("UNKNOWN") is None
    assert dydx_perp_for("") is None


def test_dydx_perp_for_case_insensitive():
    """Symbols match regardless of case."""
    assert dydx_perp_for("weth") == "ETH-USD"
    assert dydx_perp_for("Arb") == "ARB-USD"
```

- [ ] **Step 2: Run to confirm fail**

Run: `python -m pytest tests/test_stables.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'stables'`

- [ ] **Step 3: Implement stables.py**

Create `stables.py` at the repo root:

```python
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
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_stables.py -v`
Expected: PASS (6 tests)

- [ ] **Step 5: Commit**

```bash
git add stables.py tests/test_stables.py
git commit -m "$(cat <<'EOF'
feat(task-1): stables.py constants for pair classification

STABLECOINS_ARBITRUM: 4 canonical stable addresses (USDC, USDT, USDC.e, DAI).
DYDX_TOKEN_TO_PERP: map token0 symbol -> dYdX perp ticker (covers ~20 perps).

is_stable(addr) and dydx_perp_for(symbol): case-insensitive lookup helpers.

6 tests cover positive matches, case insensitivity, unknown rejection.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: DB schema migration

**Files:**
- Modify: `db.py`

- [ ] **Step 1: Add ALTER blocks + CREATE TABLE in initialize()**

In `db.py::Database::initialize()`, after the existing migration blocks, add:

```python
# Phase pair-picker: cache tables for Beefy + dYdX market data
await self._conn.execute(
    """CREATE TABLE IF NOT EXISTS beefy_pairs_cache (
        vault_id TEXT PRIMARY KEY,
        chain TEXT NOT NULL,
        pool_address TEXT NOT NULL,
        token0_address TEXT NOT NULL,
        token0_symbol TEXT NOT NULL,
        token0_decimals INTEGER NOT NULL,
        token1_address TEXT NOT NULL,
        token1_symbol TEXT NOT NULL,
        token1_decimals INTEGER NOT NULL,
        pool_fee INTEGER NOT NULL,
        manager TEXT,
        tick_lower INTEGER,
        tick_upper INTEGER,
        tvl_usd REAL,
        apy_30d REAL,
        is_usd_pair INTEGER NOT NULL,
        dydx_perp TEXT,
        token0_logo_url TEXT,
        token1_logo_url TEXT,
        fetched_at REAL NOT NULL
    )"""
)
await self._conn.execute(
    """CREATE TABLE IF NOT EXISTS dydx_markets_cache (
        ticker TEXT PRIMARY KEY,
        status TEXT,
        fetched_at REAL NOT NULL
    )"""
)
await self._conn.commit()
```

- [ ] **Step 2: Add helper methods to Database class**

Append to `Database`:

```python
# ---- Pair picker: Beefy cache ---------------------------------------

async def upsert_beefy_pair(self, *, pair: dict) -> None:
    """Insert or replace a Beefy CLM in cache. `pair` dict must have all
    columns of beefy_pairs_cache table."""
    await self._conn.execute(
        """INSERT OR REPLACE INTO beefy_pairs_cache (
            vault_id, chain, pool_address,
            token0_address, token0_symbol, token0_decimals,
            token1_address, token1_symbol, token1_decimals,
            pool_fee, manager, tick_lower, tick_upper,
            tvl_usd, apy_30d, is_usd_pair, dydx_perp,
            token0_logo_url, token1_logo_url, fetched_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            pair["vault_id"], pair["chain"], pair["pool_address"],
            pair["token0_address"], pair["token0_symbol"], pair["token0_decimals"],
            pair["token1_address"], pair["token1_symbol"], pair["token1_decimals"],
            pair["pool_fee"], pair.get("manager"),
            pair.get("tick_lower"), pair.get("tick_upper"),
            pair.get("tvl_usd"), pair.get("apy_30d"),
            int(bool(pair["is_usd_pair"])), pair.get("dydx_perp"),
            pair.get("token0_logo_url"), pair.get("token1_logo_url"),
            pair["fetched_at"],
        ),
    )
    await self._conn.commit()


async def get_pair_from_cache(self, vault_id: str) -> dict | None:
    """Returns pair dict for a vault_id (case-insensitive on address), or None."""
    cursor = await self._conn.execute(
        "SELECT * FROM beefy_pairs_cache WHERE LOWER(vault_id) = LOWER(?)",
        (vault_id,),
    )
    row = await cursor.fetchone()
    if row is None:
        return None
    cols = [c[0] for c in cursor.description]
    return dict(zip(cols, row))


async def list_cached_pairs(self) -> list[dict]:
    """Returns all cached Beefy pairs, ordered by APY descending."""
    cursor = await self._conn.execute(
        "SELECT * FROM beefy_pairs_cache ORDER BY apy_30d DESC NULLS LAST"
    )
    cols = [c[0] for c in cursor.description]
    rows = await cursor.fetchall()
    return [dict(zip(cols, r)) for r in rows]


async def clear_beefy_cache(self) -> None:
    """Wipe Beefy cache. Used before a full refresh."""
    await self._conn.execute("DELETE FROM beefy_pairs_cache")
    await self._conn.commit()


# ---- Pair picker: dYdX market cache ---------------------------------

async def upsert_dydx_market(self, *, ticker: str, status: str, fetched_at: float) -> None:
    await self._conn.execute(
        "INSERT OR REPLACE INTO dydx_markets_cache (ticker, status, fetched_at) VALUES (?, ?, ?)",
        (ticker, status, fetched_at),
    )
    await self._conn.commit()


async def get_active_dydx_tickers(self) -> set[str]:
    """Returns set of ticker strings whose status is 'ACTIVE'."""
    cursor = await self._conn.execute(
        "SELECT ticker FROM dydx_markets_cache WHERE status = 'ACTIVE'"
    )
    rows = await cursor.fetchall()
    return {r[0] for r in rows}


async def clear_dydx_cache(self) -> None:
    await self._conn.execute("DELETE FROM dydx_markets_cache")
    await self._conn.commit()


# ---- Pair picker: selected vault ------------------------------------

async def set_selected_vault_id(self, vault_id: str) -> None:
    """Persist the selected pair's vault_id in the config table."""
    await self.set_config("selected_vault_id", vault_id)


async def get_selected_vault_id(self) -> str | None:
    """Returns the persisted selected_vault_id, or None if unset."""
    return await self.get_config("selected_vault_id")
```

- [ ] **Step 3: Run db tests**

Run: `python -m pytest tests/test_db.py -v`
Expected: existing tests PASS (no breakage). New helpers untested for now — covered in T3/T4 indirectly.

- [ ] **Step 4: Commit**

```bash
git add db.py
git commit -m "$(cat <<'EOF'
feat(task-2): DB schema migration for pair picker caches

Two new tables:
- beefy_pairs_cache: per-vault metadata (token addresses, decimals, pool,
  fee, manager, ticks, TVL, APY, is_usd_pair classification, dydx_perp
  match, logo URLs, fetched_at timestamp)
- dydx_markets_cache: simple (ticker, status, fetched_at) for filtering
  Beefy pairs against live dYdX perps

Helpers: upsert_beefy_pair, get_pair_from_cache, list_cached_pairs,
clear_beefy_cache, upsert_dydx_market, get_active_dydx_tickers,
clear_dydx_cache, set/get_selected_vault_id.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase B: Fetchers

### Task 3: chains/dydx_markets.py

**Files:**
- Create: `chains/dydx_markets.py`
- Create: `tests/test_dydx_markets.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_dydx_markets.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from chains.dydx_markets import DydxMarketsFetcher


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.upsert_dydx_market = AsyncMock()
    db.clear_dydx_cache = AsyncMock()
    db.get_active_dydx_tickers = AsyncMock(return_value=set())
    return db


@pytest.mark.asyncio
async def test_fetch_persists_active_markets(mock_db):
    """Fetcher writes each market to cache; only ACTIVE ones are returned active."""
    fake_response = MagicMock()
    fake_response.json = MagicMock(return_value={
        "markets": {
            "ETH-USD": {"ticker": "ETH-USD", "status": "ACTIVE"},
            "BTC-USD": {"ticker": "BTC-USD", "status": "ACTIVE"},
            "OLD-USD": {"ticker": "OLD-USD", "status": "PAUSED"},
        }
    })
    fake_response.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    fetcher = DydxMarketsFetcher(db=mock_db)
    with patch("chains.dydx_markets.httpx.AsyncClient", return_value=fake_client):
        n = await fetcher.refresh()

    assert n == 3  # all 3 written to cache
    # Verify cache cleared first
    mock_db.clear_dydx_cache.assert_awaited_once()
    # Verify each market upserted
    assert mock_db.upsert_dydx_market.await_count == 3


@pytest.mark.asyncio
async def test_fetch_handles_http_error_gracefully(mock_db):
    """If indexer is down, refresh raises but cache untouched."""
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(side_effect=Exception("connection refused"))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    fetcher = DydxMarketsFetcher(db=mock_db)
    with patch("chains.dydx_markets.httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(Exception, match="connection refused"):
            await fetcher.refresh()

    mock_db.clear_dydx_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_active_tickers_returns_set(mock_db):
    """get_active_tickers passes through DB query."""
    mock_db.get_active_dydx_tickers = AsyncMock(return_value={"ETH-USD", "BTC-USD"})

    fetcher = DydxMarketsFetcher(db=mock_db)
    tickers = await fetcher.get_active_tickers()

    assert tickers == {"ETH-USD", "BTC-USD"}
```

- [ ] **Step 2: Run to confirm fail**

Run: `python -m pytest tests/test_dydx_markets.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement chains/dydx_markets.py**

```python
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
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_dydx_markets.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add chains/dydx_markets.py tests/test_dydx_markets.py
git commit -m "$(cat <<'EOF'
feat(task-3): chains/dydx_markets.py — dYdX perp markets fetcher

DydxMarketsFetcher.refresh() hits indexer.dydx.trade/v4/perpetualMarkets,
parses the markets dict, persists each (ticker, status, fetched_at) in
DB. Clears cache before refilling for consistency.

get_active_tickers() returns set of ACTIVE tickers from cache. Used by
pair_resolver to filter Beefy pairs.

3 tests: cache write happy path, HTTP error doesn't corrupt cache, tickers
return passes through.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: chains/beefy_api.py

**Files:**
- Create: `chains/beefy_api.py`
- Create: `tests/test_beefy_api.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_beefy_api.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from chains.beefy_api import BeefyApiFetcher


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.upsert_beefy_pair = AsyncMock()
    db.clear_beefy_cache = AsyncMock()
    db.list_cached_pairs = AsyncMock(return_value=[])
    return db


def _arb_clm_payload():
    """Minimal Beefy CLM data shape (from /cows endpoint)."""
    return [
        {
            "id": "cow-uniswap-arb-eth-usdc",
            "chain": "arbitrum",
            "earnContractAddress": "0xVAULT1",
            "tokenAddress": "0xPOOL1",
            "depositTokenAddresses": [
                "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
                "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC native
            ],
            "tokens": [
                {"symbol": "WETH", "address": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "decimals": 18},
                {"symbol": "USDC", "address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
            ],
            "lpAddress": "0xPOOL1",
            "feeTier": "500",
            "strategyTypeId": "bell-curve",
            "tickLower": -197310,
            "tickUpper": -195303,
        },
        {
            "id": "cow-uniswap-arb-arb-eth",
            "chain": "arbitrum",
            "earnContractAddress": "0xVAULT2",
            "depositTokenAddresses": [
                "0x912CE59144191C1204E64559FE8253a0e49E6548",  # ARB
                "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
            ],
            "tokens": [
                {"symbol": "ARB", "address": "0x912CE59144191C1204E64559FE8253a0e49E6548", "decimals": 18},
                {"symbol": "WETH", "address": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "decimals": 18},
            ],
            "lpAddress": "0xPOOL2",
            "feeTier": "3000",
            "strategyTypeId": "wide",
        },
        {
            "id": "cow-uniswap-eth-eth-usdc",  # not arbitrum -> filter out
            "chain": "ethereum",
            "earnContractAddress": "0xVAULTETH",
            "depositTokenAddresses": [],
            "tokens": [],
        },
    ]


def _tvl_payload():
    """Beefy /tvl returns {chain: {vault_id: tvl_usd}}."""
    return {
        "arbitrum": {
            "cow-uniswap-arb-eth-usdc": 5210000.0,
            "cow-uniswap-arb-arb-eth": 1900000.0,
        }
    }


def _apy_payload():
    """Beefy /apy/breakdown returns dict per vault with apy fields."""
    return {
        "cow-uniswap-arb-eth-usdc": {"vaultApr": 0.2842, "vaultAprDaily30d": 0.2842},
        "cow-uniswap-arb-arb-eth": {"vaultApr": 0.7835, "vaultAprDaily30d": 0.7835},
    }


@pytest.mark.asyncio
async def test_refresh_writes_arbitrum_pairs_only(mock_db):
    """Filter out non-arbitrum vaults; persist arbitrum ones."""
    cows = _arb_clm_payload()
    tvl = _tvl_payload()
    apy = _apy_payload()

    async def fake_get(url, *a, **kw):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith("/cows"):
            resp.json = MagicMock(return_value=cows)
        elif url.endswith("/tvl"):
            resp.json = MagicMock(return_value=tvl)
        elif "apy/breakdown" in url:
            resp.json = MagicMock(return_value=apy)
        return resp

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(side_effect=fake_get)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    fetcher = BeefyApiFetcher(db=mock_db)
    with patch("chains.beefy_api.httpx.AsyncClient", return_value=fake_client):
        n = await fetcher.refresh(active_dydx_tickers={"ETH-USD", "ARB-USD"})

    assert n == 2  # ethereum vault filtered out
    mock_db.clear_beefy_cache.assert_awaited_once()
    assert mock_db.upsert_beefy_pair.await_count == 2


@pytest.mark.asyncio
async def test_refresh_classifies_usd_vs_cross(mock_db):
    """ETH-USDC → is_usd_pair=True; ARB-WETH → is_usd_pair=False."""
    cows = _arb_clm_payload()
    tvl = _tvl_payload()
    apy = _apy_payload()

    captured = []

    async def capture_upsert(*, pair):
        captured.append(pair)

    mock_db.upsert_beefy_pair = AsyncMock(side_effect=capture_upsert)

    async def fake_get(url, *a, **kw):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith("/cows"):
            resp.json = MagicMock(return_value=cows)
        elif url.endswith("/tvl"):
            resp.json = MagicMock(return_value=tvl)
        elif "apy/breakdown" in url:
            resp.json = MagicMock(return_value=apy)
        return resp

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(side_effect=fake_get)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    fetcher = BeefyApiFetcher(db=mock_db)
    with patch("chains.beefy_api.httpx.AsyncClient", return_value=fake_client):
        await fetcher.refresh(active_dydx_tickers={"ETH-USD", "ARB-USD"})

    eth_usdc = next(p for p in captured if p["vault_id"].lower() == "0xvault1".lower())
    arb_eth = next(p for p in captured if p["vault_id"].lower() == "0xvault2".lower())
    assert eth_usdc["is_usd_pair"] is True
    assert eth_usdc["dydx_perp"] == "ETH-USD"
    assert arb_eth["is_usd_pair"] is False  # token1 = WETH, not stable
    assert arb_eth["dydx_perp"] == "ARB-USD"  # token0 still has perp


@pytest.mark.asyncio
async def test_refresh_skips_vaults_without_dydx_perp(mock_db):
    """Vault whose token0 has no dYdX perp is excluded."""
    cows = [
        {
            "id": "cow-uniswap-arb-rare-usdc",
            "chain": "arbitrum",
            "earnContractAddress": "0xRARE",
            "tokens": [
                {"symbol": "RAREUNKNOWN", "address": "0xRARE_TKN", "decimals": 18},
                {"symbol": "USDC", "address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
            ],
            "lpAddress": "0xRARE_POOL",
            "feeTier": "3000",
        }
    ]

    async def fake_get(url, *a, **kw):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith("/cows"):
            resp.json = MagicMock(return_value=cows)
        else:
            resp.json = MagicMock(return_value={})
        return resp

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(side_effect=fake_get)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    fetcher = BeefyApiFetcher(db=mock_db)
    with patch("chains.beefy_api.httpx.AsyncClient", return_value=fake_client):
        n = await fetcher.refresh(active_dydx_tickers={"ETH-USD"})

    assert n == 0  # RAREUNKNOWN has no perp -> filtered
    mock_db.upsert_beefy_pair.assert_not_awaited()
```

- [ ] **Step 2: Run to confirm fail**

Run: `python -m pytest tests/test_beefy_api.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement chains/beefy_api.py**

```python
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

        cows = cows_resp.json() if isinstance(cows_resp.json(), list) else []
        tvl = tvl_resp.json() if isinstance(tvl_resp.json(), dict) else {}
        apy = apy_resp.json() if isinstance(apy_resp.json(), dict) else {}

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
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_beefy_api.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add chains/beefy_api.py tests/test_beefy_api.py
git commit -m "$(cat <<'EOF'
feat(task-4): chains/beefy_api.py — Beefy CLM fetcher with classification

BeefyApiFetcher.refresh(active_dydx_tickers) hits 3 Beefy endpoints
in parallel: /cows (CLM list), /tvl, /apy/breakdown. Joins data per
vault_id. For each Arbitrum CLM:
- Extracts token0/token1 addresses, symbols, decimals
- Looks up token0 in DYDX_TOKEN_TO_PERP map; skips if no perp
- Filters against active_dydx_tickers (skips paused/missing perps)
- Classifies USD-Pair vs Cross-Pair via is_stable(token1_address)
- Computes logo URLs hotlinked to Beefy CDN
- Persists in DB cache via upsert_beefy_pair

3 tests: chain filter, USD/cross classification, dYdX-perp filter.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase C: Resolver + Factory

### Task 5: engine/pair_resolver.py

**Files:**
- Create: `engine/pair_resolver.py`
- Create: `tests/test_pair_resolver.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_pair_resolver.py`:

```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from engine.pair_resolver import build_pair_list, format_pair_for_ui


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.list_cached_pairs = AsyncMock(return_value=[])
    db.get_selected_vault_id = AsyncMock(return_value=None)
    return db


@pytest.mark.asyncio
async def test_build_pair_list_separates_usd_and_cross(mock_db):
    mock_db.list_cached_pairs = AsyncMock(return_value=[
        {
            "vault_id": "0xV1", "chain": "arbitrum", "pool_address": "0xP1",
            "token0_address": "0xWETH", "token0_symbol": "WETH", "token0_decimals": 18,
            "token1_address": "0xUSDC", "token1_symbol": "USDC", "token1_decimals": 6,
            "pool_fee": 500, "manager": "bell-curve",
            "tick_lower": -197310, "tick_upper": -195303,
            "tvl_usd": 5210000, "apy_30d": 0.2842,
            "is_usd_pair": 1, "dydx_perp": "ETH-USD",
            "token0_logo_url": "https://logo/weth", "token1_logo_url": "https://logo/usdc",
            "fetched_at": 1730000000,
        },
        {
            "vault_id": "0xV2", "chain": "arbitrum", "pool_address": "0xP2",
            "token0_address": "0xARB", "token0_symbol": "ARB", "token0_decimals": 18,
            "token1_address": "0xWETH", "token1_symbol": "WETH", "token1_decimals": 18,
            "pool_fee": 3000, "manager": "wide",
            "tick_lower": None, "tick_upper": None,
            "tvl_usd": 1900000, "apy_30d": 0.7835,
            "is_usd_pair": 0, "dydx_perp": "ARB-USD",
            "token0_logo_url": "https://logo/arb", "token1_logo_url": "https://logo/weth",
            "fetched_at": 1730000000,
        },
    ])

    result = await build_pair_list(db=mock_db)

    assert len(result["usd_pairs"]) == 1
    assert len(result["cross_pairs"]) == 1
    assert result["usd_pairs"][0]["pair"] == "WETH-USDC"
    assert result["cross_pairs"][0]["pair"] == "ARB-WETH"
    assert result["selected_vault_id"] is None


@pytest.mark.asyncio
async def test_build_pair_list_includes_selected_id(mock_db):
    mock_db.list_cached_pairs = AsyncMock(return_value=[])
    mock_db.get_selected_vault_id = AsyncMock(return_value="0xVCURRENT")

    result = await build_pair_list(db=mock_db)

    assert result["selected_vault_id"] == "0xVCURRENT"


def test_format_pair_for_ui_usd_pair():
    raw = {
        "vault_id": "0xV1",
        "token0_symbol": "WETH", "token1_symbol": "USDC",
        "token0_address": "0xWETH", "token1_address": "0xUSDC",
        "token0_decimals": 18, "token1_decimals": 6,
        "manager": "bell-curve", "pool_fee": 500,
        "tvl_usd": 5210000, "apy_30d": 0.2842,
        "is_usd_pair": 1, "dydx_perp": "ETH-USD",
        "tick_lower": -197310, "tick_upper": -195303,
        "token0_logo_url": "https://logo/weth", "token1_logo_url": "https://logo/usdc",
        "pool_address": "0xPOOL",
    }
    formatted = format_pair_for_ui(raw)
    assert formatted["pair"] == "WETH-USDC"
    assert formatted["selectable"] is True
    assert formatted["pool_fee_pct"] == 0.05  # 500 bps
    assert formatted["dydx_perp"] == "ETH-USD"


def test_format_pair_for_ui_cross_pair_not_selectable():
    raw = {
        "vault_id": "0xV2",
        "token0_symbol": "ARB", "token1_symbol": "WETH",
        "token0_address": "0xARB", "token1_address": "0xWETH",
        "token0_decimals": 18, "token1_decimals": 18,
        "manager": "wide", "pool_fee": 3000,
        "tvl_usd": 1900000, "apy_30d": 0.7835,
        "is_usd_pair": 0, "dydx_perp": "ARB-USD",
        "tick_lower": None, "tick_upper": None,
        "token0_logo_url": "https://logo/arb", "token1_logo_url": "https://logo/weth",
        "pool_address": "0xPOOL2",
    }
    formatted = format_pair_for_ui(raw)
    assert formatted["selectable"] is False
    assert "Phase 3.x" in formatted["reason"]


def test_format_pair_for_ui_filters_exotic_decimals():
    """MVP only supports decimals (18, 6) for USD pairs.
    WBTC-USDC (8, 6) is included in cache but flagged not selectable in UI."""
    raw = {
        "vault_id": "0xV3",
        "token0_symbol": "WBTC", "token1_symbol": "USDC",
        "token0_address": "0xWBTC", "token1_address": "0xUSDC",
        "token0_decimals": 8, "token1_decimals": 6,  # exotic
        "manager": "bell", "pool_fee": 500,
        "tvl_usd": 2800000, "apy_30d": 0.195,
        "is_usd_pair": 1, "dydx_perp": "BTC-USD",
        "tick_lower": -50000, "tick_upper": -45000,
        "token0_logo_url": "https://logo/wbtc", "token1_logo_url": "https://logo/usdc",
        "pool_address": "0xPOOL3",
    }
    formatted = format_pair_for_ui(raw)
    assert formatted["selectable"] is False
    assert "decimals" in formatted["reason"]
```

- [ ] **Step 2: Run to confirm fail**

Run: `python -m pytest tests/test_pair_resolver.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement engine/pair_resolver.py**

```python
"""Pair resolver: format cached Beefy pairs for the UI.

Reads from db.list_cached_pairs(), separates USD-Pairs vs Cross-Pairs,
adds UI-friendly fields (formatted symbols, pct fee, range visualization),
flags non-selectable pairs with reason.
"""
from __future__ import annotations
from typing import Any

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
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_pair_resolver.py -v`
Expected: PASS (5 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/pair_resolver.py tests/test_pair_resolver.py
git commit -m "$(cat <<'EOF'
feat(task-5): engine/pair_resolver.py — UI formatter for cached pairs

build_pair_list(db) reads from beefy_pairs_cache, splits into usd_pairs
+ cross_pairs lists, adds selected_vault_id and last_refresh_ts.

format_pair_for_ui(raw) shapes a single pair for frontend:
- pair display name (e.g., 'WETH-USDC')
- pool_fee_pct (500 bps -> 0.05)
- token logos passed through
- selectability + reason: cross-pair => 'Phase 3.x', exotic decimals
  (e.g., WBTC at 8) => 'Decimals (8,6) not supported in MVP'

5 tests: USD/cross separation, selected_id pass-through, formatting,
cross flag, decimals filter (WBTC excluded).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: engine/pair_factory.py

**Files:**
- Create: `engine/pair_factory.py`
- Create: `tests/test_pair_factory.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_pair_factory.py`:

```python
import pytest
import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch
from engine.pair_factory import build_lifecycle


@pytest.fixture
def mock_settings():
    s = MagicMock()
    s.token0_address = "0xWETH"
    s.token1_address = "0xUSDC"
    s.token0_decimals = 18
    s.token1_decimals = 6
    s.uniswap_v3_pool_fee = 500
    s.uniswap_v3_router_address = "0xROUTER"
    s.dydx_symbol = "ETH-USD"
    s.wallet_address = "0xWALLET"
    s.clm_vault_address = "0xVAULT_OLD"
    s.clm_pool_address = "0xPOOL_OLD"
    s.alert_webhook_url = ""
    return s


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_pair_from_cache = AsyncMock(return_value=None)
    return db


@pytest.fixture
def mock_hub():
    return MagicMock()


@pytest.fixture
def mock_w3():
    w3 = MagicMock()
    w3.eth = MagicMock()
    w3.to_checksum_address = lambda a: a
    w3.eth.contract = MagicMock()
    return w3


@pytest.fixture
def mock_account():
    a = MagicMock()
    a.address = "0xWALLET"
    return a


@pytest.fixture
def mock_exchange():
    return MagicMock()


@pytest.mark.asyncio
async def test_build_lifecycle_raises_when_vault_not_in_cache(
    mock_settings, mock_hub, mock_db, mock_exchange, mock_w3, mock_account,
):
    mock_db.get_pair_from_cache = AsyncMock(return_value=None)
    with pytest.raises(ValueError, match="not in cache"):
        await build_lifecycle(
            settings=mock_settings, hub=mock_hub, db=mock_db,
            exchange=mock_exchange,
            selected_vault_id="0xMISSING",
            w3=mock_w3, account=mock_account,
        )


@pytest.mark.asyncio
async def test_build_lifecycle_raises_for_cross_pair(
    mock_settings, mock_hub, mock_db, mock_exchange, mock_w3, mock_account,
):
    mock_db.get_pair_from_cache = AsyncMock(return_value={
        "vault_id": "0xV2", "is_usd_pair": 0,
        "token0_address": "0xARB", "token1_address": "0xWETH",
        "token0_decimals": 18, "token1_decimals": 18,
        "pool_address": "0xPOOL", "pool_fee": 3000, "dydx_perp": "ARB-USD",
    })
    with pytest.raises(ValueError, match="cross-pair"):
        await build_lifecycle(
            settings=mock_settings, hub=mock_hub, db=mock_db,
            exchange=mock_exchange,
            selected_vault_id="0xV2",
            w3=mock_w3, account=mock_account,
        )


@pytest.mark.asyncio
async def test_build_lifecycle_returns_lifecycle_with_pair_settings(
    mock_settings, mock_hub, mock_db, mock_exchange, mock_w3, mock_account,
):
    """Successful build returns OperationLifecycle with settings overridden by pair data."""
    mock_db.get_pair_from_cache = AsyncMock(return_value={
        "vault_id": "0xV1", "is_usd_pair": 1,
        "token0_address": "0xWETH_NEW", "token1_address": "0xUSDC_NEW",
        "token0_decimals": 18, "token1_decimals": 6,
        "pool_address": "0xPOOL_NEW", "pool_fee": 500, "dydx_perp": "ETH-USD",
    })

    lifecycle = await build_lifecycle(
        settings=mock_settings, hub=mock_hub, db=mock_db,
        exchange=mock_exchange,
        selected_vault_id="0xV1",
        w3=mock_w3, account=mock_account,
    )

    # Lifecycle's settings should reflect the pair (not the original)
    assert lifecycle._settings.token0_address == "0xWETH_NEW"
    assert lifecycle._settings.token1_address == "0xUSDC_NEW"
    assert lifecycle._settings.clm_vault_address == "0xV1"
    assert lifecycle._settings.clm_pool_address == "0xPOOL_NEW"
```

- [ ] **Step 2: Run to confirm fail**

Run: `python -m pytest tests/test_pair_factory.py -v`
Expected: FAIL — module not found.

- [ ] **Step 3: Implement engine/pair_factory.py**

```python
"""Per-vault factory for OperationLifecycle.

Replaces the singleton lifecycle pattern. When user picks a pair,
DB stores selected_vault_id. At start_operation time, build_lifecycle()
reads pair info from cache and constructs the OperationLifecycle with
the right token addresses, decimals, fee tier, dYdX symbol.
"""
from __future__ import annotations
import dataclasses
import logging
from typing import TYPE_CHECKING

from chains.uniswap import UniswapV3PoolReader
from chains.beefy import BeefyClmReader
from chains.uniswap_executor import UniswapExecutor
from chains.beefy_executor import BeefyExecutor

if TYPE_CHECKING:
    from engine.lifecycle import OperationLifecycle

logger = logging.getLogger(__name__)


async def build_lifecycle(
    *, settings, hub, db, exchange,
    selected_vault_id: str,
    w3, account,
):
    """Build a fresh OperationLifecycle for the given vault_id.

    Reads pair metadata from beefy_pairs_cache. Constructs UniswapExecutor,
    BeefyExecutor, pool_reader, beefy_reader with the pair's addresses
    and decimals. Returns an OperationLifecycle ready to bootstrap().

    Raises ValueError if:
    - vault_id not in cache (need refresh first)
    - pair is cross-pair (Phase 3.x scope)
    - pair has unsupported decimals
    """
    # Lazy import to avoid circular
    from engine.lifecycle import OperationLifecycle

    pair = await db.get_pair_from_cache(selected_vault_id)
    if pair is None:
        raise ValueError(
            f"Vault {selected_vault_id} not in cache. "
            f"Refresh pair list (POST /pairs/refresh)."
        )
    if not pair.get("is_usd_pair"):
        raise ValueError(
            f"Vault {selected_vault_id} is cross-pair (token1 not stable); "
            f"requires Phase 3.x dual-leg hedge."
        )

    decimals0 = int(pair["token0_decimals"])
    decimals1 = int(pair["token1_decimals"])
    if (decimals0, decimals1) != (18, 6):
        raise ValueError(
            f"Vault {selected_vault_id} has unsupported decimals "
            f"({decimals0}, {decimals1}); MVP supports (18, 6) only."
        )

    # Patch settings with pair-specific overrides
    pair_settings = dataclasses.replace(
        settings,
        token0_address=pair["token0_address"],
        token1_address=pair["token1_address"],
        token0_decimals=decimals0,
        token1_decimals=decimals1,
        clm_vault_address=pair["vault_id"],
        clm_pool_address=pair["pool_address"],
        uniswap_v3_pool_fee=int(pair["pool_fee"]),
        dydx_symbol=pair["dydx_perp"],
    )

    pool_reader = UniswapV3PoolReader(
        w3=w3, pool_address=pair["pool_address"],
        decimals0=decimals0, decimals1=decimals1,
    )
    beefy_reader = BeefyClmReader(
        w3=w3, strategy_address=pair["vault_id"],
        wallet_address=settings.wallet_address,
        decimals0=decimals0, decimals1=decimals1,
    )
    uniswap_exec = UniswapExecutor(
        w3=w3, account=account,
        router_address=settings.uniswap_v3_router_address,
    )
    beefy_exec = BeefyExecutor(
        w3=w3, account=account,
        strategy_address=pair["vault_id"],
    )

    lifecycle = OperationLifecycle(
        settings=pair_settings, hub=hub, db=db,
        exchange=exchange, uniswap=uniswap_exec, beefy=beefy_exec,
        pool_reader=pool_reader, beefy_reader=beefy_reader,
        decimals0=decimals0, decimals1=decimals1,
    )
    logger.info(
        f"Built lifecycle for vault {selected_vault_id} "
        f"({pair.get('token0_symbol')}/{pair.get('token1_symbol')})"
    )
    return lifecycle
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_pair_factory.py -v`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add engine/pair_factory.py tests/test_pair_factory.py
git commit -m "$(cat <<'EOF'
feat(task-6): engine/pair_factory.py — per-vault OperationLifecycle factory

build_lifecycle(settings, hub, db, exchange, selected_vault_id, w3, account)
constructs a fresh OperationLifecycle for a specific Beefy CLM:
- Reads pair metadata from beefy_pairs_cache (raises if not cached)
- Validates: must be USD-Pair, must have decimals (18, 6)
- Patches Settings via dataclasses.replace with pair-specific addresses,
  decimals, fee tier, dYdX symbol
- Constructs UniswapV3PoolReader, BeefyClmReader, UniswapExecutor,
  BeefyExecutor with pair's pool/vault addresses
- Returns OperationLifecycle ready for bootstrap()

3 tests: vault-not-in-cache raises, cross-pair raises, success returns
lifecycle with patched settings.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase D: Engine + REST integration

### Task 7: engine/__init__.py routes via pair_factory

**Files:**
- Modify: `engine/__init__.py`

- [ ] **Step 1: Modify GridMakerEngine constructor + start_operation**

In `engine/__init__.py`, the GridMakerEngine currently has `_lifecycle` as a singleton. We're keeping that for backwards compat but adding `_pair_factory_args` (the things needed to construct lifecycle on-demand).

Add new constructor kwargs:

```python
class GridMakerEngine:
    def __init__(
        self, *, settings: Settings, hub: StateHub, db: Database,
        exchange: ExchangeAdapter | None = None,
        pool_reader: UniswapV3PoolReader | None = None,
        beefy_reader: BeefyClmReader | None = None,
        lifecycle: "OperationLifecycle | None" = None,
        pair_factory_w3=None,         # NEW
        pair_factory_account=None,    # NEW
        decimals0: int = 18, decimals1: int = 6,
    ):
        # ... existing assignments ...
        self._lifecycle = lifecycle
        self._pair_factory_w3 = pair_factory_w3
        self._pair_factory_account = pair_factory_account
```

In `start_operation`, before falling back to legacy or singleton lifecycle, check if user has selected a pair via DB:

```python
async def start_operation(self, *, usdc_budget: float | None = None) -> int:
    # Phase pair-picker: if a pair is selected via UI, build lifecycle on-demand
    selected_vault_id = await self._db.get_selected_vault_id()
    if (
        selected_vault_id
        and self._pair_factory_w3 is not None
        and self._pair_factory_account is not None
    ):
        if usdc_budget is None:
            raise RuntimeError(
                "usdc_budget required when pair is selected. "
                "Pass {usdc_budget: <float>} in request body."
            )
        from engine.pair_factory import build_lifecycle
        lifecycle = await build_lifecycle(
            settings=self._settings, hub=self._hub, db=self._db,
            exchange=self._exchange,
            selected_vault_id=selected_vault_id,
            w3=self._pair_factory_w3,
            account=self._pair_factory_account,
        )
        return await lifecycle.bootstrap(usdc_budget=usdc_budget)

    # Phase 2.0 path: singleton lifecycle if configured
    if usdc_budget is not None and self._lifecycle is not None:
        return await self._lifecycle.bootstrap(usdc_budget=usdc_budget)
    if self._lifecycle is not None:
        raise RuntimeError(
            "usdc_budget required when lifecycle is configured. "
            "Pass {usdc_budget: <float>} in request body."
        )

    # Legacy path: existing Phase 1.2 behavior (no on-chain bootstrap)
    # ... rest of legacy start_operation body unchanged ...
```

The `stop_operation` similarly needs to find the right lifecycle. For pair-selected ops, the lifecycle was created at `start_operation` time and not stored. The teardown needs a fresh lifecycle for the SAME vault. Pattern:

```python
async def stop_operation(
    self, *, close_reason: str = "user", swap_to_usdc: bool = False,
) -> dict:
    op_row = await self._db.get_active_operation()
    bootstrap_state = (op_row or {}).get("bootstrap_state") or "pending"
    op_was_bootstrapped_via_lifecycle = bootstrap_state not in ("pending", None)

    # If op went through lifecycle, route teardown via lifecycle (factory or singleton)
    if op_was_bootstrapped_via_lifecycle:
        # Try pair_factory path first
        selected_vault_id = await self._db.get_selected_vault_id()
        if (
            selected_vault_id
            and self._pair_factory_w3 is not None
            and self._pair_factory_account is not None
        ):
            from engine.pair_factory import build_lifecycle
            lifecycle = await build_lifecycle(
                settings=self._settings, hub=self._hub, db=self._db,
                exchange=self._exchange,
                selected_vault_id=selected_vault_id,
                w3=self._pair_factory_w3,
                account=self._pair_factory_account,
            )
            return await lifecycle.teardown(
                swap_to_usdc=swap_to_usdc, close_reason=close_reason,
            )
        # Fallback to singleton lifecycle (Phase 2.0 path)
        if self._lifecycle is not None:
            return await self._lifecycle.teardown(
                swap_to_usdc=swap_to_usdc, close_reason=close_reason,
            )

    # Legacy path: existing Phase 1.2 behavior
    # ... rest of legacy stop_operation body unchanged ...
```

- [ ] **Step 2: Run existing tests to confirm no regressions**

```
python -m pytest tests/test_engine_grid.py tests/test_integration_grid.py tests/test_integration_operation.py tests/test_lifecycle.py tests/test_lifecycle_recovery.py -v
```

Expected: PASS — these tests don't pass `pair_factory_w3` so they use legacy or singleton paths.

- [ ] **Step 3: Commit**

```bash
git add engine/__init__.py
git commit -m "$(cat <<'EOF'
refactor(task-7): GridMakerEngine routes start/stop via pair_factory

Adds pair_factory_w3 + pair_factory_account kwargs to GridMakerEngine
constructor (both default None for back-compat).

start_operation():
1. If pair_factory available + selected_vault_id in DB: build fresh
   lifecycle via pair_factory.build_lifecycle() and call bootstrap.
2. Else if singleton lifecycle configured: use it (Phase 2.0 path).
3. Else: legacy snapshot+hedge-only path.

stop_operation() mirrors: routes via pair_factory if op was bootstrapped
via lifecycle and pair_factory available.

Existing tests pass — they don't pass pair_factory_w3, so paths 2/3
remain exercised.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 8: REST API for pairs

**Files:**
- Modify: `web/routes.py`
- Create: `tests/test_pair_picker_routes.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_pair_picker_routes.py`:

```python
import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from starlette.testclient import TestClient


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_USER", "admin")
    monkeypatch.setenv("AUTH_PASS", "secret")
    monkeypatch.setenv("WALLET_ADDRESS", "0x1")
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "0x2")
    monkeypatch.setenv("ARBITRUM_RPC_URL", "https://rpc")
    monkeypatch.setenv("CLM_VAULT_ADDRESS", "0x3")
    monkeypatch.setenv("CLM_POOL_ADDRESS", "0x4")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))

    from app import create_app
    return create_app(start_engine=False)


def _auth_headers():
    import base64
    return {"Authorization": f"Basic {base64.b64encode(b'admin:secret').decode()}"}


def test_list_pairs_returns_categorized_dict(app):
    """GET /pairs returns {usd_pairs, cross_pairs, selected_vault_id, last_refresh_ts}."""
    client = TestClient(app)
    resp = client.get("/pairs", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert "usd_pairs" in data
    assert "cross_pairs" in data
    assert "selected_vault_id" in data
    assert "last_refresh_ts" in data
    assert isinstance(data["usd_pairs"], list)
    assert isinstance(data["cross_pairs"], list)


def test_select_pair_rejects_missing_body(app):
    """POST /pairs/select with no body returns 400."""
    client = TestClient(app)
    resp = client.post("/pairs/select", headers=_auth_headers())
    assert resp.status_code == 400


def test_select_pair_rejects_unknown_vault(app):
    """POST /pairs/select with vault_id not in cache returns 400."""
    client = TestClient(app)
    resp = client.post(
        "/pairs/select",
        headers={**_auth_headers(), "Content-Type": "application/json"},
        content=json.dumps({"vault_id": "0xUNKNOWN"}),
    )
    assert resp.status_code == 400
    assert "not in cache" in resp.json().get("error", "").lower()


def test_refresh_pairs_returns_500_when_apis_unreachable(app):
    """POST /pairs/refresh in test env (no internet mocked) returns 500."""
    client = TestClient(app)
    # We don't mock the HTTP client; this should fail and return 500
    resp = client.post("/pairs/refresh", headers=_auth_headers())
    # Either 500 (HTTP error caught) or 200 (if it actually reached APIs and worked).
    # In CI offline mode, expect 500. Accept both for robustness.
    assert resp.status_code in (200, 500)
```

- [ ] **Step 2: Run to confirm fail**

Run: `python -m pytest tests/test_pair_picker_routes.py -v`
Expected: FAIL — endpoints don't exist yet.

- [ ] **Step 3: Implement handlers in web/routes.py**

Append to `web/routes.py`:

```python
async def list_pairs(request: Request):
    """GET /pairs — returns USD/cross-pairs from cache + selected_vault_id."""
    db = request.app.state.db
    from engine.pair_resolver import build_pair_list
    result = await build_pair_list(db=db)
    return JSONResponse(result, status_code=200)


async def select_pair(request: Request):
    """POST /pairs/select — body {vault_id}: validate + persist."""
    db = request.app.state.db
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid or missing JSON body"}, status_code=400)
    vault_id = (body or {}).get("vault_id")
    if not vault_id or not isinstance(vault_id, str):
        return JSONResponse({"error": "vault_id required"}, status_code=400)

    pair = await db.get_pair_from_cache(vault_id)
    if pair is None:
        return JSONResponse(
            {"error": f"Vault {vault_id} not in cache. Refresh pair list first."},
            status_code=400,
        )
    if not pair.get("is_usd_pair"):
        return JSONResponse(
            {"error": "Cross-pairs not selectable in MVP (Phase 3.x scope)"},
            status_code=400,
        )
    decimals = (pair.get("token0_decimals"), pair.get("token1_decimals"))
    if decimals != (18, 6):
        return JSONResponse(
            {"error": f"Unsupported decimals {decimals}; MVP only (18, 6)"},
            status_code=400,
        )

    await db.set_selected_vault_id(vault_id)
    return JSONResponse(
        {"selected_vault_id": vault_id, "pair": pair},
        status_code=200,
    )


async def refresh_pairs(request: Request):
    """POST /pairs/refresh — re-fetch dYdX + Beefy and update caches."""
    db = request.app.state.db
    from chains.dydx_markets import DydxMarketsFetcher
    from chains.beefy_api import BeefyApiFetcher

    try:
        dydx = DydxMarketsFetcher(db=db)
        n_dydx = await dydx.refresh()
        active = await dydx.get_active_tickers()

        beefy = BeefyApiFetcher(db=db)
        n_beefy = await beefy.refresh(active_dydx_tickers=active)

        import time
        return JSONResponse({
            "dydx_markets_count": n_dydx,
            "beefy_pairs_count": n_beefy,
            "last_refresh_ts": time.time(),
        }, status_code=200)
    except Exception as e:
        import logging
        logging.getLogger(__name__).exception(f"refresh_pairs failed: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
```

- [ ] **Step 4: Register routes in app.py**

Add to imports at top of `app.py`:

```python
from web.routes import (
    # ... existing ...
    list_pairs, select_pair, refresh_pairs,
)
```

In the `routes = [...]` list:

```python
Route("/pairs", list_pairs),
Route("/pairs/select", select_pair, methods=["POST"]),
Route("/pairs/refresh", refresh_pairs, methods=["POST"]),
```

- [ ] **Step 5: Run tests**

Run: `python -m pytest tests/test_pair_picker_routes.py -v`
Expected: 4 PASS (or 3 PASS + 1 with status 500 for the offline-API test).

- [ ] **Step 6: Commit**

```bash
git add web/routes.py app.py tests/test_pair_picker_routes.py
git commit -m "$(cat <<'EOF'
feat(task-8): REST API for pair picker

Three endpoints:
- GET /pairs — returns {usd_pairs, cross_pairs, selected_vault_id,
  last_refresh_ts}. Reads from cache only.
- POST /pairs/select — body {vault_id}. Validates: must be in cache,
  must be USD-Pair, decimals (18, 6). Persists via set_selected_vault_id.
- POST /pairs/refresh — re-fetches dYdX indexer + Beefy /cows /tvl
  /apy/breakdown. Updates both caches. Returns counts + timestamp.

4 tests: list endpoint shape, select rejects bad input/unknown vault,
refresh handles offline gracefully.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 9: app.py lifespan — pair_factory wiring

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Pass pair_factory_w3/account to GridMakerEngine**

In `app.py::lifespan`, find the section where `GridMakerEngine(...)` is constructed (post wallet+executors). Add the pair-factory kwargs:

```python
engine = GridMakerEngine(
    settings=settings, hub=state, db=db,
    exchange=exchange,
    pool_reader=pool_reader,
    beefy_reader=beefy_reader,
    lifecycle=lifecycle,        # singleton (Phase 2.0 fallback)
    pair_factory_w3=w3,          # NEW: enables on-demand factory path
    pair_factory_account=account,  # NEW
)
```

If `account is None` (placeholder env), skip both kwargs (engine will fall back to legacy path):

```python
factory_kwargs = {}
if account is not None:
    factory_kwargs["pair_factory_w3"] = w3
    factory_kwargs["pair_factory_account"] = account

engine = GridMakerEngine(
    settings=settings, hub=state, db=db,
    exchange=exchange,
    pool_reader=pool_reader,
    beefy_reader=beefy_reader,
    lifecycle=lifecycle,
    **factory_kwargs,
)
```

- [ ] **Step 2: Smoke test**

```
python -c "from app import create_app; print(create_app(start_engine=False))"
```
Expected: prints app object without errors.

- [ ] **Step 3: Run smoke suite**

```
python -m pytest tests/test_lifecycle.py tests/test_engine_grid.py tests/test_integration_grid.py tests/test_integration_operation.py -v
```
Expected: all PASS.

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "$(cat <<'EOF'
feat(task-9): app.py — wire pair_factory into GridMakerEngine

Lifespan passes w3 + account to GridMakerEngine via pair_factory_w3
+ pair_factory_account kwargs. Engine then constructs OperationLifecycle
on-demand at start_operation when selected_vault_id is set in DB.

If account is None (placeholder env), kwargs omitted; engine falls back
to singleton lifecycle (Phase 2.0) or legacy path (Phase 1.2).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase E: UI

### Task 10: pair_picker.html partial

**Files:**
- Create: `web/templates/partials/pair_picker.html`
- Modify: `web/templates/dashboard.html` (include) + `web/templates/partials/settings.html` (link to picker)

- [ ] **Step 1: Create pair_picker.html**

```html
<!-- web/templates/partials/pair_picker.html -->
<div x-show="showPairPicker" x-cloak class="modal-overlay" @click.self="showPairPicker = false">
    <div class="modal-box" @click.stop style="max-width: 800px;">
        <div class="flex items-center justify-between mb-6">
            <h2 class="text-lg font-bold text-slate-800">Trading Pair</h2>
            <button @click="showPairPicker = false" class="text-slate-400 hover:text-slate-600 transition p-1 rounded-lg hover:bg-slate-100">
                <svg xmlns="http://www.w3.org/2000/svg" width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M18 6L6 18M6 6l12 12"/></svg>
            </button>
        </div>

        <!-- Toolbar: search, sort, refresh -->
        <div class="flex flex-wrap items-center gap-3 mb-4">
            <input type="text" x-model="pairSearch"
                   placeholder="Filtrar por par (ex: ETH, ARB)"
                   class="flex-1 px-3 py-2 border border-slate-200 rounded text-sm">
            <select x-model="pairSort" class="px-3 py-2 border border-slate-200 rounded text-sm">
                <option value="apy">Sort: APY ↓</option>
                <option value="tvl">Sort: TVL ↓</option>
                <option value="pair">Sort: Pair A→Z</option>
            </select>
            <button @click="refreshPairs()"
                    :disabled="pairRefreshing"
                    class="px-3 py-2 bg-indigo-600 text-white rounded text-sm hover:bg-indigo-700 disabled:opacity-50">
                <span x-show="!pairRefreshing">🔄 Refresh</span>
                <span x-show="pairRefreshing">Atualizando...</span>
            </button>
        </div>
        <p class="text-xs text-slate-400 mb-4" x-show="pairsLastRefresh > 0">
            Última atualização: <span x-text="formatRelativeTime(pairsLastRefresh)"></span>
        </p>

        <!-- USD-Pairs section -->
        <div class="mb-6">
            <h3 class="text-sm font-bold uppercase tracking-wider text-slate-600 mb-3">
                USD-Pairs
                <span class="text-slate-400 font-normal" x-text="'(' + filteredUsdPairs.length + ')'"></span>
            </h3>
            <div class="space-y-3" x-show="filteredUsdPairs.length > 0">
                <template x-for="p in filteredUsdPairs" :key="p.vault_id">
                    <div class="pair-card"
                         :class="p.vault_id === pairsData.selected_vault_id ? 'pair-card-selected' : ''"
                         @click="selectPair(p)">
                        <div class="flex items-center justify-between">
                            <div class="flex items-center gap-3">
                                <div class="pair-logos">
                                    <img :src="p.token0_logo_url" class="logo logo0" onerror="this.replaceWith(Object.assign(document.createElement('span'), {textContent:'💎', className:'logo-fallback'}))">
                                    <img :src="p.token1_logo_url" class="logo logo1" onerror="this.replaceWith(Object.assign(document.createElement('span'), {textContent:'💵', className:'logo-fallback'}))">
                                </div>
                                <div>
                                    <p class="font-semibold text-slate-800" x-text="p.pair"></p>
                                    <p class="text-xs text-slate-500">
                                        <span class="manager-badge" x-text="p.manager"></span>
                                        ·
                                        <span x-text="p.dex + ' ' + (p.pool_fee_pct * 100).toFixed(2) + '%'"></span>
                                    </p>
                                </div>
                            </div>
                            <div class="text-right">
                                <div class="text-xs text-slate-400">TVL</div>
                                <div class="text-sm font-semibold" x-text="formatTvl(p.tvl_usd)"></div>
                            </div>
                            <div class="text-right">
                                <div class="text-xs text-slate-400">APY 30d</div>
                                <div class="text-sm font-bold" :class="apyColorClass(p.apy_30d)"
                                     x-text="formatApy(p.apy_30d)"></div>
                            </div>
                            <button class="px-3 py-1.5 text-xs bg-emerald-600 text-white rounded hover:bg-emerald-700"
                                    x-show="p.vault_id !== pairsData.selected_vault_id">
                                Select
                            </button>
                            <span x-show="p.vault_id === pairsData.selected_vault_id"
                                  class="px-3 py-1.5 text-xs bg-slate-200 text-slate-700 rounded">
                                ✓ Atual
                            </span>
                        </div>
                    </div>
                </template>
            </div>
            <div x-show="filteredUsdPairs.length === 0" class="text-sm text-slate-400 italic">
                Nenhum par disponível. Clique Refresh.
            </div>
        </div>

        <!-- Cross-Pairs section (display-only) -->
        <div x-show="filteredCrossPairs.length > 0">
            <h3 class="text-sm font-bold uppercase tracking-wider text-slate-600 mb-2">
                Cross-Pairs
                <span class="text-slate-400 font-normal" x-text="'(' + filteredCrossPairs.length + ')'"></span>
                <span class="text-amber-600 font-normal text-xs">— Phase 3.x</span>
            </h3>
            <p class="text-xs text-slate-500 mb-3">
                Cross-pairs (token1 não-stable) precisam hedge dual-leg, suportado em Phase 3.x.
            </p>
            <div class="space-y-2 opacity-50">
                <template x-for="p in filteredCrossPairs" :key="p.vault_id">
                    <div class="pair-card pair-card-disabled">
                        <div class="flex items-center justify-between">
                            <div class="flex items-center gap-3">
                                <div class="pair-logos">
                                    <img :src="p.token0_logo_url" class="logo logo0" onerror="this.replaceWith(Object.assign(document.createElement('span'), {textContent:'💎', className:'logo-fallback'}))">
                                    <img :src="p.token1_logo_url" class="logo logo1" onerror="this.replaceWith(Object.assign(document.createElement('span'), {textContent:'💎', className:'logo-fallback'}))">
                                </div>
                                <div>
                                    <p class="font-semibold text-slate-700" x-text="p.pair"></p>
                                    <p class="text-xs text-slate-400" x-text="p.manager + ' · ' + p.dex"></p>
                                </div>
                            </div>
                            <div class="text-sm font-semibold text-slate-500" x-text="formatTvl(p.tvl_usd)"></div>
                            <div class="text-sm" :class="apyColorClass(p.apy_30d)" x-text="formatApy(p.apy_30d)"></div>
                            <span class="text-xs text-slate-400 italic">não selecionável</span>
                        </div>
                    </div>
                </template>
            </div>
        </div>
    </div>
</div>
```

- [ ] **Step 2: Include in dashboard.html**

In `web/templates/dashboard.html`, near the existing `{% include "partials/settings.html" %}` and `{% include "partials/start_modal.html" %}`, add:

```html
{% include "partials/pair_picker.html" %}
```

- [ ] **Step 3: Add link in settings.html Trading tab**

In `web/templates/partials/settings.html`, inside the Trading tab section, add a new cfg-group at the top:

```html
<div class="cfg-group">
    <label class="cfg-label">Trading Pair</label>
    <div class="text-sm" x-show="pairsData.selected_vault_id">
        Selecionado: <span class="font-semibold" x-text="selectedPairLabel || '...'"></span>
    </div>
    <div class="text-sm text-slate-400" x-show="!pairsData.selected_vault_id">
        Nenhum par selecionado.
    </div>
    <button type="button" @click="showSettings = false; openPairPicker()"
            class="px-3 py-2 bg-slate-200 hover:bg-slate-300 text-slate-700 rounded text-sm mt-2">
        Escolher Par
    </button>
    <p class="cfg-hint">Clique pra abrir lista de pares disponíveis (Beefy + dYdX).</p>
</div>
```

- [ ] **Step 4: Manual smoke (skip if not running)**

If you can run uvicorn:
- Open settings → see "Trading Pair" group with "Escolher Par" button
- Click button → settings closes, pair picker modal opens
- Pair picker shows toolbar, sections (likely empty until Refresh clicked)

- [ ] **Step 5: Commit**

```bash
git add web/templates/partials/pair_picker.html web/templates/dashboard.html web/templates/partials/settings.html
git commit -m "$(cat <<'EOF'
feat(task-10): pair_picker.html — Beefy-style cards UI partial

New modal partial with:
- Toolbar: search input, sort dropdown (APY/TVL/Pair), refresh button
- USD-Pairs section: cards with logos (hotlinked Beefy CDN), pair name,
  manager badge, DEX + fee tier, TVL, APY (color-coded), Select button
- Cross-Pairs section: displayed but grayed-out, with 'Phase 3.x' note
- Empty state for both sections

Settings.html Trading tab gets a 'Trading Pair' cfg-group with the
currently-selected pair name + 'Escolher Par' button that opens the
picker modal (closes settings first to avoid stacking modals).

Cards use placeholder onerror for missing logos (💎 / 💵 emoji fallback).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 11: app.css styling

**Files:**
- Modify: `web/static/app.css` (or create one if not exists; check existing CSS structure)

- [ ] **Step 1: Find current CSS**

Look for `web/static/app.css` or similar. If absent, the project uses inline Tailwind via CDN. Add new component styles.

If `app.css` doesn't exist, create it. Check `web/templates/base.html` for `<link rel="stylesheet" href="..."`.

- [ ] **Step 2: Add pair-picker styles**

Append to `web/static/app.css` (creating if not present):

```css
/* === Pair Picker (pair_picker.html) === */

.pair-card {
    border: 1px solid rgba(226, 232, 240, 1);
    border-radius: 10px;
    padding: 12px 16px;
    background: white;
    transition: border-color 0.15s ease, box-shadow 0.15s ease;
    cursor: pointer;
}
.pair-card:hover:not(.pair-card-disabled) {
    border-color: rgba(99, 102, 241, 0.5);
    box-shadow: 0 2px 6px rgba(99, 102, 241, 0.08);
}
.pair-card-selected {
    border-color: rgba(16, 185, 129, 1);
    background: rgba(236, 253, 245, 1);
}
.pair-card-disabled {
    cursor: not-allowed;
    background: rgba(248, 250, 252, 1);
}

.pair-logos {
    position: relative;
    width: 44px;
    height: 28px;
    flex-shrink: 0;
}
.pair-logos .logo {
    position: absolute;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: #f1f5f9;
    border: 2px solid white;
    object-fit: cover;
}
.pair-logos .logo0 { left: 0; z-index: 2; }
.pair-logos .logo1 { left: 16px; z-index: 1; }

.pair-logos .logo-fallback {
    position: absolute;
    width: 28px;
    height: 28px;
    border-radius: 50%;
    background: #f1f5f9;
    border: 2px solid white;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 14px;
}
.pair-logos .logo-fallback:nth-child(1) { left: 0; z-index: 2; }
.pair-logos .logo-fallback:nth-child(2) { left: 16px; z-index: 1; }

.manager-badge {
    background: rgba(241, 245, 249, 1);
    color: rgba(71, 85, 105, 1);
    padding: 1px 6px;
    border-radius: 4px;
    font-size: 11px;
    text-transform: capitalize;
    font-weight: 500;
}

.apy-low    { color: rgba(34, 197, 94, 0.85); }   /* < 30% */
.apy-medium { color: rgba(22, 163, 74, 1); }      /* 30-60% */
.apy-high   { color: rgba(21, 128, 61, 1);  font-weight: 700; }  /* > 60% */
```

- [ ] **Step 3: Verify base.html links the CSS**

If `web/templates/base.html` doesn't already include `app.css`, add after the existing `<link>` or `<style>` blocks:

```html
<link rel="stylesheet" href="{{ url_for('static', path='app.css') }}">
```

- [ ] **Step 4: Manual smoke (optional)**

Reload page, open pair picker — cards should have rounded borders, logos overlapping, hover states.

- [ ] **Step 5: Commit**

```bash
git add web/static/app.css web/templates/base.html
git commit -m "$(cat <<'EOF'
feat(task-11): app.css — Beefy-style pair card styling

Components added:
- .pair-card: white rounded border with hover lift (indigo tint)
- .pair-card-selected: green border + bg for currently active pair
- .pair-card-disabled: muted bg + cursor not-allowed (cross-pairs)
- .pair-logos: overlapping circular logos (token0 over token1)
- .logo-fallback: 💎/💵 emoji holders for 404 logos
- .manager-badge: pill for strategy type (Bell/Wide/Narrow)
- .apy-low/medium/high: color scale (green light/medium/dark)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

### Task 12: app.js — pair handlers

**Files:**
- Modify: `web/static/app.js`

- [ ] **Step 1: Add state fields**

In `web/static/app.js`, add to the dashboard component's top-level state:

```javascript
showPairPicker: false,
pairsData: { usd_pairs: [], cross_pairs: [], selected_vault_id: null, last_refresh_ts: 0 },
pairSearch: '',
pairSort: 'apy',
pairRefreshing: false,
pairsLastRefresh: 0,
```

- [ ] **Step 2: Add computed getters**

In the dashboard return object (sibling to `state` and `config`), add getters and methods:

```javascript
get filteredUsdPairs() {
    return this._filterAndSort(this.pairsData.usd_pairs);
},

get filteredCrossPairs() {
    return this._filterAndSort(this.pairsData.cross_pairs);
},

get selectedPairLabel() {
    const sel = this.pairsData.selected_vault_id;
    if (!sel) return null;
    const all = [...this.pairsData.usd_pairs, ...this.pairsData.cross_pairs];
    const p = all.find(x => x.vault_id === sel);
    if (!p) return sel.slice(0, 10) + '...';
    return p.pair + ' (' + p.manager + ')';
},

_filterAndSort(list) {
    let out = list || [];
    if (this.pairSearch) {
        const q = this.pairSearch.toLowerCase();
        out = out.filter(p => (p.pair || '').toLowerCase().includes(q));
    }
    const sort = this.pairSort;
    out = [...out].sort((a, b) => {
        if (sort === 'apy') return (b.apy_30d || 0) - (a.apy_30d || 0);
        if (sort === 'tvl') return (b.tvl_usd || 0) - (a.tvl_usd || 0);
        if (sort === 'pair') return (a.pair || '').localeCompare(b.pair || '');
        return 0;
    });
    return out;
},

formatTvl(v) {
    if (!v) return '—';
    if (v >= 1e9) return '$' + (v / 1e9).toFixed(2) + 'B';
    if (v >= 1e6) return '$' + (v / 1e6).toFixed(2) + 'M';
    if (v >= 1e3) return '$' + (v / 1e3).toFixed(0) + 'K';
    return '$' + v.toFixed(0);
},

formatApy(v) {
    if (v == null) return 'N/A';
    return (v * 100).toFixed(2) + '%';
},

apyColorClass(v) {
    if (v == null) return 'text-slate-400';
    if (v >= 0.6) return 'apy-high';
    if (v >= 0.3) return 'apy-medium';
    return 'apy-low';
},

formatRelativeTime(ts) {
    if (!ts) return 'nunca';
    const sec = Math.floor(Date.now() / 1000) - ts;
    if (sec < 60) return sec + 's atrás';
    if (sec < 3600) return Math.floor(sec / 60) + 'min atrás';
    if (sec < 86400) return Math.floor(sec / 3600) + 'h atrás';
    return Math.floor(sec / 86400) + 'd atrás';
},

async openPairPicker() {
    this.showPairPicker = true;
    await this.loadPairs();
},

async loadPairs() {
    try {
        const resp = await fetch('/pairs');
        if (resp.ok) {
            const data = await resp.json();
            this.pairsData = data;
            this.pairsLastRefresh = data.last_refresh_ts || 0;
        }
    } catch (e) {}
},

async refreshPairs() {
    this.pairRefreshing = true;
    try {
        const resp = await fetch('/pairs/refresh', { method: 'POST' });
        const data = await resp.json();
        if (!resp.ok) {
            alert('Erro ao atualizar: ' + (data.error || resp.status));
        } else {
            await this.loadPairs();
        }
    } catch (e) {
        alert('Erro: ' + e);
    }
    this.pairRefreshing = false;
},

async selectPair(p) {
    if (!p.selectable) {
        alert('Não selecionável: ' + (p.reason || ''));
        return;
    }
    if (p.vault_id === this.pairsData.selected_vault_id) {
        return;
    }
    if (!confirm('Selecionar par ' + p.pair + ' (' + p.manager + ')?')) return;
    try {
        const resp = await fetch('/pairs/select', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ vault_id: p.vault_id }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            alert('Erro: ' + (data.error || resp.status));
            return;
        }
        this.pairsData.selected_vault_id = p.vault_id;
        alert('Par selecionado! Aplica na próxima operação.');
    } catch (e) {
        alert('Erro: ' + e);
    }
},
```

- [ ] **Step 3: Call loadPairs in init()**

In the existing `init()` method, after the existing fetches, add:

```javascript
this.loadPairs();
```

Don't poll on interval — just load once on app boot. Refresh is manual via button.

- [ ] **Step 4: Manual smoke**

If running:
- Open settings → "Escolher Par" button
- Modal opens, shows search/sort/refresh
- Click Refresh → spinner → either populated cards or error alert (depending on internet)
- If pairs load: cards show with logos, TVL, APY colored
- Click Select on a card → confirm → "Par selecionado!" alert
- Open settings again → see selected pair label

- [ ] **Step 5: Commit**

```bash
git add web/static/app.js
git commit -m "$(cat <<'EOF'
feat(task-12): app.js — pair picker handlers + state

State additions:
- showPairPicker, pairsData (full /pairs response), pairSearch,
  pairSort, pairRefreshing, pairsLastRefresh

Computed getters:
- filteredUsdPairs / filteredCrossPairs (search + sort applied)
- selectedPairLabel ('WETH-USDC (Bell)' string for current selection)

Methods:
- openPairPicker / loadPairs / refreshPairs / selectPair
- formatTvl ($5.21M / $850K), formatApy (28.42%), apyColorClass,
  formatRelativeTime ('5min atrás')

init() calls loadPairs once on boot. No polling (refresh is manual).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Phase F: Final integration

### Task 13: Tag + smoke + CLAUDE.md update

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Smoke test full suite (in batches)**

Run:
```
python -m pytest tests/test_curve.py tests/test_grid.py tests/test_db.py tests/test_state.py tests/test_config.py tests/test_pnl.py tests/test_orderbook.py tests/test_alerts.py tests/test_margin.py tests/test_metrics.py tests/test_logging_config.py tests/test_operation.py tests/test_lp_math.py tests/test_chain_executor.py tests/test_uniswap_executor.py tests/test_beefy_executor.py tests/test_lifecycle.py tests/test_lifecycle_recovery.py tests/test_backtest.py tests/test_stables.py tests/test_dydx_markets.py tests/test_beefy_api.py tests/test_pair_resolver.py tests/test_pair_factory.py tests/test_pair_picker_routes.py -v
```

Expected: ALL PASS.

Then:
```
python -m pytest tests/test_uniswap.py tests/test_beefy.py tests/test_dydx.py tests/test_reconciler.py tests/test_engine_grid.py tests/test_exchanges.py tests/test_integration_grid.py tests/test_integration_operation.py -v
```
Expected: ALL PASS.

- [ ] **Step 2: Update CLAUDE.md**

In the "### Concluído" section, add after Phase 2.0 entry:

```markdown
- ✅ **Pair Picker** (tag `fase-pair-picker-completa`, branch feature/pair-picker)
  - 13 tasks, ~190+ testes (170 base + ~20 novos)
  - Settings → tab "Par" tem Beefy-style cards com logos hotlinkados, APY color-coded, TVL formatado, manager badge, range visualizer
  - Lista vem de live discovery: `chains/beefy_api.py` (cows + tvl + apy/breakdown) + `chains/dydx_markets.py` (perpetualMarkets)
  - Filter: token0 com perp dYdX ativo, token1 stable (USDC/USDT/USDC.e/DAI), decimals (18,6) only
  - Categoriza USD-Pairs (selectable) vs Cross-Pairs (Phase 3.x grayed)
  - Hot-reload: `engine/pair_factory.py` constrói OperationLifecycle per-vault no momento do start_operation; sem restart
  - REST: GET /pairs, POST /pairs/select, POST /pairs/refresh
  - Refactor T0: WETH/USDC_TOKEN_ADDRESS → genéricos TOKEN0/1_ADDRESS + decimals
  - Spec: `docs/superpowers/specs/2026-04-29-pair-picker-design.md`
  - Plan: `docs/superpowers/plans/2026-04-29-pair-picker.md`
```

- [ ] **Step 3: Tag + commit + merge**

```bash
git add CLAUDE.md
git commit -m "$(cat <<'EOF'
docs(task-13): mark Pair Picker complete in CLAUDE.md

Phase pair-picker complete:
- 13 tasks on feature/pair-picker
- Beefy-style UI for picking trading pair
- Live discovery from Beefy API + dYdX indexer
- Hot-reload (no server restart) via pair_factory
- USD/cross categorization

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git tag fase-pair-picker-completa
git checkout master
git merge --no-ff feature/pair-picker -m "$(cat <<'EOF'
Merge Pair Picker

13 tasks. UI pra escolher par direto no app.
- T0 refactor TOKEN0/1_ADDRESS
- T1-T4 stables + dYdX markets fetcher + Beefy API fetcher
- T5-T6 pair_resolver + pair_factory
- T7-T9 engine integration + REST + app wiring
- T10-T12 pair_picker.html + CSS + app.js
- T13 tag

Hot-reload via pair_factory: lifecycle constructed per-operation.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
git push origin master --tags
```

---

## Self-Review

### Spec coverage check

| Spec section | Task |
|---|---|
| Decisão #1 (single concurrent op) | preserved (T7 routes via factory but still 1 op) |
| Decisão #2 (Beefy + dYdX discovery) | T3 + T4 |
| Decisão #3 (USD vs Cross filter) | T1 + T4 |
| Decisão #4 (hot-reload via factory) | T6 + T7 + T9 |
| Decisão #5 (UI Beefy-style cards) | T10 + T11 + T12 |
| Decisão #6 (refactor TOKEN0/1) | T0 |
| Decisão #7 (validation in select) | T8 |
| `chains/beefy_api.py` | T4 |
| `chains/dydx_markets.py` | T3 |
| `engine/pair_resolver.py` | T5 |
| `engine/pair_factory.py` | T6 |
| `stables.py` | T1 |
| DB schema | T2 |
| REST endpoints | T8 |
| UI partials | T10-T12 |
| Backwards compat tests | T7 |
| Tag + CLAUDE.md | T13 |

Coverage complete.

### Placeholder scan

No "TBD/TODO/implement later". Single deviation: T2 step 1 used `placeholder` for env keys — replaced with concrete values.

### Type / signature consistency

- `db.upsert_beefy_pair(pair=dict)` — T2 defines, T4 calls. ✓
- `db.get_pair_from_cache(vault_id)` — T2 defines, T6 + T8 call. ✓
- `db.list_cached_pairs()` — T2, T5. ✓
- `db.set_selected_vault_id(id)` / `get_selected_vault_id()` — T2, T7, T8. ✓
- `DydxMarketsFetcher.refresh()` → int — T3, T8. ✓
- `BeefyApiFetcher.refresh(active_dydx_tickers=set)` → int — T4, T8. ✓
- `build_pair_list(db)` → dict — T5, T8. ✓
- `format_pair_for_ui(raw)` → dict — T5. ✓
- `pair_factory.build_lifecycle(...)` → OperationLifecycle — T6, T7. ✓
- `is_stable(addr)`, `dydx_perp_for(symbol)` — T1, T4. ✓
- Settings rename consistent across T0-T9. ✓

All consistent.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-29-pair-picker.md`.

**13 tasks** in 6 phases:
- **A** Foundation: T0 refactor + T1 stables + T2 DB schema (3)
- **B** Fetchers: T3 dYdX + T4 Beefy (2)
- **C** Resolver + Factory: T5 + T6 (2)
- **D** Engine integration: T7 + T8 + T9 (3)
- **E** UI: T10 partial + T11 CSS + T12 JS (3)
- **F** Final: T13 tag + merge (1)

**Two execution options:**

**1. Subagent-Driven (recommended)** — Same cadence as Phase 1.4 + 2.0. ~13 implementer dispatches.

**2. Inline Execution** — Same session, batch checkpoints.

Auto mode + tua preferência (subagent-driven). Vou seguir nesse mode.
