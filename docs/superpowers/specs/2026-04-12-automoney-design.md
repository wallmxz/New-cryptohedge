# AutoMoney - Design Specification

## Overview

AutoMoney is a delta-neutral yield farming bot that provides liquidity via Beefy CLM vaults and hedges directional exposure on perpetual DEXs (Hyperliquid + dYdX v4). The goal is to collect pool fees with zero directional risk, using maker-only order execution to minimize trading costs.

**Initial pair:** ARB/WETH on Arbitrum (Uniswap v3 via Beefy CLM)
**Capital:** ~$200 in a dedicated hot wallet
**Deploy:** fly.io (`automoney`), shared-cpu-1x 256MB

---

## 1. Architecture

Single-process async Python monolith. Everything runs in one `asyncio` event loop: chain monitoring, exchange WebSockets, hedge engine, and web dashboard.

```
┌──────────────────────── AutoMoney (1 process) ────────────────────────┐
│                                                                        │
│  asyncio event loop                                                    │
│                                                                        │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────┐  ┌──────────────┐  │
│  │ Chain Reader │  │ Exchange WS  │  │   Hedge    │  │  Starlette   │  │
│  │ (RPC poll    │  │ (book+fills) │  │   Engine   │  │  (SSE+HTMX)  │  │
│  │  every 1s)   │  │  (stream)    │  │            │  │              │  │
│  └──────┬───────┘  └──────┬───────┘  └─────┬──────┘  └──────┬───────┘  │
│         │                 │                │                │          │
│         └─────────────────┴────────────────┘                │          │
│                           │                                 │          │
│                     ┌─────┴─────┐                           │          │
│                     │ State Hub │───────── SSE broadcast ───┘          │
│                     └─────┬─────┘                                      │
│                           │                                            │
│                     ┌─────┴─────┐                                      │
│                     │  SQLite   │                                      │
│                     └───────────┘                                      │
└────────────────────────────────────────────────────────────────────────┘
```

### Why this architecture

- **RAM:** ~120-150MB, fits in 256MB fly.io instance (~$2/month)
- **Latency:** Zero inter-process overhead, data flows within the same event loop
- **Simplicity:** No threads, no IPC, no race conditions, pure asyncio
- **Resilience:** fly.io health check auto-restarts on crash; SQLite persists state for recovery

---

## 2. State Hub

In-memory object holding all current state. The hot path (hedge engine cycle) never touches SQLite -- it reads and writes only to StateHub. SQLite is written to asynchronously for persistence and history.

```python
class StateHub:
    # ── Pool ──
    pool_value_usd: float           # current pool position in USD
    pool_deposited_usd: float       # original deposit value
    pool_tokens: dict               # {"ARB": 1500.0, "WETH": 0.3}
    cow_balance: float              # cowToken balance
    cow_total_supply: float         # vault total supply
    vault_balances: tuple           # (token0_amount, token1_amount)

    # ── Hedge ──
    hedge_position: dict | None     # {"side": "short", "size": 190.0, "entry": 1.05}
    hedge_unrealized_pnl: float
    hedge_realized_pnl: float       # cumulative from all closed trades
    funding_total: float            # cumulative funding payments

    # ── Orderbook ──
    best_bid: float
    best_ask: float
    my_order: dict | None           # active order on the book
    my_order_depth: int             # price level position (1st, 2nd, 3rd)

    # ── Config (editable via dashboard) ──
    hedge_ratio: float = 0.95       # target: 95% hedged
    max_exposure_pct: float = 0.05  # 0-5% = maker, >5% = aggressive

    # ── Metrics ──
    total_maker_fills: int = 0
    total_taker_fills: int = 0
    total_maker_volume: float = 0.0
    total_taker_volume: float = 0.0
    total_fees_paid: float = 0.0
    total_fees_earned: float = 0.0  # pool fees collected

    # ── System ──
    connected_exchange: bool = False
    connected_chain: bool = False
    safe_mode: bool = False         # True = stop trading
    last_update: float              # timestamp
```

---

## 3. Chain Reader (Beefy CLM direct contract)

Reads position data directly from the Beefy CLM smart contract via `eth_call` every 1 second. No Beefy API dependency -- eliminates their 10-30s data delay.

### Data source

Direct RPC calls to the CLM vault contract on Arbitrum, batched via Multicall for atomicity (all data from the same block).

```
1 RPC call / second via Multicall:
  ├── balanceOf(wallet)     → cowToken balance
  ├── totalSupply()         → vault total supply
  ├── balances()            → (token0_amount, token1_amount) in vault
  └── pool.slot0()          → current price (sqrtPriceX96)

Result: my_share = cowBalance / totalSupply
        my_token0 = vault_token0 * my_share
        my_token1 = vault_token1 * my_share
        position_value_usd = my_token0 * price0 + my_token1 * price1
```

### RPC considerations

- Arbitrum block time: ~250ms (data updates 4x/sec, 1s poll is sufficient)
- `eth_call` is free (read-only, no gas)
- Free RPC tiers (Alchemy/Infura): 10-30 req/s, we use 1 req/s
- Fallback RPC list for reliability

### Extensibility

`ChainReader` is an abstract base class. `EVMChainReader` implements it for EVM chains. Adding a new chain = new RPC endpoint + CLM contract address in config.

---

## 4. Exchange Adapters

Abstract base class with two implementations. Both connect via WebSocket for real-time data and REST for order management.

```python
class ExchangeAdapter(ABC):
    async def connect(self) -> None: ...
    async def disconnect(self) -> None: ...

    # Market data (via WebSocket)
    async def subscribe_orderbook(self, symbol: str) -> None: ...
    async def subscribe_fills(self, symbol: str) -> None: ...

    # Order management (via REST)
    async def place_limit_order(self, symbol, side, size, price) -> Order: ...
    async def cancel_order(self, order_id) -> None: ...
    async def get_position(self, symbol) -> Position: ...
    async def get_fills(self, symbol, since) -> list[Fill]: ...

    # Info
    def get_tick_size(self, symbol) -> float: ...
    def get_min_notional(self, symbol) -> float: ...
```

### Hyperliquid specifics

- WebSocket: `wss://api.hyperliquid.xyz/ws`
- Orderbook L2 stream + fill updates
- Closed PnL includes fees (embedded)
- Fill data includes maker/taker flag
- Maker fee: 0.015% (base tier)
- Min notional: ~$10

### dYdX v4 specifics

- WebSocket: indexer websocket for orderbook + fills
- Realized PnL: includes funding, need to verify if includes fees
- Fill data: maker/taker flag not explicitly documented -- infer from order type
- Maker fee: 0.02% (base tier), possible rebate via governance
- Min equity: $20 = 1 order, $100 = 5 orders
- PnL calculation: build from fills + funding + fees separately for accuracy

### PnL normalization

Since exchanges handle PnL differently, we calculate internally:

```
For each exchange:
  realized_pnl = sum(fill.pnl for fill in closed_fills)
  fees_paid = sum(fill.fee for fill in all_fills)
  funding = sum(funding_payments)

Normalized hedge PnL = realized_pnl - fees_paid + funding
```

---

## 5. Hedge Engine

The core logic. Runs on every price tick from the exchange WebSocket.

### Cycle

```
ON PRICE TICK:

1. READ STATE
   target_hedge = pool_value * token_exposure_ratio * hedge_ratio
   current_hedge = abs(hedge_position.size) if hedge_position else 0
   delta = target_hedge - current_hedge
   exposure_pct = abs(delta) / pool_value

2. DECIDE
   if exposure_pct <= max_exposure_pct (0-5%):
       mode = MAKER
   else:
       mode = AGGRESSIVE

3. PRICE CALCULATION
   tick = exchange.get_tick_size(symbol)
   spread = best_ask - best_bid

   if mode == MAKER:
       if side == "sell":
           price = best_ask - tick if spread > tick else best_ask
       if side == "buy":
           price = best_bid + tick if spread > tick else best_bid

       # Safety: NEVER cross the spread
       if side == "sell" and price <= best_bid:
           price = best_bid + tick
       if side == "buy" and price >= best_ask:
           price = best_ask - tick

   if mode == AGGRESSIVE:
       if side == "sell":
           price = best_bid + tick   # just above bid, still limit
       if side == "buy":
           price = best_ask - tick   # just below ask, still limit
       # Aggressive but still limit order, never market

4. ORDER MANAGEMENT
   if no active order:
       place new limit order at calculated price
   elif active order exists:
       if my_order_depth >= 3:  # fell to 3rd price level
           cancel and replace at new price
       elif price changed significantly:
           cancel and replace

5. PERSIST + BROADCAST
   write fill/order events to SQLite (async)
   SSE push new state to dashboard
```

### Orderbook depth monitoring

The bot subscribes to the exchange's L2 orderbook WebSocket. On each book update:

1. Find which price level my order sits at
2. Count levels from the best price on my side
3. If my order is at the 3rd level or beyond, cancel and repost closer

```python
async def check_order_depth(self, book, my_order):
    if my_order.side == "sell":
        levels = sorted(book.asks.keys())  # ascending
        my_level = levels.index(my_order.price) if my_order.price in levels else -1
    else:
        levels = sorted(book.bids.keys(), reverse=True)  # descending
        my_level = levels.index(my_order.price) if my_order.price in levels else -1

    if my_level >= 2:  # 0-indexed, so 2 = 3rd level
        return "REPOST"
    return "HOLD"
```

### Safe mode triggers

Bot stops trading and alerts when:
- Exchange WebSocket disconnects for > 10 seconds
- Chain RPC fails for > 5 consecutive polls
- Order placement fails 3x consecutively
- Exposure exceeds 10% (double the max threshold)

---

## 6. PnL Calculation

### Formula

```
Pool PnL       = pool_value_now - pool_deposited_value
Hedge PnL      = hedge_realized_pnl + hedge_unrealized_pnl
Funding PnL    = funding_total
Fees Earned    = pool_fees_collected (from Beefy CLM harvests)
Fees Paid      = total_trading_fees (maker + taker)

Net PnL = Pool PnL + Hedge PnL + Funding PnL + Fees Earned - Fees Paid
```

### Pool fees (Beefy CLM autocompound)

Beefy CLM autocompounds pool trading fees back into the position. This means `cowToken` price increases over time as fees accrue. To isolate the fees earned:

```
fees_earned = pool_value_now - pool_value_if_no_fees

Since cowToken price embeds fees:
  pool_value_now = cow_balance * (vault_total_value / cow_total_supply)
  pool_value_no_fees = deposited tokens at current market prices (no compounding)

Approximation:
  fees_earned ≈ pool_value_now - (deposited_token0 * price0_now + deposited_token1 * price1_now)
  This captures fees + IL combined. Separating them precisely requires tracking
  the vault's fee harvest events on-chain (BeefyHarvest events).
```

For V1, we track `pool_pnl = pool_value_now - pool_deposited_usd` which includes both fees and IL. The hedge cancels IL, so net = fees. This is accurate enough without parsing harvest events.

### Tracking from deposit

SQLite stores:
- `deposits` table: timestamp, pool_value_at_deposit, tokens_deposited
- `fills` table: every trade fill with maker/taker flag, fee, pnl
- `funding` table: every funding payment
- `pool_snapshots` table: pool value snapshots every 10 seconds for the chart

### Maker/Taker tracking

- Hyperliquid: fill response includes `liquidity` field (maker/taker)
- dYdX v4: if not in fill response, infer from order type -- limit orders that don't cross spread = maker
- Every fill is tagged and stored in SQLite for reporting

---

## 7. Database Schema (SQLite)

```sql
-- Configuration
CREATE TABLE config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at REAL
);

-- Pool deposits and withdrawals
CREATE TABLE deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    action TEXT NOT NULL,  -- 'deposit' or 'withdraw'
    pool_value_usd REAL NOT NULL,
    token0_amount REAL,
    token1_amount REAL,
    cow_tokens REAL,
    tx_hash TEXT
);

-- Every trade fill
CREATE TABLE fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    exchange TEXT NOT NULL,      -- 'hyperliquid' or 'dydx'
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,          -- 'buy' or 'sell'
    size REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    fee_currency TEXT,
    liquidity TEXT NOT NULL,     -- 'maker' or 'taker'
    realized_pnl REAL,
    order_id TEXT
);

-- Funding payments
CREATE TABLE funding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    amount REAL NOT NULL,
    rate REAL
);

-- Pool snapshots for charts (every 30s)
CREATE TABLE pool_snapshots (
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

-- Order events log
CREATE TABLE order_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    exchange TEXT NOT NULL,
    action TEXT NOT NULL,        -- 'place', 'cancel', 'repost', 'fill'
    side TEXT,
    size REAL,
    price REAL,
    reason TEXT                  -- 'exposure_rebalance', 'depth_3rd_level', 'aggressive'
);
```

---

## 8. Web Dashboard

### Stack

- **Starlette:** async HTTP server (runs in the same event loop as the engine)
- **Jinja2:** server-side HTML templates
- **HTMX + SSE:** real-time updates without full page reloads
- **Alpine.js:** minimal client-side interactivity (tabs, modals, toggles)
- **Lightweight chart library:** Chart.js or uPlot for the correlation chart
- **CSS:** Tailwind CSS (via CDN) for a clean, modern look

### SSE Architecture

```
Server:
  EventSource endpoint: GET /sse/state
  Broadcasts StateHub snapshot every time it changes (on price tick)
  Additional endpoint: GET /sse/logs for order activity stream

Client (HTMX):
  <div hx-ext="sse" sse-connect="/sse/state">
    <div sse-swap="state-update">  <!-- swapped on each event -->
      ... dashboard content ...
    </div>
  </div>
```

### Dashboard Layout

```
┌──────────────────────────────────────────────────────────────┐
│  AutoMoney                              [Safe Mode: OFF] ⚙  │
├──────────────┬──────────────┬────────────────────────────────┤
│  POOL        │  HEDGE       │  PnL SUMMARY                  │
│  Value: $204 │  Side: Short │  Pool PnL:    +$4.20          │
│  ARB: 1500   │  Size: $190  │  Hedge PnL:   -$3.80          │
│  WETH: 0.3   │  Entry: 1.05 │  Funding:     +$0.15          │
│  Dep: $200   │  uPnL: -$1.2 │  Fees Earned: +$1.50          │
│              │  Exposure: 2%│  Fees Paid:   -$0.30          │
│              │              │  ─────────────────             │
│              │              │  NET PnL:     +$1.75           │
├──────────────┴──────────────┴────────────────────────────────┤
│  CORRELATION CHART                                           │
│  $  │                                                        │
│     │   ╱──── Pool PnL                                       │
│     │  ╱                                                     │
│  ───│╱──────── Hedge PnL x -1                                │
│     │╲                                                       │
│     │ ╲         ── Net PnL (green line)                       │
│     └──────────────────────────────────── time                │
├──────────────────────────────────────────────────────────────┤
│  ORDERBOOK              │  ACTIVITY LOG                      │
│  ASK 1.0610  [200]      │  12:01:03 REPOST sell @ 1.0609     │
│  ASK 1.0609  [150]      │  12:01:01 CANCEL sell (depth: 3rd) │
│  ─── spread ──          │  12:00:45 FILL sell 50 @ 1.0608 MK │
│  BID 1.0608  [300] ◄ MY │  12:00:30 PLACE sell 50 @ 1.0608   │
│  BID 1.0607  [180]      │  11:59:12 POOL snapshot: $204.20   │
│  BID 1.0606  [90]       │                                     │
├──────────────────────────┴───────────────────────────────────┤
│  REPORTS                                                     │
│  Maker fills: 142 (vol: $12,400)  │  Taker fills: 3 ($580)  │
│  Maker rate: 97.9%                │  Avg fee: -0.012%        │
│  Total corretagem: $1.86          │  Total fees earned: $4.20│
├──────────────────────────────────────────────────────────────┤
│  SETTINGS (editable)                                         │
│  Hedge ratio: [0.95]  Max exposure: [0.05]  Exchange: [HL▼]  │
│  Alert webhook: [https://...]     Repost depth: [3]          │
└──────────────────────────────────────────────────────────────┘
```

### Chart specifics

- **Pool PnL line:** `pool_value_now - pool_deposited`
- **Hedge PnL x -1 line:** `-(hedge_realized + hedge_unrealized + funding)` (inverted so overlap = good hedge)
- **Net PnL line:** sum of both + fees (the actual profit)
- **X axis:** time since deposit
- **Data source:** `pool_snapshots` table (every 10s) + real-time point from StateHub
- Library: **uPlot** (2KB gzipped, renders 100k points without lag -- much lighter than Chart.js)

---

## 9. Security Model

### Key management

| Secret | Storage | Risk scope |
|--------|---------|------------|
| Hot wallet private key | `.env` file (not in repo) | Pool position (~$200) |
| Hyperliquid API key | `.env` (no withdraw permission) | Open hedge positions only |
| dYdX v4 API key | `.env` (no withdraw permission) | Open hedge positions only |
| Hardware wallet | User's physical device | Inaccessible to bot |

### Capital isolation

```
Hardware wallet (main funds)
    │
    ├── Manual transfer: $200 ──► Hot wallet (bot-controlled)
    │                                  ├── Beefy CLM vault (deposit/withdraw)
    │                                  ├── Hyperliquid (margin/trading)
    │                                  └── dYdX v4 (margin/trading)
    │
    └── Manual transfer: profits ◄── Hot wallet
```

### Dashboard security

- Dashboard accessible only via fly.io URL
- Basic auth required (username/password from environment variables `AUTH_USER` and `AUTH_PASS`)
- Starlette `AuthenticationMiddleware` enforces on all routes
- No wallet operations from the dashboard -- only config changes and monitoring

---

## 10. Safe Mode and Alerts

### Triggers

| Trigger | Threshold | Action |
|---------|-----------|--------|
| Exchange WS disconnect | > 10 seconds | Enter safe mode, cancel open orders |
| Chain RPC failure | > 5 consecutive failures | Enter safe mode |
| Order placement failure | > 3 consecutive | Enter safe mode |
| Exposure exceeds 2x limit | > 10% | Enter safe mode, attempt aggressive close |
| Process restart | On startup | Read state from SQLite, recalculate, resume |

### Safe mode behavior

- Cancel all open orders
- Do NOT close hedge position (it protects the pool)
- Stop placing new orders
- Continue reading pool + exchange data (if connected)
- Alert via webhook (configurable: Telegram, Discord, etc.)
- Dashboard shows SAFE MODE prominently

### WebSocket reconnection strategy

```
On disconnect:
  attempt 1: immediate reconnect
  attempt 2: wait 1s
  attempt 3: wait 2s
  attempt 4: wait 4s
  ...exponential backoff, max 30s between attempts
  After 10s total disconnected: enter safe mode
  Continue reconnecting indefinitely in background
  On reconnect: resync orderbook snapshot, verify position, resume if auto-recoverable
```

### Recovery

Manual or automatic:
- If connections restore within 60s, auto-resume
- If longer, require manual confirmation via dashboard button

---

## 11. Deployment (fly.io)

### fly.toml

```toml
app = "automoney"
primary_region = "iad"  # US-East (closest to exchange APIs)

[build]
  dockerfile = "Dockerfile"

[env]
  PYTHONUNBUFFERED = "true"

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = "off"    # CRITICAL: always-on
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

### Dockerfile

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

### Estimated cost

- shared-cpu-1x 256MB always-on: ~$2/month
- Outbound bandwidth (SSE + API calls): ~$0-1/month
- **Total: ~$2-3/month**

---

## 12. Project Structure

```
automoney/
├── app.py                      # Starlette app, startup/shutdown lifecycle
├── config.py                   # Settings from env vars, chain configs
├── db.py                       # SQLite schema, migrations, async queries
├── state.py                    # StateHub dataclass
├── engine/
│   ├── __init__.py
│   ├── hedge.py                # Hedge engine (exposure calc, order decisions)
│   ├── orderbook.py            # Book depth monitoring, price calculation
│   └── pnl.py                  # PnL calculation and aggregation
├── exchanges/
│   ├── __init__.py
│   ├── base.py                 # ExchangeAdapter ABC
│   ├── hyperliquid.py          # Hyperliquid WS + REST
│   └── dydx.py                 # dYdX v4 WS + REST
├── chains/
│   ├── __init__.py
│   ├── base.py                 # ChainReader ABC
│   └── evm.py                  # EVM RPC, Multicall, Beefy CLM ABI
├── web/
│   ├── __init__.py
│   ├── routes.py               # HTTP routes + SSE endpoints
│   ├── templates/
│   │   ├── base.html           # Layout with Tailwind + Alpine.js
│   │   ├── dashboard.html      # Main dashboard page
│   │   └── partials/
│   │       ├── pool.html       # Pool position fragment
│   │       ├── hedge.html      # Hedge position fragment
│   │       ├── pnl.html        # PnL summary fragment
│   │       ├── chart.html      # Correlation chart
│   │       ├── book.html       # Orderbook view
│   │       ├── logs.html       # Activity log
│   │       ├── reports.html    # Maker/taker reports
│   │       └── settings.html   # Config panel
│   └── static/
│       ├── app.js              # Alpine.js components
│       └── chart.js            # uPlot chart setup
├── .env.example                # Template for secrets
├── Dockerfile
├── fly.toml
└── requirements.txt
```

### Dependencies

```
starlette>=0.40
uvicorn>=0.30
jinja2>=3.1
sse-starlette>=2.0
httpx>=0.27           # async HTTP client for REST APIs
websockets>=13.0      # exchange WS connections
web3>=7.0             # EVM RPC calls + ABI encoding
aiosqlite>=0.20       # async SQLite
python-dotenv>=1.0
```

---

## 13. Extensibility

### Adding a new exchange

1. Create `exchanges/newexchange.py` implementing `ExchangeAdapter`
2. Add config entry in `.env`
3. Register in `config.py`

### Adding a new chain

1. Add RPC endpoint + CLM contract address to config
2. `EVMChainReader` handles all EVM chains generically
3. For non-EVM: create new `ChainReader` subclass

### Adding a new pool type

1. Currently: Beefy CLM (wraps Uniswap v3)
2. For raw v2: simpler math (50/50 split), implement `V2PositionCalc`
3. For raw v3: tick/range math, implement `V3PositionCalc`
4. Beefy CLM abstracts this, so raw support is optional

### Adding new pairs

1. Add pair config: `{"symbol": "ETH/USDC", "chain": "arbitrum", "vault": "0x...", "exchange": "hyperliquid"}`
2. Engine supports multiple pairs running concurrently in the same event loop
