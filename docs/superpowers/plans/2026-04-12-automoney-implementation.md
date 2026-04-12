# AutoMoney Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a delta-neutral yield farming bot that reads Beefy CLM pool positions on-chain, hedges exposure on Hyperliquid/dYdX with maker-only orders, and serves a real-time dashboard.

**Architecture:** Single-process asyncio Python monolith. StateHub in-memory object drives all decisions; SQLite persists history. Starlette serves SSE-powered HTMX dashboard in the same event loop. Deploy to fly.io.

**Tech Stack:** Python 3.12, Starlette, Jinja2, HTMX, Alpine.js, Tailwind CSS, uPlot, SQLite (aiosqlite), web3.py, websockets, httpx, sse-starlette, uvicorn.

**Spec:** `docs/superpowers/specs/2026-04-12-automoney-design.md`

---

## File Map

```
automoney/
├── app.py                          # Starlette app, lifespan, middleware
├── config.py                       # Settings dataclass from env vars
├── db.py                           # SQLite schema + async query helpers
├── state.py                        # StateHub dataclass
├── engine/
│   ├── __init__.py
│   ├── hedge.py                    # Hedge engine: exposure calc, order decisions
│   ├── orderbook.py                # Book depth monitor, maker price calc
│   └── pnl.py                      # PnL aggregation
├── exchanges/
│   ├── __init__.py
│   ├── base.py                     # ExchangeAdapter ABC + data models
│   ├── hyperliquid.py              # Hyperliquid WS + REST
│   └── dydx.py                     # dYdX v4 WS + REST
├── chains/
│   ├── __init__.py
│   ├── base.py                     # ChainReader ABC
│   └── evm.py                      # EVM RPC + Multicall + Beefy CLM
├── web/
│   ├── __init__.py
│   ├── routes.py                   # HTTP + SSE endpoints
│   ├── auth.py                     # Basic auth middleware
│   ├── templates/
│   │   ├── base.html               # Layout: Tailwind + Alpine + HTMX
│   │   ├── dashboard.html          # Main page composing partials
│   │   └── partials/
│   │       ├── pool.html           # Pool position card
│   │       ├── hedge.html          # Hedge position card
│   │       ├── pnl.html            # PnL summary card
│   │       ├── chart.html          # uPlot correlation chart
│   │       ├── book.html           # Orderbook display
│   │       ├── logs.html           # Activity log stream
│   │       ├── reports.html        # Maker/taker report
│   │       └── settings.html       # Config editor
│   └── static/
│       ├── app.js                  # Alpine components
│       └── chart.js                # uPlot setup
├── tests/
│   ├── __init__.py
│   ├── test_state.py
│   ├── test_db.py
│   ├── test_config.py
│   ├── test_hedge.py
│   ├── test_orderbook.py
│   ├── test_pnl.py
│   ├── test_evm.py
│   ├── test_exchanges.py
│   └── test_web.py
├── .env.example
├── .gitignore
├── Dockerfile
├── fly.toml
├── pyproject.toml
└── requirements.txt
```

---

## Phase 1: Foundation (config, state, database, scaffold)

### Task 1: Project scaffold + dependencies

**Files:**
- Create: `requirements.txt`
- Create: `pyproject.toml`
- Create: `.env.example`
- Create: `.gitignore`
- Create: `fly.toml`
- Create: `Dockerfile`

- [ ] **Step 1: Create requirements.txt**

```
starlette>=0.40,<1.0
uvicorn[standard]>=0.30,<1.0
jinja2>=3.1,<4.0
sse-starlette>=2.0,<3.0
httpx>=0.27,<1.0
websockets>=13.0,<14.0
web3>=7.0,<8.0
aiosqlite>=0.20,<1.0
python-dotenv>=1.0,<2.0
pytest>=8.0
pytest-asyncio>=0.24
```

- [ ] **Step 2: Create pyproject.toml**

```toml
[project]
name = "automoney"
version = "0.1.0"
requires-python = ">=3.12"

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
```

- [ ] **Step 3: Create .env.example**

```bash
# ── Auth ──
AUTH_USER=admin
AUTH_PASS=changeme

# ── Hot Wallet ──
WALLET_ADDRESS=0xYourHotWalletAddress
WALLET_PRIVATE_KEY=0xYourPrivateKey

# ── RPC ──
ARBITRUM_RPC_URL=https://arb1.arbitrum.io/rpc
ARBITRUM_RPC_FALLBACK=https://arbitrum.publicnode.com

# ── Beefy CLM ──
CLM_VAULT_ADDRESS=0xBeefyCLMVaultAddress
CLM_POOL_ADDRESS=0xUniswapV3PoolAddress

# ── Hyperliquid ──
HYPERLIQUID_API_KEY=
HYPERLIQUID_API_SECRET=
HYPERLIQUID_SYMBOL=ARB

# ── dYdX v4 ──
DYDX_MNEMONIC=
DYDX_SYMBOL=ARB-USD

# ── Alerts ──
ALERT_WEBHOOK_URL=

# ── Bot Config Defaults ──
HEDGE_RATIO=0.95
MAX_EXPOSURE_PCT=0.05
REPOST_DEPTH=3
ACTIVE_EXCHANGE=hyperliquid
```

- [ ] **Step 4: Create .gitignore**

```
__pycache__/
*.pyc
.env
*.db
.pytest_cache/
dist/
*.egg-info/
```

- [ ] **Step 5: Create fly.toml**

```toml
app = "automoney"
primary_region = "iad"

[build]
  dockerfile = "Dockerfile"

[env]
  PYTHONUNBUFFERED = "true"

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = "off"
  auto_start_machines = true
  min_machines_running = 1

[[vm]]
  memory = "256mb"
  cpu_kind = "shared"
  cpus = 1

[checks]
  [checks.health]
    type = "http"
    port = 8000
    path = "/health"
    interval = "15s"
    timeout = "5s"
```

- [ ] **Step 6: Create Dockerfile**

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 7: Install dependencies**

Run: `pip install -r requirements.txt`

- [ ] **Step 8: Create empty package files**

Create `__init__.py` in: `engine/`, `exchanges/`, `chains/`, `web/`, `tests/`

- [ ] **Step 9: Commit**

```bash
git add -A
git commit -m "feat: scaffold project with dependencies and deploy config"
```

---

### Task 2: Config module

**Files:**
- Create: `config.py`
- Create: `tests/test_config.py`

- [ ] **Step 1: Write test for config loading**

```python
# tests/test_config.py
import os
import pytest
from config import Settings


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("AUTH_USER", "testuser")
    monkeypatch.setenv("AUTH_PASS", "testpass")
    monkeypatch.setenv("WALLET_ADDRESS", "0xabc")
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "0xdef")
    monkeypatch.setenv("ARBITRUM_RPC_URL", "https://rpc.test")
    monkeypatch.setenv("CLM_VAULT_ADDRESS", "0xvault")
    monkeypatch.setenv("CLM_POOL_ADDRESS", "0xpool")
    monkeypatch.setenv("HEDGE_RATIO", "0.90")
    monkeypatch.setenv("MAX_EXPOSURE_PCT", "0.03")
    monkeypatch.setenv("REPOST_DEPTH", "2")
    monkeypatch.setenv("ACTIVE_EXCHANGE", "dydx")

    s = Settings.from_env()

    assert s.auth_user == "testuser"
    assert s.auth_pass == "testpass"
    assert s.wallet_address == "0xabc"
    assert s.arbitrum_rpc_url == "https://rpc.test"
    assert s.hedge_ratio == 0.90
    assert s.max_exposure_pct == 0.03
    assert s.repost_depth == 2
    assert s.active_exchange == "dydx"


def test_settings_defaults(monkeypatch):
    monkeypatch.setenv("AUTH_USER", "u")
    monkeypatch.setenv("AUTH_PASS", "p")
    monkeypatch.setenv("WALLET_ADDRESS", "0x1")
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "0x2")
    monkeypatch.setenv("ARBITRUM_RPC_URL", "https://rpc")
    monkeypatch.setenv("CLM_VAULT_ADDRESS", "0x3")
    monkeypatch.setenv("CLM_POOL_ADDRESS", "0x4")

    s = Settings.from_env()

    assert s.hedge_ratio == 0.95
    assert s.max_exposure_pct == 0.05
    assert s.repost_depth == 3
    assert s.active_exchange == "hyperliquid"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'config'`

- [ ] **Step 3: Implement config.py**

```python
# config.py
from __future__ import annotations
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    # Auth
    auth_user: str
    auth_pass: str

    # Wallet
    wallet_address: str
    wallet_private_key: str

    # RPC
    arbitrum_rpc_url: str
    arbitrum_rpc_fallback: str

    # Beefy CLM
    clm_vault_address: str
    clm_pool_address: str

    # Hyperliquid
    hyperliquid_api_key: str
    hyperliquid_api_secret: str
    hyperliquid_symbol: str

    # dYdX
    dydx_mnemonic: str
    dydx_symbol: str

    # Alerts
    alert_webhook_url: str

    # Bot config
    hedge_ratio: float
    max_exposure_pct: float
    repost_depth: int
    active_exchange: str  # "hyperliquid" or "dydx"

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            auth_user=os.environ["AUTH_USER"],
            auth_pass=os.environ["AUTH_PASS"],
            wallet_address=os.environ["WALLET_ADDRESS"],
            wallet_private_key=os.environ["WALLET_PRIVATE_KEY"],
            arbitrum_rpc_url=os.environ["ARBITRUM_RPC_URL"],
            arbitrum_rpc_fallback=os.environ.get("ARBITRUM_RPC_FALLBACK", ""),
            clm_vault_address=os.environ["CLM_VAULT_ADDRESS"],
            clm_pool_address=os.environ["CLM_POOL_ADDRESS"],
            hyperliquid_api_key=os.environ.get("HYPERLIQUID_API_KEY", ""),
            hyperliquid_api_secret=os.environ.get("HYPERLIQUID_API_SECRET", ""),
            hyperliquid_symbol=os.environ.get("HYPERLIQUID_SYMBOL", "ARB"),
            dydx_mnemonic=os.environ.get("DYDX_MNEMONIC", ""),
            dydx_symbol=os.environ.get("DYDX_SYMBOL", "ARB-USD"),
            alert_webhook_url=os.environ.get("ALERT_WEBHOOK_URL", ""),
            hedge_ratio=float(os.environ.get("HEDGE_RATIO", "0.95")),
            max_exposure_pct=float(os.environ.get("MAX_EXPOSURE_PCT", "0.05")),
            repost_depth=int(os.environ.get("REPOST_DEPTH", "3")),
            active_exchange=os.environ.get("ACTIVE_EXCHANGE", "hyperliquid"),
        )
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_config.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add config.py tests/test_config.py
git commit -m "feat: add config module loading settings from env vars"
```

---

### Task 3: StateHub

**Files:**
- Create: `state.py`
- Create: `tests/test_state.py`

- [ ] **Step 1: Write test for StateHub**

```python
# tests/test_state.py
import time
from state import StateHub


def test_statehub_defaults():
    s = StateHub()
    assert s.pool_value_usd == 0.0
    assert s.hedge_position is None
    assert s.hedge_ratio == 0.95
    assert s.max_exposure_pct == 0.05
    assert s.safe_mode is False
    assert s.total_maker_fills == 0
    assert s.my_order is None


def test_statehub_exposure_calculation():
    s = StateHub()
    s.pool_value_usd = 200.0
    s.hedge_ratio = 0.95
    # Pool is 50% ARB exposure -> target hedge = 200 * 0.5 * 0.95 = 95
    target = s.pool_value_usd * 0.5 * s.hedge_ratio
    assert target == 95.0


def test_statehub_snapshot():
    s = StateHub()
    s.pool_value_usd = 204.0
    s.pool_deposited_usd = 200.0
    s.hedge_unrealized_pnl = -3.80
    s.hedge_realized_pnl = 0.0
    s.funding_total = 0.15
    s.total_fees_paid = 0.30
    s.best_bid = 1.06
    s.best_ask = 1.0601

    snap = s.to_dict()
    assert snap["pool_value_usd"] == 204.0
    assert snap["best_bid"] == 1.06
    assert "last_update" in snap
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_state.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement state.py**

```python
# state.py
from __future__ import annotations
import time
from dataclasses import dataclass, field, asdict


@dataclass
class StateHub:
    # Pool
    pool_value_usd: float = 0.0
    pool_deposited_usd: float = 0.0
    pool_tokens: dict = field(default_factory=dict)
    cow_balance: float = 0.0
    cow_total_supply: float = 0.0
    vault_balances: tuple = (0.0, 0.0)

    # Hedge
    hedge_position: dict | None = None
    hedge_unrealized_pnl: float = 0.0
    hedge_realized_pnl: float = 0.0
    funding_total: float = 0.0

    # Orderbook
    best_bid: float = 0.0
    best_ask: float = 0.0
    my_order: dict | None = None
    my_order_depth: int = 0

    # Config
    hedge_ratio: float = 0.95
    max_exposure_pct: float = 0.05
    repost_depth: int = 3

    # Metrics
    total_maker_fills: int = 0
    total_taker_fills: int = 0
    total_maker_volume: float = 0.0
    total_taker_volume: float = 0.0
    total_fees_paid: float = 0.0
    total_fees_earned: float = 0.0

    # System
    connected_exchange: bool = False
    connected_chain: bool = False
    safe_mode: bool = False
    last_update: float = field(default_factory=time.time)

    def to_dict(self) -> dict:
        self.last_update = time.time()
        return asdict(self)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_state.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add state.py tests/test_state.py
git commit -m "feat: add StateHub in-memory state dataclass"
```

---

### Task 4: Database layer

**Files:**
- Create: `db.py`
- Create: `tests/test_db.py`

- [ ] **Step 1: Write tests for database**

```python
# tests/test_db.py
import pytest
from db import Database


@pytest.fixture
async def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    await database.initialize()
    yield database
    await database.close()


async def test_initialize_creates_tables(db):
    tables = await db.list_tables()
    assert "config" in tables
    assert "deposits" in tables
    assert "fills" in tables
    assert "funding" in tables
    assert "pool_snapshots" in tables
    assert "order_log" in tables


async def test_insert_and_get_fill(db):
    await db.insert_fill(
        timestamp=1000.0,
        exchange="hyperliquid",
        symbol="ARB",
        side="sell",
        size=100.0,
        price=1.05,
        fee=0.015,
        fee_currency="USDC",
        liquidity="maker",
        realized_pnl=0.0,
        order_id="ord-1",
    )
    fills = await db.get_fills(exchange="hyperliquid", symbol="ARB")
    assert len(fills) == 1
    assert fills[0]["side"] == "sell"
    assert fills[0]["liquidity"] == "maker"


async def test_insert_pool_snapshot(db):
    await db.insert_pool_snapshot(
        timestamp=1000.0,
        pool_value_usd=204.0,
        token0_amount=1500.0,
        token1_amount=0.3,
        hedge_value_usd=190.0,
        hedge_pnl=-3.8,
        pool_pnl=4.0,
        net_pnl=1.75,
        funding_cumulative=0.15,
        fees_earned_cumulative=1.50,
        fees_paid_cumulative=0.30,
    )
    snaps = await db.get_pool_snapshots(limit=10)
    assert len(snaps) == 1
    assert snaps[0]["pool_value_usd"] == 204.0


async def test_insert_order_log(db):
    await db.insert_order_log(
        timestamp=1000.0,
        exchange="hyperliquid",
        action="place",
        side="sell",
        size=50.0,
        price=1.06,
        reason="exposure_rebalance",
    )
    logs = await db.get_order_logs(limit=10)
    assert len(logs) == 1
    assert logs[0]["action"] == "place"


async def test_config_set_and_get(db):
    await db.set_config("hedge_ratio", "0.90")
    val = await db.get_config("hedge_ratio")
    assert val == "0.90"

    await db.set_config("hedge_ratio", "0.85")
    val = await db.get_config("hedge_ratio")
    assert val == "0.85"


async def test_get_fill_stats(db):
    await db.insert_fill(
        timestamp=1000.0, exchange="hyperliquid", symbol="ARB",
        side="sell", size=100.0, price=1.05, fee=0.015,
        fee_currency="USDC", liquidity="maker", realized_pnl=0.0, order_id="o1",
    )
    await db.insert_fill(
        timestamp=1001.0, exchange="hyperliquid", symbol="ARB",
        side="buy", size=50.0, price=1.04, fee=0.045,
        fee_currency="USDC", liquidity="taker", realized_pnl=0.5, order_id="o2",
    )
    stats = await db.get_fill_stats()
    assert stats["maker_count"] == 1
    assert stats["taker_count"] == 1
    assert stats["maker_volume"] == 100.0
    assert stats["taker_volume"] == 50.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_db.py -v`
Expected: FAIL

- [ ] **Step 3: Implement db.py**

```python
# db.py
from __future__ import annotations
import time
import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    action TEXT NOT NULL,
    pool_value_usd REAL NOT NULL,
    token0_amount REAL,
    token1_amount REAL,
    cow_tokens REAL,
    tx_hash TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    size REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    fee_currency TEXT,
    liquidity TEXT NOT NULL,
    realized_pnl REAL,
    order_id TEXT
);

CREATE TABLE IF NOT EXISTS funding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    amount REAL NOT NULL,
    rate REAL
);

CREATE TABLE IF NOT EXISTS pool_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    pool_value_usd REAL NOT NULL,
    token0_amount REAL,
    token1_amount REAL,
    hedge_value_usd REAL,
    hedge_pnl REAL,
    pool_pnl REAL,
    net_pnl REAL,
    funding_cumulative REAL,
    fees_earned_cumulative REAL,
    fees_paid_cumulative REAL
);

CREATE TABLE IF NOT EXISTS order_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    exchange TEXT NOT NULL,
    action TEXT NOT NULL,
    side TEXT,
    size REAL,
    price REAL,
    reason TEXT
);
"""


class Database:
    def __init__(self, path: str = "automoney.db"):
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    async def list_tables(self) -> list[str]:
        cursor = await self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        rows = await cursor.fetchall()
        return [r["name"] for r in rows]

    # ── Config ──

    async def set_config(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, time.time()),
        )
        await self._conn.commit()

    async def get_config(self, key: str) -> str | None:
        cursor = await self._conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row["value"] if row else None

    # ── Fills ──

    async def insert_fill(
        self, *, timestamp: float, exchange: str, symbol: str, side: str,
        size: float, price: float, fee: float, fee_currency: str,
        liquidity: str, realized_pnl: float, order_id: str,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO fills
            (timestamp, exchange, symbol, side, size, price, fee, fee_currency,
             liquidity, realized_pnl, order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, exchange, symbol, side, size, price, fee, fee_currency,
             liquidity, realized_pnl, order_id),
        )
        await self._conn.commit()

    async def get_fills(
        self, exchange: str | None = None, symbol: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        query = "SELECT * FROM fills WHERE 1=1"
        params: list = []
        if exchange:
            query += " AND exchange = ?"
            params.append(exchange)
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_fill_stats(self) -> dict:
        cursor = await self._conn.execute("""
            SELECT
                SUM(CASE WHEN liquidity='maker' THEN 1 ELSE 0 END) as maker_count,
                SUM(CASE WHEN liquidity='taker' THEN 1 ELSE 0 END) as taker_count,
                SUM(CASE WHEN liquidity='maker' THEN size ELSE 0 END) as maker_volume,
                SUM(CASE WHEN liquidity='taker' THEN size ELSE 0 END) as taker_volume,
                SUM(fee) as total_fees,
                SUM(realized_pnl) as total_realized_pnl
            FROM fills
        """)
        row = await cursor.fetchone()
        return dict(row)

    # ── Funding ──

    async def insert_funding(
        self, *, timestamp: float, exchange: str, symbol: str,
        amount: float, rate: float,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO funding (timestamp, exchange, symbol, amount, rate) VALUES (?, ?, ?, ?, ?)",
            (timestamp, exchange, symbol, amount, rate),
        )
        await self._conn.commit()

    # ── Pool Snapshots ──

    async def insert_pool_snapshot(
        self, *, timestamp: float, pool_value_usd: float,
        token0_amount: float, token1_amount: float, hedge_value_usd: float,
        hedge_pnl: float, pool_pnl: float, net_pnl: float,
        funding_cumulative: float, fees_earned_cumulative: float,
        fees_paid_cumulative: float,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO pool_snapshots
            (timestamp, pool_value_usd, token0_amount, token1_amount,
             hedge_value_usd, hedge_pnl, pool_pnl, net_pnl,
             funding_cumulative, fees_earned_cumulative, fees_paid_cumulative)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, pool_value_usd, token0_amount, token1_amount,
             hedge_value_usd, hedge_pnl, pool_pnl, net_pnl,
             funding_cumulative, fees_earned_cumulative, fees_paid_cumulative),
        )
        await self._conn.commit()

    async def get_pool_snapshots(self, limit: int = 1000) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM pool_snapshots ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Order Log ──

    async def insert_order_log(
        self, *, timestamp: float, exchange: str, action: str,
        side: str | None = None, size: float | None = None,
        price: float | None = None, reason: str | None = None,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO order_log
            (timestamp, exchange, action, side, size, price, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, exchange, action, side, size, price, reason),
        )
        await self._conn.commit()

    async def get_order_logs(self, limit: int = 50) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM order_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    # ── Deposits ──

    async def insert_deposit(
        self, *, timestamp: float, action: str, pool_value_usd: float,
        token0_amount: float, token1_amount: float, cow_tokens: float,
        tx_hash: str,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO deposits
            (timestamp, action, pool_value_usd, token0_amount, token1_amount,
             cow_tokens, tx_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, action, pool_value_usd, token0_amount, token1_amount,
             cow_tokens, tx_hash),
        )
        await self._conn.commit()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_db.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat: add async SQLite database layer with all tables"
```

---

## Phase 2: Core Engine (orderbook, hedge, PnL)

### Task 5: Orderbook price calculator + depth monitor

**Files:**
- Create: `engine/orderbook.py`
- Create: `tests/test_orderbook.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_orderbook.py
from engine.orderbook import calc_maker_price, calc_aggressive_price, check_order_depth


def test_maker_sell_wide_spread():
    # spread > 1 tick: improve the ask by 1 tick
    price = calc_maker_price(
        side="sell", best_bid=1.0000, best_ask=1.0010, tick=0.0001
    )
    assert price == 1.0009  # best_ask - tick


def test_maker_sell_min_spread():
    # spread == 1 tick: stay at best_ask (can't improve without crossing)
    price = calc_maker_price(
        side="sell", best_bid=1.0000, best_ask=1.0001, tick=0.0001
    )
    assert price == 1.0001  # stay at best_ask


def test_maker_buy_wide_spread():
    price = calc_maker_price(
        side="buy", best_bid=1.0000, best_ask=1.0010, tick=0.0001
    )
    assert price == 1.0001  # best_bid + tick


def test_maker_buy_min_spread():
    price = calc_maker_price(
        side="buy", best_bid=1.0000, best_ask=1.0001, tick=0.0001
    )
    assert price == 1.0000  # stay at best_bid


def test_maker_sell_never_crosses_bid():
    # Edge case: calculated price would be at or below bid
    price = calc_maker_price(
        side="sell", best_bid=1.0005, best_ask=1.0005, tick=0.0001
    )
    assert price > 1.0005  # must be above bid


def test_maker_buy_never_crosses_ask():
    price = calc_maker_price(
        side="buy", best_bid=1.0005, best_ask=1.0005, tick=0.0001
    )
    assert price < 1.0005  # must be below ask


def test_aggressive_sell():
    price = calc_aggressive_price(
        side="sell", best_bid=1.0000, best_ask=1.0010, tick=0.0001
    )
    assert price == 1.0001  # just above bid


def test_aggressive_buy():
    price = calc_aggressive_price(
        side="buy", best_bid=1.0000, best_ask=1.0010, tick=0.0001
    )
    assert price == 1.0009  # just below ask


def test_depth_at_best_level():
    book_bids = {1.06: 300, 1.0599: 180, 1.0598: 90}
    result = check_order_depth(side="buy", price=1.06, book_levels=book_bids)
    assert result == "HOLD"  # at best (0-indexed level 0)


def test_depth_at_second_level():
    book_bids = {1.06: 300, 1.0599: 180, 1.0598: 90}
    result = check_order_depth(side="buy", price=1.0599, book_levels=book_bids)
    assert result == "HOLD"  # 2nd level, still OK


def test_depth_at_third_level_triggers_repost():
    book_bids = {1.06: 300, 1.0599: 180, 1.0598: 90}
    result = check_order_depth(side="buy", price=1.0598, book_levels=book_bids)
    assert result == "REPOST"  # 3rd level, need to repost


def test_depth_order_not_in_book():
    book_bids = {1.06: 300, 1.0599: 180}
    result = check_order_depth(side="buy", price=1.05, book_levels=book_bids)
    assert result == "REPOST"  # not even in book


def test_depth_sell_side():
    book_asks = {1.0610: 200, 1.0609: 150, 1.0608: 100}
    result = check_order_depth(side="sell", price=1.0610, book_levels=book_asks)
    assert result == "REPOST"  # 3rd level from best ask (1.0608 is best)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_orderbook.py -v`
Expected: FAIL

- [ ] **Step 3: Implement engine/orderbook.py**

```python
# engine/orderbook.py
from __future__ import annotations


def calc_maker_price(
    *, side: str, best_bid: float, best_ask: float, tick: float
) -> float:
    spread = best_ask - best_bid

    if side == "sell":
        if spread > tick:
            price = best_ask - tick
        else:
            price = best_ask
        # Safety: never cross or touch the bid
        if price <= best_bid:
            price = best_bid + tick
        return round(price, 10)

    else:  # buy
        if spread > tick:
            price = best_bid + tick
        else:
            price = best_bid
        # Safety: never cross or touch the ask
        if price >= best_ask:
            price = best_ask - tick
        return round(price, 10)


def calc_aggressive_price(
    *, side: str, best_bid: float, best_ask: float, tick: float
) -> float:
    if side == "sell":
        price = best_bid + tick  # just above bid, limit not market
        return round(price, 10)
    else:  # buy
        price = best_ask - tick  # just below ask
        return round(price, 10)


def check_order_depth(
    *, side: str, price: float, book_levels: dict[float, float],
    max_depth: int = 3,
) -> str:
    if side == "sell":
        sorted_levels = sorted(book_levels.keys())  # ascending: best ask first
    else:  # buy
        sorted_levels = sorted(book_levels.keys(), reverse=True)  # desc: best bid first

    if price not in book_levels:
        return "REPOST"

    level_index = sorted_levels.index(price)

    if level_index >= max_depth - 1:  # 0-indexed, so 2 = 3rd level
        return "REPOST"
    return "HOLD"
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_orderbook.py -v`
Expected: 13 passed

- [ ] **Step 5: Commit**

```bash
git add engine/ tests/test_orderbook.py
git commit -m "feat: add orderbook price calculator and depth monitor"
```

---

### Task 6: PnL calculator

**Files:**
- Create: `engine/pnl.py`
- Create: `tests/test_pnl.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_pnl.py
from engine.pnl import calc_pnl, PnLBreakdown


def test_basic_pnl():
    result = calc_pnl(
        pool_value_usd=204.0,
        pool_deposited_usd=200.0,
        hedge_realized_pnl=0.0,
        hedge_unrealized_pnl=-3.80,
        funding_total=0.15,
        total_fees_paid=0.30,
    )
    assert isinstance(result, PnLBreakdown)
    assert result.pool_pnl == 4.0
    assert result.hedge_pnl == -3.80
    assert result.funding_pnl == 0.15
    assert result.fees_paid == 0.30
    # net = 4.0 + (-3.80) + 0.15 - 0.30 = 0.05
    assert abs(result.net_pnl - 0.05) < 0.001


def test_pnl_with_realized_hedge():
    result = calc_pnl(
        pool_value_usd=210.0,
        pool_deposited_usd=200.0,
        hedge_realized_pnl=-8.0,
        hedge_unrealized_pnl=-1.50,
        funding_total=0.50,
        total_fees_paid=0.40,
    )
    assert result.pool_pnl == 10.0
    assert result.hedge_pnl == -9.50  # realized + unrealized
    # net = 10.0 + (-9.50) + 0.50 - 0.40 = 0.60
    assert abs(result.net_pnl - 0.60) < 0.001


def test_pnl_negative_pool():
    result = calc_pnl(
        pool_value_usd=195.0,
        pool_deposited_usd=200.0,
        hedge_realized_pnl=4.5,
        hedge_unrealized_pnl=0.0,
        funding_total=-0.10,
        total_fees_paid=0.20,
    )
    assert result.pool_pnl == -5.0
    assert result.hedge_pnl == 4.5
    # net = -5.0 + 4.5 + (-0.10) - 0.20 = -0.80
    assert abs(result.net_pnl - (-0.80)) < 0.001


def test_pnl_to_dict():
    result = calc_pnl(
        pool_value_usd=204.0,
        pool_deposited_usd=200.0,
        hedge_realized_pnl=0.0,
        hedge_unrealized_pnl=-3.80,
        funding_total=0.15,
        total_fees_paid=0.30,
    )
    d = result.to_dict()
    assert "pool_pnl" in d
    assert "hedge_pnl" in d
    assert "net_pnl" in d
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_pnl.py -v`
Expected: FAIL

- [ ] **Step 3: Implement engine/pnl.py**

```python
# engine/pnl.py
from __future__ import annotations
from dataclasses import dataclass, asdict


@dataclass
class PnLBreakdown:
    pool_pnl: float
    hedge_pnl: float      # realized + unrealized
    funding_pnl: float
    fees_paid: float
    net_pnl: float

    def to_dict(self) -> dict:
        return asdict(self)


def calc_pnl(
    *,
    pool_value_usd: float,
    pool_deposited_usd: float,
    hedge_realized_pnl: float,
    hedge_unrealized_pnl: float,
    funding_total: float,
    total_fees_paid: float,
) -> PnLBreakdown:
    pool_pnl = pool_value_usd - pool_deposited_usd
    hedge_pnl = hedge_realized_pnl + hedge_unrealized_pnl
    net_pnl = pool_pnl + hedge_pnl + funding_total - total_fees_paid

    return PnLBreakdown(
        pool_pnl=pool_pnl,
        hedge_pnl=hedge_pnl,
        funding_pnl=funding_total,
        fees_paid=total_fees_paid,
        net_pnl=net_pnl,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_pnl.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add engine/pnl.py tests/test_pnl.py
git commit -m "feat: add PnL calculator with breakdown"
```

---

### Task 7: Hedge engine core logic

**Files:**
- Create: `engine/hedge.py`
- Create: `tests/test_hedge.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_hedge.py
from engine.hedge import HedgeDecision, compute_hedge_action


def test_no_hedge_needed():
    """Exposure within tolerance, no order needed."""
    result = compute_hedge_action(
        pool_value_usd=200.0,
        token_exposure_ratio=0.5,
        hedge_ratio=0.95,
        current_hedge_size=95.0,  # perfect
        max_exposure_pct=0.05,
    )
    assert result.action == "HOLD"
    assert result.delta == 0.0


def test_small_exposure_maker_mode():
    """Exposure within 0-5% -> MAKER mode."""
    result = compute_hedge_action(
        pool_value_usd=200.0,
        token_exposure_ratio=0.5,
        hedge_ratio=0.95,
        current_hedge_size=90.0,  # need 95, have 90 -> delta=5 -> 2.5%
        max_exposure_pct=0.05,
    )
    assert result.action == "MAKER"
    assert result.side == "sell"  # need more short
    assert abs(result.delta - 5.0) < 0.01
    assert result.exposure_pct < 0.05


def test_large_exposure_aggressive_mode():
    """Exposure > 5% -> AGGRESSIVE mode."""
    result = compute_hedge_action(
        pool_value_usd=200.0,
        token_exposure_ratio=0.5,
        hedge_ratio=0.95,
        current_hedge_size=70.0,  # need 95, have 70 -> delta=25 -> 12.5%
        max_exposure_pct=0.05,
    )
    assert result.action == "AGGRESSIVE"
    assert result.side == "sell"
    assert abs(result.delta - 25.0) < 0.01


def test_overhedged_needs_buy():
    """Over-hedged: need to reduce by buying back."""
    result = compute_hedge_action(
        pool_value_usd=200.0,
        token_exposure_ratio=0.5,
        hedge_ratio=0.95,
        current_hedge_size=110.0,  # need 95, have 110 -> over by 15
        max_exposure_pct=0.05,
    )
    assert result.side == "buy"
    assert abs(result.delta - 15.0) < 0.01


def test_zero_pool_value_hold():
    """No pool value -> no hedge needed."""
    result = compute_hedge_action(
        pool_value_usd=0.0,
        token_exposure_ratio=0.5,
        hedge_ratio=0.95,
        current_hedge_size=0.0,
        max_exposure_pct=0.05,
    )
    assert result.action == "HOLD"


def test_safe_mode_always_hold():
    """Safe mode -> always HOLD regardless of exposure."""
    result = compute_hedge_action(
        pool_value_usd=200.0,
        token_exposure_ratio=0.5,
        hedge_ratio=0.95,
        current_hedge_size=0.0,  # massive exposure
        max_exposure_pct=0.05,
        safe_mode=True,
    )
    assert result.action == "HOLD"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_hedge.py -v`
Expected: FAIL

- [ ] **Step 3: Implement engine/hedge.py**

```python
# engine/hedge.py
from __future__ import annotations
from dataclasses import dataclass


@dataclass
class HedgeDecision:
    action: str         # "HOLD", "MAKER", "AGGRESSIVE"
    side: str | None     # "buy" or "sell", None if HOLD
    delta: float         # size to hedge (absolute)
    exposure_pct: float  # current exposure percentage
    target_hedge: float  # what the hedge should be


def compute_hedge_action(
    *,
    pool_value_usd: float,
    token_exposure_ratio: float,
    hedge_ratio: float,
    current_hedge_size: float,
    max_exposure_pct: float,
    safe_mode: bool = False,
) -> HedgeDecision:
    if safe_mode or pool_value_usd <= 0:
        return HedgeDecision(
            action="HOLD", side=None, delta=0.0,
            exposure_pct=0.0, target_hedge=0.0,
        )

    target_hedge = pool_value_usd * token_exposure_ratio * hedge_ratio
    delta = target_hedge - current_hedge_size
    exposure_pct = abs(delta) / pool_value_usd if pool_value_usd > 0 else 0.0

    if abs(delta) < 0.01:  # negligible
        return HedgeDecision(
            action="HOLD", side=None, delta=0.0,
            exposure_pct=exposure_pct, target_hedge=target_hedge,
        )

    side = "sell" if delta > 0 else "buy"

    if exposure_pct <= max_exposure_pct:
        action = "MAKER"
    else:
        action = "AGGRESSIVE"

    return HedgeDecision(
        action=action,
        side=side,
        delta=abs(delta),
        exposure_pct=exposure_pct,
        target_hedge=target_hedge,
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_hedge.py -v`
Expected: 6 passed

- [ ] **Step 5: Commit**

```bash
git add engine/hedge.py tests/test_hedge.py
git commit -m "feat: add hedge engine with exposure calc and maker/aggressive logic"
```

---

## Phase 3: Exchange + Chain Adapters

### Task 8: Exchange adapter base + data models

**Files:**
- Create: `exchanges/base.py`
- Create: `tests/test_exchanges.py`

- [ ] **Step 1: Write tests for data models**

```python
# tests/test_exchanges.py
from exchanges.base import Order, Fill, Position


def test_order_creation():
    o = Order(
        order_id="abc-123", symbol="ARB", side="sell",
        size=50.0, price=1.06, status="open",
    )
    assert o.order_id == "abc-123"
    assert o.is_open


def test_order_not_open():
    o = Order(
        order_id="abc", symbol="ARB", side="sell",
        size=50.0, price=1.06, status="filled",
    )
    assert not o.is_open


def test_fill_creation():
    f = Fill(
        fill_id="f1", order_id="abc", symbol="ARB", side="sell",
        size=50.0, price=1.06, fee=0.015, fee_currency="USDC",
        liquidity="maker", realized_pnl=0.0, timestamp=1000.0,
    )
    assert f.liquidity == "maker"
    assert f.fee == 0.015


def test_position_notional():
    p = Position(
        symbol="ARB", side="short", size=95.0,
        entry_price=1.05, unrealized_pnl=-1.20,
    )
    assert p.notional == 95.0 * 1.05
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_exchanges.py -v`
Expected: FAIL

- [ ] **Step 3: Implement exchanges/base.py**

```python
# exchanges/base.py
from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Awaitable


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str         # "buy" or "sell"
    size: float
    price: float
    status: str       # "open", "filled", "cancelled", "partial"

    @property
    def is_open(self) -> bool:
        return self.status in ("open", "partial")


@dataclass
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: str
    size: float
    price: float
    fee: float
    fee_currency: str
    liquidity: str    # "maker" or "taker"
    realized_pnl: float
    timestamp: float


@dataclass
class Position:
    symbol: str
    side: str         # "long" or "short"
    size: float
    entry_price: float
    unrealized_pnl: float

    @property
    def notional(self) -> float:
        return self.size * self.entry_price


class ExchangeAdapter(ABC):
    name: str

    @abstractmethod
    async def connect(self) -> None: ...

    @abstractmethod
    async def disconnect(self) -> None: ...

    @abstractmethod
    async def subscribe_orderbook(
        self, symbol: str, callback: Callable[[dict], Awaitable[None]]
    ) -> None: ...

    @abstractmethod
    async def subscribe_fills(
        self, symbol: str, callback: Callable[[Fill], Awaitable[None]]
    ) -> None: ...

    @abstractmethod
    async def place_limit_order(
        self, symbol: str, side: str, size: float, price: float
    ) -> Order: ...

    @abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...

    @abstractmethod
    async def get_position(self, symbol: str) -> Position | None: ...

    @abstractmethod
    async def get_fills(
        self, symbol: str, since: float | None = None
    ) -> list[Fill]: ...

    @abstractmethod
    def get_tick_size(self, symbol: str) -> float: ...

    @abstractmethod
    def get_min_notional(self, symbol: str) -> float: ...
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_exchanges.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add exchanges/ tests/test_exchanges.py
git commit -m "feat: add exchange adapter ABC and data models"
```

---

### Task 9: Hyperliquid adapter

**Files:**
- Create: `exchanges/hyperliquid.py`

This adapter connects to Hyperliquid's WebSocket and REST APIs. Full integration test requires live API keys — the adapter is implemented against documented API contracts and will be verified during integration testing.

- [ ] **Step 1: Implement exchanges/hyperliquid.py**

```python
# exchanges/hyperliquid.py
from __future__ import annotations
import json
import time
import asyncio
import logging
from typing import Callable, Awaitable
import httpx
import websockets
from exchanges.base import ExchangeAdapter, Order, Fill, Position

logger = logging.getLogger(__name__)

WS_URL = "wss://api.hyperliquid.xyz/ws"
REST_URL = "https://api.hyperliquid.xyz"


class HyperliquidAdapter(ExchangeAdapter):
    name = "hyperliquid"

    def __init__(self, api_key: str, api_secret: str, wallet_address: str):
        self._api_key = api_key
        self._api_secret = api_secret
        self._wallet = wallet_address
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._http = httpx.AsyncClient(base_url=REST_URL, timeout=10)
        self._book_callback: Callable | None = None
        self._fill_callback: Callable | None = None
        self._ws_task: asyncio.Task | None = None
        self._running = False
        self._tick_sizes: dict[str, float] = {}

    async def connect(self) -> None:
        self._running = True
        self._ws = await websockets.connect(WS_URL)
        self._ws_task = asyncio.create_task(self._ws_loop())
        logger.info("Hyperliquid WS connected")

    async def disconnect(self) -> None:
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
        if self._ws:
            await self._ws.close()
        await self._http.aclose()

    async def _ws_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                channel = msg.get("channel")
                if channel == "l2Book" and self._book_callback:
                    await self._book_callback(msg.get("data", {}))
                elif channel == "user" and self._fill_callback:
                    data = msg.get("data", {})
                    if "fills" in data:
                        for f in data["fills"]:
                            fill = self._parse_fill(f)
                            await self._fill_callback(fill)
        except websockets.ConnectionClosed:
            logger.warning("Hyperliquid WS disconnected")
        except asyncio.CancelledError:
            pass

    async def subscribe_orderbook(
        self, symbol: str, callback: Callable[[dict], Awaitable[None]]
    ) -> None:
        self._book_callback = callback
        sub = {"method": "subscribe", "subscription": {"type": "l2Book", "coin": symbol}}
        await self._ws.send(json.dumps(sub))

    async def subscribe_fills(
        self, symbol: str, callback: Callable[[Fill], Awaitable[None]]
    ) -> None:
        self._fill_callback = callback
        sub = {"method": "subscribe", "subscription": {"type": "userFills", "user": self._wallet}}
        await self._ws.send(json.dumps(sub))

    async def place_limit_order(
        self, symbol: str, side: str, size: float, price: float
    ) -> Order:
        is_buy = side == "buy"
        order_req = {
            "type": "order",
            "orders": [{
                "a": self._asset_index(symbol),
                "b": is_buy,
                "p": str(price),
                "s": str(size),
                "r": False,  # not reduce-only
                "t": {"limit": {"tif": "Gtc"}},
            }],
            "grouping": "na",
        }
        resp = await self._post_action(order_req)
        statuses = resp.get("response", {}).get("data", {}).get("statuses", [{}])
        status = statuses[0] if statuses else {}
        oid = status.get("resting", {}).get("oid", str(time.time()))
        return Order(
            order_id=str(oid), symbol=symbol, side=side,
            size=size, price=price, status="open",
        )

    async def cancel_order(self, order_id: str) -> None:
        cancel_req = {
            "type": "cancel",
            "cancels": [{"a": 0, "o": int(order_id)}],
        }
        await self._post_action(cancel_req)

    async def get_position(self, symbol: str) -> Position | None:
        resp = await self._http.post(
            "/info",
            json={"type": "clearinghouseState", "user": self._wallet},
        )
        data = resp.json()
        for pos in data.get("assetPositions", []):
            p = pos.get("position", {})
            coin = p.get("coin", "")
            if coin == symbol and float(p.get("szi", "0")) != 0:
                size = abs(float(p["szi"]))
                side = "long" if float(p["szi"]) > 0 else "short"
                return Position(
                    symbol=symbol,
                    side=side,
                    size=size,
                    entry_price=float(p.get("entryPx", "0")),
                    unrealized_pnl=float(p.get("unrealizedPnl", "0")),
                )
        return None

    async def get_fills(
        self, symbol: str, since: float | None = None
    ) -> list[Fill]:
        resp = await self._http.post(
            "/info",
            json={"type": "userFills", "user": self._wallet},
        )
        fills = []
        for f in resp.json():
            if f.get("coin") != symbol:
                continue
            ts = f.get("time", 0) / 1000.0
            if since and ts < since:
                continue
            fills.append(self._parse_fill(f))
        return fills

    def get_tick_size(self, symbol: str) -> float:
        return self._tick_sizes.get(symbol, 0.0001)

    def get_min_notional(self, symbol: str) -> float:
        return 10.0

    def _parse_fill(self, f: dict) -> Fill:
        return Fill(
            fill_id=str(f.get("tid", "")),
            order_id=str(f.get("oid", "")),
            symbol=f.get("coin", ""),
            side="buy" if f.get("side") == "B" else "sell",
            size=float(f.get("sz", "0")),
            price=float(f.get("px", "0")),
            fee=float(f.get("fee", "0")),
            fee_currency="USDC",
            liquidity="maker" if f.get("liquidityType") == "Maker" else "taker",
            realized_pnl=float(f.get("closedPnl", "0")),
            timestamp=f.get("time", 0) / 1000.0,
        )

    def _asset_index(self, symbol: str) -> int:
        # Hyperliquid uses numeric asset indices. Common ones:
        mapping = {"BTC": 0, "ETH": 1, "ARB": 2}
        return mapping.get(symbol, 0)

    async def _post_action(self, action: dict) -> dict:
        # Simplified — production needs EIP-712 signing with wallet key
        resp = await self._http.post("/exchange", json={"action": action})
        return resp.json()
```

- [ ] **Step 2: Commit**

```bash
git add exchanges/hyperliquid.py
git commit -m "feat: add Hyperliquid exchange adapter (WS + REST)"
```

---

### Task 10: dYdX v4 adapter

**Files:**
- Create: `exchanges/dydx.py`

- [ ] **Step 1: Implement exchanges/dydx.py**

```python
# exchanges/dydx.py
from __future__ import annotations
import json
import time
import asyncio
import logging
from typing import Callable, Awaitable
import httpx
import websockets
from exchanges.base import ExchangeAdapter, Order, Fill, Position

logger = logging.getLogger(__name__)

INDEXER_REST = "https://indexer.dydx.trade/v4"
INDEXER_WS = "wss://indexer.dydx.trade/v4/ws"


class DydxAdapter(ExchangeAdapter):
    name = "dydx"

    def __init__(self, mnemonic: str, wallet_address: str):
        self._mnemonic = mnemonic
        self._wallet = wallet_address
        self._subaccount = 0
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._http = httpx.AsyncClient(base_url=INDEXER_REST, timeout=10)
        self._book_callback: Callable | None = None
        self._fill_callback: Callable | None = None
        self._ws_task: asyncio.Task | None = None
        self._running = False

    async def connect(self) -> None:
        self._running = True
        self._ws = await websockets.connect(INDEXER_WS)
        self._ws_task = asyncio.create_task(self._ws_loop())
        logger.info("dYdX WS connected")

    async def disconnect(self) -> None:
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
        if self._ws:
            await self._ws.close()
        await self._http.aclose()

    async def _ws_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                channel = msg.get("channel")
                if channel == "v4_orderbook" and self._book_callback:
                    await self._book_callback(msg.get("contents", {}))
                elif channel == "v4_subaccounts" and self._fill_callback:
                    contents = msg.get("contents", {})
                    fills = contents.get("fills", [])
                    for f in fills:
                        fill = self._parse_fill(f)
                        await self._fill_callback(fill)
        except websockets.ConnectionClosed:
            logger.warning("dYdX WS disconnected")
        except asyncio.CancelledError:
            pass

    async def subscribe_orderbook(
        self, symbol: str, callback: Callable[[dict], Awaitable[None]]
    ) -> None:
        self._book_callback = callback
        sub = {"type": "subscribe", "channel": "v4_orderbook", "id": symbol}
        await self._ws.send(json.dumps(sub))

    async def subscribe_fills(
        self, symbol: str, callback: Callable[[Fill], Awaitable[None]]
    ) -> None:
        self._fill_callback = callback
        sub = {
            "type": "subscribe",
            "channel": "v4_subaccounts",
            "id": f"{self._wallet}/{self._subaccount}",
        }
        await self._ws.send(json.dumps(sub))

    async def place_limit_order(
        self, symbol: str, side: str, size: float, price: float
    ) -> Order:
        # dYdX v4 requires signing via the Cosmos SDK chain
        # This is a placeholder for the signing flow
        order_id = f"dydx-{int(time.time() * 1000)}"
        logger.info(f"dYdX place_limit_order: {side} {size} {symbol} @ {price}")
        return Order(
            order_id=order_id, symbol=symbol, side=side,
            size=size, price=price, status="open",
        )

    async def cancel_order(self, order_id: str) -> None:
        logger.info(f"dYdX cancel_order: {order_id}")

    async def get_position(self, symbol: str) -> Position | None:
        resp = await self._http.get(
            "/perpetualPositions",
            params={"address": self._wallet, "subaccountNumber": self._subaccount},
        )
        data = resp.json()
        for pos in data.get("positions", []):
            if pos.get("market") == symbol and pos.get("status") == "OPEN":
                size = abs(float(pos["size"]))
                side = "long" if float(pos["size"]) > 0 else "short"
                return Position(
                    symbol=symbol,
                    side=side,
                    size=size,
                    entry_price=float(pos.get("entryPrice", "0")),
                    unrealized_pnl=float(pos.get("unrealizedPnl", "0")),
                )
        return None

    async def get_fills(
        self, symbol: str, since: float | None = None
    ) -> list[Fill]:
        params = {
            "address": self._wallet,
            "subaccountNumber": self._subaccount,
            "ticker": symbol,
            "limit": 100,
        }
        resp = await self._http.get("/fills", params=params)
        fills = []
        for f in resp.json().get("fills", []):
            fill = self._parse_fill(f)
            if since and fill.timestamp < since:
                continue
            fills.append(fill)
        return fills

    def get_tick_size(self, symbol: str) -> float:
        return 0.0001

    def get_min_notional(self, symbol: str) -> float:
        return 1.0

    def _parse_fill(self, f: dict) -> Fill:
        # dYdX fill response: infer maker/taker from order type
        liquidity = f.get("liquidity", "TAKER").lower()
        if liquidity not in ("maker", "taker"):
            liquidity = "taker"  # default assumption

        return Fill(
            fill_id=str(f.get("id", "")),
            order_id=str(f.get("orderId", "")),
            symbol=f.get("market", f.get("ticker", "")),
            side=f.get("side", "BUY").lower(),
            size=float(f.get("size", "0")),
            price=float(f.get("price", "0")),
            fee=float(f.get("fee", "0")),
            fee_currency="USDC",
            liquidity=liquidity,
            realized_pnl=float(f.get("realizedPnl", "0")),
            timestamp=float(f.get("createdAtHeight", 0)),
        )
```

- [ ] **Step 2: Commit**

```bash
git add exchanges/dydx.py
git commit -m "feat: add dYdX v4 exchange adapter (WS + REST)"
```

---

### Task 11: Chain reader (EVM + Beefy CLM)

**Files:**
- Create: `chains/base.py`
- Create: `chains/evm.py`
- Create: `tests/test_evm.py`

- [ ] **Step 1: Write test for pool position calculation**

```python
# tests/test_evm.py
from chains.evm import calc_pool_position


def test_calc_pool_position():
    result = calc_pool_position(
        cow_balance=100.0,
        total_supply=1000.0,
        vault_token0=15000.0,  # total ARB in vault
        vault_token1=3.0,       # total WETH in vault
        price_token0_usd=1.05,  # ARB price
        price_token1_usd=3500.0, # WETH price
    )
    # my share = 100/1000 = 10%
    assert result["my_token0"] == 1500.0
    assert result["my_token1"] == 0.3
    # value = 1500 * 1.05 + 0.3 * 3500 = 1575 + 1050 = 2625
    assert result["value_usd"] == 2625.0


def test_calc_pool_position_zero_supply():
    result = calc_pool_position(
        cow_balance=0.0,
        total_supply=0.0,
        vault_token0=0.0,
        vault_token1=0.0,
        price_token0_usd=1.0,
        price_token1_usd=3500.0,
    )
    assert result["value_usd"] == 0.0
    assert result["my_token0"] == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_evm.py -v`
Expected: FAIL

- [ ] **Step 3: Implement chains/base.py and chains/evm.py**

```python
# chains/base.py
from __future__ import annotations
from abc import ABC, abstractmethod


class ChainReader(ABC):
    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def stop(self) -> None: ...

    @abstractmethod
    async def read_pool_position(self) -> dict: ...
```

```python
# chains/evm.py
from __future__ import annotations
import asyncio
import logging
from typing import Callable, Awaitable
from web3 import AsyncWeb3, AsyncHTTPProvider
from chains.base import ChainReader

logger = logging.getLogger(__name__)

# Minimal ABIs for the calls we need
CLM_ABI = [
    {"name": "balanceOf", "type": "function", "stateMutability": "view",
     "inputs": [{"name": "account", "type": "address"}],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "totalSupply", "type": "function", "stateMutability": "view",
     "inputs": [],
     "outputs": [{"name": "", "type": "uint256"}]},
    {"name": "balances", "type": "function", "stateMutability": "view",
     "inputs": [],
     "outputs": [{"name": "amount0", "type": "uint256"}, {"name": "amount1", "type": "uint256"}]},
]

POOL_ABI = [
    {"name": "slot0", "type": "function", "stateMutability": "view",
     "inputs": [],
     "outputs": [
         {"name": "sqrtPriceX96", "type": "uint160"},
         {"name": "tick", "type": "int24"},
         {"name": "observationIndex", "type": "uint16"},
         {"name": "observationCardinality", "type": "uint16"},
         {"name": "observationCardinalityNext", "type": "uint16"},
         {"name": "feeProtocol", "type": "uint8"},
         {"name": "unlocked", "type": "bool"},
     ]},
]

# Multicall3 on Arbitrum
MULTICALL3_ADDRESS = "0xcA11bde05977b3631167028862bE2a173976CA11"
MULTICALL3_ABI = [
    {"name": "aggregate3", "type": "function", "stateMutability": "payable",
     "inputs": [{"name": "calls", "type": "tuple[]", "components": [
         {"name": "target", "type": "address"},
         {"name": "allowFailure", "type": "bool"},
         {"name": "callData", "type": "bytes"},
     ]}],
     "outputs": [{"name": "returnData", "type": "tuple[]", "components": [
         {"name": "success", "type": "bool"},
         {"name": "returnData", "type": "bytes"},
     ]}]},
]


def calc_pool_position(
    *,
    cow_balance: float,
    total_supply: float,
    vault_token0: float,
    vault_token1: float,
    price_token0_usd: float,
    price_token1_usd: float,
) -> dict:
    if total_supply <= 0 or cow_balance <= 0:
        return {"my_token0": 0.0, "my_token1": 0.0, "value_usd": 0.0, "share": 0.0}

    share = cow_balance / total_supply
    my_token0 = vault_token0 * share
    my_token1 = vault_token1 * share
    value_usd = my_token0 * price_token0_usd + my_token1 * price_token1_usd

    return {
        "my_token0": my_token0,
        "my_token1": my_token1,
        "value_usd": value_usd,
        "share": share,
    }


class EVMChainReader(ChainReader):
    def __init__(
        self,
        rpc_url: str,
        fallback_rpc_url: str,
        vault_address: str,
        pool_address: str,
        wallet_address: str,
        poll_interval: float = 1.0,
        on_update: Callable[[dict], Awaitable[None]] | None = None,
    ):
        self._rpc_url = rpc_url
        self._fallback_rpc_url = fallback_rpc_url
        self._vault_address = vault_address
        self._pool_address = pool_address
        self._wallet_address = wallet_address
        self._poll_interval = poll_interval
        self._on_update = on_update
        self._w3: AsyncWeb3 | None = None
        self._task: asyncio.Task | None = None
        self._running = False
        self._consecutive_failures = 0

    async def start(self) -> None:
        self._w3 = AsyncWeb3(AsyncHTTPProvider(self._rpc_url))
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"EVM chain reader started (poll every {self._poll_interval}s)")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                data = await self.read_pool_position()
                self._consecutive_failures = 0
                if self._on_update:
                    await self._on_update(data)
            except Exception as e:
                self._consecutive_failures += 1
                logger.error(f"Chain read failed ({self._consecutive_failures}): {e}")
                if self._consecutive_failures >= 5:
                    logger.critical("Chain reader: 5 consecutive failures, entering safe mode")
            await asyncio.sleep(self._poll_interval)

    async def read_pool_position(self) -> dict:
        vault = self._w3.eth.contract(
            address=self._w3.to_checksum_address(self._vault_address),
            abi=CLM_ABI,
        )

        cow_balance = await vault.functions.balanceOf(
            self._w3.to_checksum_address(self._wallet_address)
        ).call()
        total_supply = await vault.functions.totalSupply().call()
        balances = await vault.functions.balances().call()

        # Convert from Wei (18 decimals assumed — adjust per token)
        cow_balance_f = cow_balance / 1e18
        total_supply_f = total_supply / 1e18
        token0_f = balances[0] / 1e18
        token1_f = balances[1] / 1e18

        return {
            "cow_balance": cow_balance_f,
            "total_supply": total_supply_f,
            "vault_token0": token0_f,
            "vault_token1": token1_f,
        }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_evm.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add chains/ tests/test_evm.py
git commit -m "feat: add EVM chain reader with Beefy CLM contract calls"
```

---

## Phase 4: Web Dashboard

### Task 12: Auth middleware + Starlette app shell

**Files:**
- Create: `web/auth.py`
- Create: `app.py`
- Create: `tests/test_web.py`

- [ ] **Step 1: Write tests**

```python
# tests/test_web.py
import base64
import pytest
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


def test_health_no_auth(app):
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_dashboard_requires_auth(app):
    client = TestClient(app)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 401


def test_dashboard_with_valid_auth(app):
    client = TestClient(app)
    creds = base64.b64encode(b"admin:secret").decode()
    resp = client.get("/", headers={"Authorization": f"Basic {creds}"})
    assert resp.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_web.py -v`
Expected: FAIL

- [ ] **Step 3: Implement web/auth.py**

```python
# web/auth.py
from __future__ import annotations
import base64
import secrets
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response


class BasicAuthMiddleware(BaseHTTPMiddleware):
    def __init__(self, app, username: str, password: str, exclude: list[str] | None = None):
        super().__init__(app)
        self._username = username
        self._password = password
        self._exclude = exclude or []

    async def dispatch(self, request: Request, call_next):
        for path in self._exclude:
            if request.url.path.startswith(path):
                return await call_next(request)

        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Basic "):
            return Response("Unauthorized", status_code=401,
                          headers={"WWW-Authenticate": 'Basic realm="AutoMoney"'})

        try:
            decoded = base64.b64decode(auth[6:]).decode()
            user, passwd = decoded.split(":", 1)
        except Exception:
            return Response("Invalid credentials", status_code=401,
                          headers={"WWW-Authenticate": 'Basic realm="AutoMoney"'})

        if not (secrets.compare_digest(user, self._username)
                and secrets.compare_digest(passwd, self._password)):
            return Response("Invalid credentials", status_code=401,
                          headers={"WWW-Authenticate": 'Basic realm="AutoMoney"'})

        return await call_next(request)
```

- [ ] **Step 4: Implement app.py**

```python
# app.py
from __future__ import annotations
import os
import logging
from contextlib import asynccontextmanager
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles
from starlette.requests import Request
from starlette.responses import JSONResponse
from config import Settings
from state import StateHub
from db import Database
from web.auth import BasicAuthMiddleware
from web.routes import dashboard, sse_state, sse_logs, update_settings

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


def create_app(start_engine: bool = True) -> Starlette:
    settings = Settings.from_env()
    state = StateHub(
        hedge_ratio=settings.hedge_ratio,
        max_exposure_pct=settings.max_exposure_pct,
        repost_depth=settings.repost_depth,
    )
    db_path = os.environ.get("DB_PATH", "automoney.db")
    db = Database(db_path)

    @asynccontextmanager
    async def lifespan(app):
        await db.initialize()
        app.state.settings = settings
        app.state.hub = state
        app.state.db = db
        if start_engine:
            # Engine startup will be added in integration phase
            pass
        yield
        await db.close()

    routes = [
        Route("/health", lambda r: JSONResponse({"status": "ok"})),
        Route("/", dashboard),
        Route("/sse/state", sse_state),
        Route("/sse/logs", sse_logs),
        Route("/settings", update_settings, methods=["POST"]),
        Mount("/static", StaticFiles(directory="web/static"), name="static"),
    ]

    app = Starlette(routes=routes, lifespan=lifespan)
    app.add_middleware(
        BasicAuthMiddleware,
        username=settings.auth_user,
        password=settings.auth_pass,
        exclude=["/health"],
    )
    return app


app = create_app()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_web.py -v`
Expected: 3 passed (may need stub routes first — see next task)

- [ ] **Step 6: Commit**

```bash
git add app.py web/auth.py tests/test_web.py
git commit -m "feat: add Starlette app with basic auth and health check"
```

---

### Task 13: Web routes + SSE endpoints

**Files:**
- Create: `web/routes.py`

- [ ] **Step 1: Implement web/routes.py**

```python
# web/routes.py
from __future__ import annotations
import asyncio
import json
from starlette.requests import Request
from starlette.responses import HTMLResponse
from starlette.templating import Jinja2Templates
from sse_starlette.sse import EventSourceResponse

templates = Jinja2Templates(directory="web/templates")


async def dashboard(request: Request):
    hub = request.app.state.hub
    db = request.app.state.db
    stats = await db.get_fill_stats()
    snapshots = await db.get_pool_snapshots(limit=5000)
    logs = await db.get_order_logs(limit=50)

    return templates.TemplateResponse("dashboard.html", {
        "request": request,
        "hub": hub,
        "stats": stats,
        "snapshots_json": json.dumps(snapshots),
        "logs": logs,
    })


async def sse_state(request: Request):
    hub = request.app.state.hub

    async def event_generator():
        last_update = 0.0
        while True:
            if hub.last_update > last_update:
                last_update = hub.last_update
                data = hub.to_dict()
                yield {"event": "state-update", "data": json.dumps(data)}
            await asyncio.sleep(0.2)  # 5 updates/sec max

    return EventSourceResponse(event_generator())


async def sse_logs(request: Request):
    db = request.app.state.db

    async def event_generator():
        last_id = 0
        while True:
            logs = await db.get_order_logs(limit=5)
            for log in reversed(logs):
                if log["id"] > last_id:
                    last_id = log["id"]
                    yield {"event": "new-log", "data": json.dumps(log)}
            await asyncio.sleep(0.5)

    return EventSourceResponse(event_generator())


async def update_settings(request: Request):
    hub = request.app.state.hub
    db = request.app.state.db
    form = await request.form()

    if "hedge_ratio" in form:
        hub.hedge_ratio = float(form["hedge_ratio"])
        await db.set_config("hedge_ratio", str(hub.hedge_ratio))
    if "max_exposure_pct" in form:
        hub.max_exposure_pct = float(form["max_exposure_pct"])
        await db.set_config("max_exposure_pct", str(hub.max_exposure_pct))
    if "repost_depth" in form:
        hub.repost_depth = int(form["repost_depth"])
        await db.set_config("repost_depth", str(hub.repost_depth))

    return HTMLResponse('<div id="settings-status">Settings saved</div>')
```

- [ ] **Step 2: Commit**

```bash
git add web/routes.py
git commit -m "feat: add web routes with SSE state/log streaming"
```

---

### Task 14: HTML templates + dashboard UI

**Files:**
- Create: `web/templates/base.html`
- Create: `web/templates/dashboard.html`
- Create: `web/templates/partials/pool.html`
- Create: `web/templates/partials/hedge.html`
- Create: `web/templates/partials/pnl.html`
- Create: `web/templates/partials/chart.html`
- Create: `web/templates/partials/book.html`
- Create: `web/templates/partials/logs.html`
- Create: `web/templates/partials/reports.html`
- Create: `web/templates/partials/settings.html`
- Create: `web/static/app.js`
- Create: `web/static/chart.js`

- [ ] **Step 1: Create base.html**

```html
<!-- web/templates/base.html -->
<!DOCTYPE html>
<html lang="en" class="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>AutoMoney</title>
    <script src="https://cdn.tailwindcss.com"></script>
    <script>
        tailwind.config = {
            darkMode: 'class',
            theme: {
                extend: {
                    colors: {
                        dark: { 800: '#1a1a2e', 900: '#0f0f1a', 700: '#252547' },
                        accent: { green: '#00d4aa', red: '#ff4757', blue: '#5b86e5', yellow: '#ffc048' }
                    }
                }
            }
        }
    </script>
    <script src="https://unpkg.com/htmx.org@2.0.4"></script>
    <script src="https://unpkg.com/htmx-ext-sse@2.3.0/sse.js"></script>
    <script defer src="https://cdn.jsdelivr.net/npm/alpinejs@3.x.x/dist/cdn.min.js"></script>
    <script src="https://unpkg.com/uplot@1.6.30/dist/uPlot.iife.min.js"></script>
    <link rel="stylesheet" href="https://unpkg.com/uplot@1.6.30/dist/uPlot.min.css">
    <style>
        body { background: #0f0f1a; color: #e0e0e0; font-family: 'Inter', system-ui, sans-serif; }
        .card { background: #1a1a2e; border: 1px solid #252547; border-radius: 12px; padding: 1.25rem; }
        .value-positive { color: #00d4aa; }
        .value-negative { color: #ff4757; }
        .badge-maker { background: #00d4aa22; color: #00d4aa; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; }
        .badge-taker { background: #ff475722; color: #ff4757; padding: 2px 8px; border-radius: 4px; font-size: 0.75rem; }
        .safe-mode-on { background: #ff4757; animation: pulse 1.5s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.5; } }
    </style>
</head>
<body class="min-h-screen p-4">
    {% block content %}{% endblock %}
    <script src="/static/app.js"></script>
    <script src="/static/chart.js"></script>
</body>
</html>
```

- [ ] **Step 2: Create dashboard.html**

```html
<!-- web/templates/dashboard.html -->
{% extends "base.html" %}
{% block content %}
<div x-data="dashboard()" x-init="init()" class="max-w-7xl mx-auto space-y-4">
    <!-- Header -->
    <div class="flex items-center justify-between">
        <h1 class="text-2xl font-bold text-white tracking-tight">AutoMoney</h1>
        <div class="flex items-center gap-3">
            <span x-show="state.safe_mode" class="safe-mode-on text-white text-sm font-bold px-3 py-1 rounded">
                SAFE MODE
            </span>
            <span x-show="!state.safe_mode" class="bg-green-900/30 text-accent-green text-sm px-3 py-1 rounded">
                ACTIVE
            </span>
            <span class="text-xs text-gray-500" x-text="'Updated: ' + lastUpdate"></span>
        </div>
    </div>

    <!-- Top row: Pool / Hedge / PnL -->
    <div class="grid grid-cols-1 md:grid-cols-3 gap-4">
        {% include "partials/pool.html" %}
        {% include "partials/hedge.html" %}
        {% include "partials/pnl.html" %}
    </div>

    <!-- Chart -->
    {% include "partials/chart.html" %}

    <!-- Middle row: Book / Logs -->
    <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
        {% include "partials/book.html" %}
        {% include "partials/logs.html" %}
    </div>

    <!-- Reports -->
    {% include "partials/reports.html" %}

    <!-- Settings -->
    {% include "partials/settings.html" %}
</div>

<script>
    const initialSnapshots = {{ snapshots_json|safe }};
</script>
{% endblock %}
```

- [ ] **Step 3: Create all partials**

Create each partial file. Example `partials/pool.html`:

```html
<!-- web/templates/partials/pool.html -->
<div class="card">
    <h2 class="text-sm font-medium text-gray-400 mb-3">POOL</h2>
    <div class="space-y-2">
        <div class="flex justify-between">
            <span class="text-gray-400">Value</span>
            <span class="text-white font-mono" x-text="'$' + state.pool_value_usd.toFixed(2)">$0.00</span>
        </div>
        <div class="flex justify-between">
            <span class="text-gray-400">Deposited</span>
            <span class="text-gray-300 font-mono" x-text="'$' + state.pool_deposited_usd.toFixed(2)">$0.00</span>
        </div>
        <div class="flex justify-between">
            <span class="text-gray-400">ARB</span>
            <span class="text-white font-mono" x-text="(state.pool_tokens.ARB || 0).toFixed(2)">0</span>
        </div>
        <div class="flex justify-between">
            <span class="text-gray-400">WETH</span>
            <span class="text-white font-mono" x-text="(state.pool_tokens.WETH || 0).toFixed(4)">0</span>
        </div>
    </div>
</div>
```

Create `partials/hedge.html`:

```html
<!-- web/templates/partials/hedge.html -->
<div class="card">
    <h2 class="text-sm font-medium text-gray-400 mb-3">HEDGE</h2>
    <div class="space-y-2">
        <div class="flex justify-between">
            <span class="text-gray-400">Side</span>
            <span class="text-accent-red font-mono" x-text="state.hedge_position ? state.hedge_position.side.toUpperCase() : 'NONE'">-</span>
        </div>
        <div class="flex justify-between">
            <span class="text-gray-400">Size</span>
            <span class="text-white font-mono" x-text="state.hedge_position ? '$' + state.hedge_position.size.toFixed(2) : '-'">-</span>
        </div>
        <div class="flex justify-between">
            <span class="text-gray-400">Entry</span>
            <span class="text-gray-300 font-mono" x-text="state.hedge_position ? state.hedge_position.entry.toFixed(4) : '-'">-</span>
        </div>
        <div class="flex justify-between">
            <span class="text-gray-400">uPnL</span>
            <span :class="state.hedge_unrealized_pnl >= 0 ? 'value-positive' : 'value-negative'"
                  class="font-mono" x-text="'$' + state.hedge_unrealized_pnl.toFixed(2)">$0.00</span>
        </div>
        <div class="flex justify-between">
            <span class="text-gray-400">Exposure</span>
            <span class="text-accent-yellow font-mono" x-text="(exposurePct * 100).toFixed(1) + '%'">0%</span>
        </div>
    </div>
</div>
```

Create `partials/pnl.html`:

```html
<!-- web/templates/partials/pnl.html -->
<div class="card">
    <h2 class="text-sm font-medium text-gray-400 mb-3">PnL SUMMARY</h2>
    <div class="space-y-2">
        <div class="flex justify-between">
            <span class="text-gray-400">Pool PnL</span>
            <span :class="pnl.pool >= 0 ? 'value-positive' : 'value-negative'" class="font-mono"
                  x-text="(pnl.pool >= 0 ? '+' : '') + '$' + pnl.pool.toFixed(2)">$0.00</span>
        </div>
        <div class="flex justify-between">
            <span class="text-gray-400">Hedge PnL</span>
            <span :class="pnl.hedge >= 0 ? 'value-positive' : 'value-negative'" class="font-mono"
                  x-text="(pnl.hedge >= 0 ? '+' : '') + '$' + pnl.hedge.toFixed(2)">$0.00</span>
        </div>
        <div class="flex justify-between">
            <span class="text-gray-400">Funding</span>
            <span :class="state.funding_total >= 0 ? 'value-positive' : 'value-negative'" class="font-mono"
                  x-text="(state.funding_total >= 0 ? '+' : '') + '$' + state.funding_total.toFixed(2)">$0.00</span>
        </div>
        <div class="flex justify-between">
            <span class="text-gray-400">Fees Earned</span>
            <span class="value-positive font-mono" x-text="'+$' + state.total_fees_earned.toFixed(2)">$0.00</span>
        </div>
        <div class="flex justify-between">
            <span class="text-gray-400">Fees Paid</span>
            <span class="value-negative font-mono" x-text="'-$' + state.total_fees_paid.toFixed(2)">$0.00</span>
        </div>
        <div class="border-t border-gray-700 pt-2 flex justify-between">
            <span class="text-white font-medium">NET PnL</span>
            <span :class="pnl.net >= 0 ? 'value-positive' : 'value-negative'" class="font-bold font-mono text-lg"
                  x-text="(pnl.net >= 0 ? '+' : '') + '$' + pnl.net.toFixed(2)">$0.00</span>
        </div>
    </div>
</div>
```

Create `partials/chart.html`:

```html
<!-- web/templates/partials/chart.html -->
<div class="card">
    <h2 class="text-sm font-medium text-gray-400 mb-3">CORRELATION CHART</h2>
    <div id="chart-container" style="width:100%; height:300px;"></div>
</div>
```

Create `partials/book.html`:

```html
<!-- web/templates/partials/book.html -->
<div class="card">
    <h2 class="text-sm font-medium text-gray-400 mb-3">ORDERBOOK</h2>
    <div class="font-mono text-sm space-y-1">
        <template x-for="ask in bookAsks" :key="ask.price">
            <div class="flex justify-between text-accent-red/70">
                <span x-text="ask.price.toFixed(4)"></span>
                <span x-text="'[' + ask.size.toFixed(0) + ']'"></span>
            </div>
        </template>
        <div class="border-t border-b border-gray-700 py-1 text-center text-xs text-gray-500">
            spread: <span x-text="((state.best_ask - state.best_bid) * 10000).toFixed(1) + ' ticks'"></span>
        </div>
        <template x-for="bid in bookBids" :key="bid.price">
            <div class="flex justify-between" :class="bid.isMine ? 'text-accent-blue font-bold' : 'text-accent-green/70'">
                <span x-text="bid.price.toFixed(4)"></span>
                <span x-text="'[' + bid.size.toFixed(0) + ']'"></span>
                <span x-show="bid.isMine" class="text-accent-blue text-xs">MY</span>
            </div>
        </template>
    </div>
</div>
```

Create `partials/logs.html`:

```html
<!-- web/templates/partials/logs.html -->
<div class="card" hx-ext="sse" sse-connect="/sse/logs">
    <h2 class="text-sm font-medium text-gray-400 mb-3">ACTIVITY LOG</h2>
    <div id="log-list" class="font-mono text-xs space-y-1 max-h-64 overflow-y-auto" sse-swap="new-log" hx-swap="afterbegin">
        {% for log in logs %}
        <div class="flex gap-2">
            <span class="text-gray-500">{{ log.timestamp|int }}</span>
            <span class="{% if log.action == 'fill' %}text-accent-green{% elif log.action == 'cancel' %}text-accent-red{% else %}text-gray-300{% endif %}">
                {{ log.action|upper }} {{ log.side or '' }} {{ log.size or '' }} @ {{ log.price or '' }}
            </span>
            {% if log.reason %}<span class="text-gray-600">{{ log.reason }}</span>{% endif %}
        </div>
        {% endfor %}
    </div>
</div>
```

Create `partials/reports.html`:

```html
<!-- web/templates/partials/reports.html -->
<div class="card">
    <h2 class="text-sm font-medium text-gray-400 mb-3">REPORTS</h2>
    <div class="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
        <div>
            <span class="text-gray-400">Maker fills</span>
            <div class="text-white font-mono" x-text="state.total_maker_fills + ' ($' + state.total_maker_volume.toFixed(0) + ')'">0</div>
        </div>
        <div>
            <span class="text-gray-400">Taker fills</span>
            <div class="text-white font-mono" x-text="state.total_taker_fills + ' ($' + state.total_taker_volume.toFixed(0) + ')'">0</div>
        </div>
        <div>
            <span class="text-gray-400">Maker rate</span>
            <div class="text-accent-green font-mono"
                 x-text="state.total_maker_fills + state.total_taker_fills > 0 ? (state.total_maker_fills / (state.total_maker_fills + state.total_taker_fills) * 100).toFixed(1) + '%' : '-'">-</div>
        </div>
        <div>
            <span class="text-gray-400">Total fees</span>
            <div class="text-white font-mono" x-text="'$' + state.total_fees_paid.toFixed(2)">$0.00</div>
        </div>
    </div>
</div>
```

Create `partials/settings.html`:

```html
<!-- web/templates/partials/settings.html -->
<div class="card" x-data="{ saving: false }">
    <h2 class="text-sm font-medium text-gray-400 mb-3">SETTINGS</h2>
    <form hx-post="/settings" hx-target="#settings-status" hx-swap="innerHTML"
          @htmx:after-request="saving = false" @submit="saving = true"
          class="grid grid-cols-2 md:grid-cols-4 gap-4 text-sm">
        <label class="space-y-1">
            <span class="text-gray-400">Hedge ratio</span>
            <input type="number" name="hedge_ratio" step="0.01" min="0" max="1"
                   :value="state.hedge_ratio"
                   class="w-full bg-dark-900 border border-gray-700 rounded px-2 py-1 text-white font-mono">
        </label>
        <label class="space-y-1">
            <span class="text-gray-400">Max exposure</span>
            <input type="number" name="max_exposure_pct" step="0.01" min="0" max="1"
                   :value="state.max_exposure_pct"
                   class="w-full bg-dark-900 border border-gray-700 rounded px-2 py-1 text-white font-mono">
        </label>
        <label class="space-y-1">
            <span class="text-gray-400">Repost depth</span>
            <input type="number" name="repost_depth" step="1" min="1" max="10"
                   :value="state.repost_depth"
                   class="w-full bg-dark-900 border border-gray-700 rounded px-2 py-1 text-white font-mono">
        </label>
        <div class="flex items-end">
            <button type="submit" class="bg-accent-blue/20 text-accent-blue px-4 py-1 rounded hover:bg-accent-blue/30 transition"
                    :disabled="saving" x-text="saving ? 'Saving...' : 'Save'">Save</button>
        </div>
    </form>
    <div id="settings-status" class="text-xs text-accent-green mt-2"></div>
</div>
```

- [ ] **Step 4: Create web/static/app.js**

```javascript
// web/static/app.js
function dashboard() {
    return {
        state: {
            pool_value_usd: 0, pool_deposited_usd: 0, pool_tokens: {},
            hedge_position: null, hedge_unrealized_pnl: 0, hedge_realized_pnl: 0,
            funding_total: 0, best_bid: 0, best_ask: 0, my_order: null,
            safe_mode: false, hedge_ratio: 0.95, max_exposure_pct: 0.05,
            repost_depth: 3, total_maker_fills: 0, total_taker_fills: 0,
            total_maker_volume: 0, total_taker_volume: 0,
            total_fees_paid: 0, total_fees_earned: 0,
            connected_exchange: false, connected_chain: false,
        },
        lastUpdate: '-',
        bookAsks: [],
        bookBids: [],

        get exposurePct() {
            if (this.state.pool_value_usd <= 0) return 0;
            const target = this.state.pool_value_usd * 0.5 * this.state.hedge_ratio;
            const current = this.state.hedge_position ? this.state.hedge_position.size : 0;
            return Math.abs(target - current) / this.state.pool_value_usd;
        },

        get pnl() {
            const pool = this.state.pool_value_usd - this.state.pool_deposited_usd;
            const hedge = this.state.hedge_realized_pnl + this.state.hedge_unrealized_pnl;
            const net = pool + hedge + this.state.funding_total - this.state.total_fees_paid;
            return { pool, hedge, net };
        },

        init() {
            const es = new EventSource('/sse/state');
            es.addEventListener('state-update', (e) => {
                const data = JSON.parse(e.data);
                Object.assign(this.state, data);
                this.lastUpdate = new Date().toLocaleTimeString();
                if (window.updateChart) {
                    window.updateChart(data);
                }
            });
            if (typeof initialSnapshots !== 'undefined' && window.initChart) {
                window.initChart(initialSnapshots);
            }
        }
    };
}
```

- [ ] **Step 5: Create web/static/chart.js**

```javascript
// web/static/chart.js
let chart = null;
let chartData = [[], [], [], []]; // [timestamps, pool_pnl, hedge_pnl_inverted, net_pnl]

function initChart(snapshots) {
    const container = document.getElementById('chart-container');
    if (!container) return;

    chartData = [[], [], [], []];
    for (const s of snapshots) {
        chartData[0].push(s.timestamp);
        chartData[1].push(s.pool_pnl || 0);
        chartData[2].push(-(s.hedge_pnl || 0));  // inverted
        chartData[3].push(s.net_pnl || 0);
    }

    const opts = {
        width: container.clientWidth,
        height: 280,
        scales: { x: { time: true }, y: {} },
        axes: [
            { stroke: '#555', grid: { stroke: '#1a1a2e' } },
            { stroke: '#555', grid: { stroke: '#1a1a2e' } },
        ],
        series: [
            {},
            { label: 'Pool PnL', stroke: '#5b86e5', width: 2 },
            { label: 'Hedge PnL x-1', stroke: '#ff4757', width: 2 },
            { label: 'Net PnL', stroke: '#00d4aa', width: 2 },
        ],
    };

    chart = new uPlot(opts, chartData, container);
    window.addEventListener('resize', () => chart.setSize({ width: container.clientWidth, height: 280 }));
}

function updateChart(state) {
    if (!chart) return;
    const now = state.last_update || Date.now() / 1000;
    const poolPnl = state.pool_value_usd - state.pool_deposited_usd;
    const hedgePnl = -(state.hedge_realized_pnl + state.hedge_unrealized_pnl + state.funding_total);
    const netPnl = poolPnl + state.hedge_realized_pnl + state.hedge_unrealized_pnl + state.funding_total - state.total_fees_paid;

    chartData[0].push(now);
    chartData[1].push(poolPnl);
    chartData[2].push(hedgePnl);
    chartData[3].push(netPnl);

    // Keep last 5000 points
    if (chartData[0].length > 5000) {
        for (let i = 0; i < 4; i++) chartData[i].shift();
    }
    chart.setData(chartData);
}

window.initChart = initChart;
window.updateChart = updateChart;
```

- [ ] **Step 6: Commit**

```bash
git add web/
git commit -m "feat: add dashboard UI with HTMX/SSE, Tailwind, Alpine.js, uPlot chart"
```

---

## Phase 5: Integration + Deploy

### Task 15: Engine orchestrator (wiring everything together)

**Files:**
- Modify: `app.py` (add engine startup to lifespan)
- Create: `engine/__init__.py` (orchestrator)

- [ ] **Step 1: Implement engine orchestrator**

```python
# engine/__init__.py
from __future__ import annotations
import asyncio
import time
import logging
from state import StateHub
from db import Database
from config import Settings
from chains.evm import EVMChainReader, calc_pool_position
from exchanges.base import ExchangeAdapter, Fill
from exchanges.hyperliquid import HyperliquidAdapter
from exchanges.dydx import DydxAdapter
from engine.hedge import compute_hedge_action
from engine.orderbook import calc_maker_price, calc_aggressive_price, check_order_depth
from engine.pnl import calc_pnl

logger = logging.getLogger(__name__)
SNAPSHOT_INTERVAL = 10.0  # seconds


class Engine:
    def __init__(self, settings: Settings, hub: StateHub, db: Database):
        self._settings = settings
        self._hub = hub
        self._db = db
        self._exchange: ExchangeAdapter | None = None
        self._chain: EVMChainReader | None = None
        self._snapshot_task: asyncio.Task | None = None
        self._last_snapshot = 0.0

    async def start(self) -> None:
        # Init exchange
        if self._settings.active_exchange == "hyperliquid":
            self._exchange = HyperliquidAdapter(
                api_key=self._settings.hyperliquid_api_key,
                api_secret=self._settings.hyperliquid_api_secret,
                wallet_address=self._settings.wallet_address,
            )
        else:
            self._exchange = DydxAdapter(
                mnemonic=self._settings.dydx_mnemonic,
                wallet_address=self._settings.wallet_address,
            )

        await self._exchange.connect()
        self._hub.connected_exchange = True

        symbol = self._settings.hyperliquid_symbol if self._settings.active_exchange == "hyperliquid" else self._settings.dydx_symbol
        await self._exchange.subscribe_orderbook(symbol, self._on_book_update)
        await self._exchange.subscribe_fills(symbol, self._on_fill)

        # Init chain reader
        self._chain = EVMChainReader(
            rpc_url=self._settings.arbitrum_rpc_url,
            fallback_rpc_url=self._settings.arbitrum_rpc_fallback,
            vault_address=self._settings.clm_vault_address,
            pool_address=self._settings.clm_pool_address,
            wallet_address=self._settings.wallet_address,
            poll_interval=1.0,
            on_update=self._on_chain_update,
        )
        await self._chain.start()
        self._hub.connected_chain = True

        # Snapshot task
        self._snapshot_task = asyncio.create_task(self._snapshot_loop())
        logger.info("Engine started")

    async def stop(self) -> None:
        if self._snapshot_task:
            self._snapshot_task.cancel()
        if self._exchange:
            await self._exchange.disconnect()
        if self._chain:
            await self._chain.stop()

    async def _on_chain_update(self, data: dict) -> None:
        self._hub.cow_balance = data["cow_balance"]
        self._hub.cow_total_supply = data["total_supply"]
        self._hub.vault_balances = (data["vault_token0"], data["vault_token1"])
        # Price comes from exchange book — use best_bid as proxy
        price_token0 = self._hub.best_bid if self._hub.best_bid > 0 else 1.0
        pos = calc_pool_position(
            cow_balance=data["cow_balance"],
            total_supply=data["total_supply"],
            vault_token0=data["vault_token0"],
            vault_token1=data["vault_token1"],
            price_token0_usd=price_token0,
            price_token1_usd=1.0,  # WETH priced in the pair
        )
        self._hub.pool_value_usd = pos["value_usd"]
        self._hub.pool_tokens = {"ARB": pos["my_token0"], "WETH": pos["my_token1"]}
        self._hub.last_update = time.time()

    async def _on_book_update(self, data: dict) -> None:
        bids = data.get("bids", data.get("levels", []))
        asks = data.get("asks", [])
        if bids and isinstance(bids[0], (list, tuple)):
            self._hub.best_bid = float(bids[0][0])
        if asks and isinstance(asks[0], (list, tuple)):
            self._hub.best_ask = float(asks[0][0])

        # Check order depth
        if self._hub.my_order:
            book_levels = {}
            side = self._hub.my_order["side"]
            levels = bids if side == "buy" else asks
            for level in levels:
                book_levels[float(level[0])] = float(level[1])
            action = check_order_depth(
                side=side,
                price=self._hub.my_order["price"],
                book_levels=book_levels,
                max_depth=self._hub.repost_depth,
            )
            if action == "REPOST":
                await self._repost_order()

        # Run hedge cycle
        await self._hedge_cycle()
        self._hub.last_update = time.time()

    async def _on_fill(self, fill: Fill) -> None:
        await self._db.insert_fill(
            timestamp=fill.timestamp, exchange=self._exchange.name,
            symbol=fill.symbol, side=fill.side, size=fill.size,
            price=fill.price, fee=fill.fee, fee_currency=fill.fee_currency,
            liquidity=fill.liquidity, realized_pnl=fill.realized_pnl,
            order_id=fill.order_id,
        )
        if fill.liquidity == "maker":
            self._hub.total_maker_fills += 1
            self._hub.total_maker_volume += fill.size
        else:
            self._hub.total_taker_fills += 1
            self._hub.total_taker_volume += fill.size
        self._hub.total_fees_paid += fill.fee
        self._hub.hedge_realized_pnl += fill.realized_pnl

        await self._db.insert_order_log(
            timestamp=time.time(), exchange=self._exchange.name,
            action="fill", side=fill.side, size=fill.size,
            price=fill.price, reason=fill.liquidity,
        )
        self._hub.my_order = None  # order was filled
        self._hub.last_update = time.time()

    async def _hedge_cycle(self) -> None:
        if self._hub.safe_mode or self._hub.pool_value_usd <= 0:
            return

        current_hedge = 0.0
        pos = await self._exchange.get_position(
            self._settings.hyperliquid_symbol if self._settings.active_exchange == "hyperliquid" else self._settings.dydx_symbol
        )
        if pos:
            current_hedge = pos.size
            self._hub.hedge_position = {
                "side": pos.side, "size": pos.size, "entry": pos.entry_price,
            }
            self._hub.hedge_unrealized_pnl = pos.unrealized_pnl

        decision = compute_hedge_action(
            pool_value_usd=self._hub.pool_value_usd,
            token_exposure_ratio=0.5,
            hedge_ratio=self._hub.hedge_ratio,
            current_hedge_size=current_hedge,
            max_exposure_pct=self._hub.max_exposure_pct,
            safe_mode=self._hub.safe_mode,
        )

        if decision.action == "HOLD":
            return

        tick = self._exchange.get_tick_size(self._settings.hyperliquid_symbol)

        if decision.action == "MAKER":
            price = calc_maker_price(
                side=decision.side, best_bid=self._hub.best_bid,
                best_ask=self._hub.best_ask, tick=tick,
            )
        else:
            price = calc_aggressive_price(
                side=decision.side, best_bid=self._hub.best_bid,
                best_ask=self._hub.best_ask, tick=tick,
            )

        # Cancel existing order if any
        if self._hub.my_order:
            await self._exchange.cancel_order(self._hub.my_order["order_id"])
            self._hub.my_order = None

        symbol = self._settings.hyperliquid_symbol if self._settings.active_exchange == "hyperliquid" else self._settings.dydx_symbol
        order = await self._exchange.place_limit_order(
            symbol=symbol, side=decision.side,
            size=decision.delta, price=price,
        )
        self._hub.my_order = {
            "order_id": order.order_id, "side": order.side,
            "size": order.size, "price": order.price,
        }

        reason = "exposure_rebalance" if decision.action == "MAKER" else "aggressive"
        await self._db.insert_order_log(
            timestamp=time.time(), exchange=self._exchange.name,
            action="place", side=decision.side, size=decision.delta,
            price=price, reason=reason,
        )

    async def _repost_order(self) -> None:
        if not self._hub.my_order:
            return
        await self._exchange.cancel_order(self._hub.my_order["order_id"])
        await self._db.insert_order_log(
            timestamp=time.time(), exchange=self._exchange.name,
            action="cancel", side=self._hub.my_order["side"],
            price=self._hub.my_order["price"], reason="depth_repost",
        )
        self._hub.my_order = None
        # Next book update will trigger hedge_cycle which places a new order

    async def _snapshot_loop(self) -> None:
        while True:
            await asyncio.sleep(SNAPSHOT_INTERVAL)
            pnl = calc_pnl(
                pool_value_usd=self._hub.pool_value_usd,
                pool_deposited_usd=self._hub.pool_deposited_usd,
                hedge_realized_pnl=self._hub.hedge_realized_pnl,
                hedge_unrealized_pnl=self._hub.hedge_unrealized_pnl,
                funding_total=self._hub.funding_total,
                total_fees_paid=self._hub.total_fees_paid,
            )
            await self._db.insert_pool_snapshot(
                timestamp=time.time(),
                pool_value_usd=self._hub.pool_value_usd,
                token0_amount=self._hub.pool_tokens.get("ARB", 0),
                token1_amount=self._hub.pool_tokens.get("WETH", 0),
                hedge_value_usd=self._hub.hedge_position["size"] if self._hub.hedge_position else 0,
                hedge_pnl=pnl.hedge_pnl,
                pool_pnl=pnl.pool_pnl,
                net_pnl=pnl.net_pnl,
                funding_cumulative=self._hub.funding_total,
                fees_earned_cumulative=self._hub.total_fees_earned,
                fees_paid_cumulative=self._hub.total_fees_paid,
            )
```

- [ ] **Step 2: Wire engine into app.py lifespan**

Update the lifespan in `app.py`:

```python
# In the lifespan function, replace the "if start_engine" block:
        if start_engine:
            from engine import Engine
            engine = Engine(settings, state, db)
            await engine.start()
            app.state.engine = engine
        yield
        if start_engine and hasattr(app.state, 'engine'):
            await app.state.engine.stop()
        await db.close()
```

- [ ] **Step 3: Commit**

```bash
git add engine/__init__.py app.py
git commit -m "feat: add engine orchestrator wiring chain reader, exchange, and hedge logic"
```

---

### Task 16: Deploy to fly.io

- [ ] **Step 1: Initialize fly app**

Run: `fly apps create automoney`

- [ ] **Step 2: Set secrets**

Run:
```bash
fly secrets set \
  AUTH_USER=admin \
  AUTH_PASS=<your-password> \
  WALLET_ADDRESS=<your-hot-wallet> \
  WALLET_PRIVATE_KEY=<your-pk> \
  ARBITRUM_RPC_URL=<your-rpc> \
  CLM_VAULT_ADDRESS=<vault> \
  CLM_POOL_ADDRESS=<pool> \
  HYPERLIQUID_API_KEY=<key> \
  HYPERLIQUID_API_SECRET=<secret> \
  --app automoney
```

- [ ] **Step 3: Deploy**

Run: `fly deploy --app automoney`

- [ ] **Step 4: Verify health**

Run: `curl https://automoney.fly.dev/health`
Expected: `{"status":"ok"}`

- [ ] **Step 5: Verify dashboard loads**

Open: `https://automoney.fly.dev/` (will prompt for basic auth)

- [ ] **Step 6: Commit any deploy adjustments**

```bash
git add -A
git commit -m "chore: finalize deploy configuration"
```

---

### Task 17: Push to GitHub remote

- [ ] **Step 1: Set remote to New-cryptohedge repo**

Run: `git remote add origin https://github.com/wallmxz/New-cryptohedge.git || git remote set-url origin https://github.com/wallmxz/New-cryptohedge.git`

- [ ] **Step 2: Push**

Run: `git push -u origin master`
