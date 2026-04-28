# Operation Lifecycle Implementation Plan (Phase 1.2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adicionar ciclo de vida explícito de operação ao bot (start/stop), com PnL detalhado por operação e cleanup do código legacy.

**Architecture:** Engine consulta `hub.operation_state` antes de placear grade. Operações persistidas em tabela `operations` com baseline + acumuladores. Endpoints REST `/operations/start|stop|current|list`. UI ganha card de operação ativa + aba histórico.

**Tech Stack:** Python 3.14, asyncio, aiosqlite, Starlette, Alpine.js (existing).

**Spec:** [`docs/superpowers/specs/2026-04-27-operation-lifecycle-design.md`](../specs/2026-04-27-operation-lifecycle-design.md)

---

## File Structure

### New
- `engine/operation.py` — Operation dataclass, OperationState enum, transition helpers
- `web/templates/partials/operation.html` — operation card no dashboard
- `web/templates/partials/history.html` — aba histórico
- `tests/test_operation.py`
- `tests/test_integration_operation.py`

### Modified
- `db.py` — operations table, FK `operation_id` em fills/grid_orders/order_log, helpers
- `state.py` — campos de operação
- `engine/__init__.py` — respeita state, métodos start/stop, atribui events
- `engine/pnl.py` — `compute_operation_pnl(op, current_state)`
- `chains/beefy.py` — listener de evento `Harvest`
- `web/routes.py` — endpoints /operations
- `web/templates/dashboard.html` — inclui operation.html + tab Histórico
- `web/static/app.js` — operation state + actions

### Deleted (cleanup)
- `engine/hedge.py` + `tests/test_hedge.py`
- `chains/evm.py` + `tests/test_evm.py`
- `exchanges/hyperliquid.py`
- Asserts em `tests/test_exchanges.py` que dependem de Hyperliquid

---

## Phase A: Foundation (DB + State + Operation entity)

### Task 1: DB schema — operations table + FKs

**Files:**
- Modify: `db.py`
- Modify: `tests/test_db.py`

- [ ] **Step 1: Escrever testes**

Adicione a `tests/test_db.py`:

```python
async def test_insert_and_get_active_operation(db):
    op_id = await db.insert_operation(
        started_at=1000.0, status="starting",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0,
        baseline_collateral=130.0,
    )
    active = await db.get_active_operation()
    assert active is not None
    assert active["id"] == op_id
    assert active["status"] == "starting"


async def test_close_operation(db):
    op_id = await db.insert_operation(
        started_at=1000.0, status="active",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0,
        baseline_collateral=130.0,
    )
    await db.close_operation(op_id, ended_at=2000.0, final_net_pnl=5.50, close_reason="user")
    active = await db.get_active_operation()
    assert active is None
    history = await db.get_operations(limit=10)
    assert len(history) == 1
    assert history[0]["status"] == "closed"
    assert history[0]["final_net_pnl"] == 5.50


async def test_operation_id_fk_in_fills(db):
    """Fills should accept operation_id and surface it on read."""
    op_id = await db.insert_operation(
        started_at=1000.0, status="active",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0,
        baseline_collateral=130.0,
    )
    fill_id = await db.insert_fill(
        timestamp=1500.0, exchange="dydx", symbol="ETH-USD",
        side="sell", size=0.001, price=3000.0, fee=0.0003, fee_currency="USDC",
        liquidity="maker", realized_pnl=0.0, order_id="cl-1",
        operation_id=op_id,
    )
    rows = await db.get_fills()
    assert rows[0]["operation_id"] == op_id


async def test_operation_accumulators_update(db):
    op_id = await db.insert_operation(
        started_at=1000.0, status="active",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0,
        baseline_collateral=130.0,
    )
    await db.add_to_operation_accumulator(op_id, "perp_fees_paid", 0.5)
    await db.add_to_operation_accumulator(op_id, "perp_fees_paid", 0.3)
    await db.add_to_operation_accumulator(op_id, "lp_fees_earned", 2.10)
    op = await db.get_operation(op_id)
    assert abs(op["perp_fees_paid"] - 0.8) < 1e-9
    assert abs(op["lp_fees_earned"] - 2.10) < 1e-9
```

- [ ] **Step 2: Rodar tests**

Run: `python -m pytest tests/test_db.py -v -k operation`
Expected: FAIL — `AttributeError: 'Database' object has no attribute 'insert_operation'`

- [ ] **Step 3: Adicionar schema em db.py**

Append to the SCHEMA constant:

```sql

CREATE TABLE IF NOT EXISTS operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at REAL,
    status TEXT NOT NULL,
    baseline_eth_price REAL,
    baseline_pool_value_usd REAL,
    baseline_amount0 REAL,
    baseline_amount1 REAL,
    baseline_collateral REAL,
    perp_fees_paid REAL DEFAULT 0,
    funding_paid REAL DEFAULT 0,
    lp_fees_earned REAL DEFAULT 0,
    bootstrap_slippage REAL DEFAULT 0,
    final_net_pnl REAL,
    close_reason TEXT
);
CREATE INDEX IF NOT EXISTS idx_operations_active ON operations(status)
    WHERE status IN ('starting', 'active', 'stopping');
```

Add ALTER-style schema for adding `operation_id` columns. Since SQLite supports ADD COLUMN, append:

```sql

ALTER TABLE fills ADD COLUMN operation_id INTEGER;
ALTER TABLE grid_orders ADD COLUMN operation_id INTEGER;
ALTER TABLE order_log ADD COLUMN operation_id INTEGER;
```

Note: For fresh DBs the ALTER runs and adds the column. For existing DBs it would fail if already added. Wrap each ALTER in a try/except in `initialize()`:

```python
async def initialize(self) -> None:
    self._conn = await aiosqlite.connect(self._path)
    self._conn.row_factory = aiosqlite.Row
    await self._conn.executescript(SCHEMA_BASE)
    await self._conn.commit()
    # Optional ADD COLUMN for migrations
    for stmt in (
        "ALTER TABLE fills ADD COLUMN operation_id INTEGER",
        "ALTER TABLE grid_orders ADD COLUMN operation_id INTEGER",
        "ALTER TABLE order_log ADD COLUMN operation_id INTEGER",
    ):
        try:
            await self._conn.execute(stmt)
        except Exception:
            pass  # column already exists
    await self._conn.commit()
```

Split the SCHEMA constant into `SCHEMA_BASE` (without the ALTERs) and add the ALTERs separately as above.

- [ ] **Step 4: Adicionar métodos a Database**

```python
async def insert_operation(
    self, *, started_at: float, status: str,
    baseline_eth_price: float, baseline_pool_value_usd: float,
    baseline_amount0: float, baseline_amount1: float, baseline_collateral: float,
) -> int:
    cursor = await self._conn.execute(
        """INSERT INTO operations
           (started_at, status, baseline_eth_price, baseline_pool_value_usd,
            baseline_amount0, baseline_amount1, baseline_collateral)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (started_at, status, baseline_eth_price, baseline_pool_value_usd,
         baseline_amount0, baseline_amount1, baseline_collateral),
    )
    await self._conn.commit()
    return cursor.lastrowid


async def update_operation_status(self, op_id: int, status: str) -> None:
    await self._conn.execute(
        "UPDATE operations SET status = ? WHERE id = ?", (status, op_id),
    )
    await self._conn.commit()


async def close_operation(
    self, op_id: int, *, ended_at: float, final_net_pnl: float, close_reason: str,
) -> None:
    await self._conn.execute(
        """UPDATE operations
           SET status = 'closed', ended_at = ?, final_net_pnl = ?, close_reason = ?
           WHERE id = ?""",
        (ended_at, final_net_pnl, close_reason, op_id),
    )
    await self._conn.commit()


async def get_operation(self, op_id: int) -> dict | None:
    cursor = await self._conn.execute(
        "SELECT * FROM operations WHERE id = ?", (op_id,),
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_active_operation(self) -> dict | None:
    cursor = await self._conn.execute(
        """SELECT * FROM operations
           WHERE status IN ('starting', 'active', 'stopping')
           ORDER BY started_at DESC LIMIT 1"""
    )
    row = await cursor.fetchone()
    return dict(row) if row else None


async def get_operations(self, limit: int = 20) -> list[dict]:
    cursor = await self._conn.execute(
        "SELECT * FROM operations ORDER BY started_at DESC LIMIT ?", (limit,),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def add_to_operation_accumulator(self, op_id: int, field: str, delta: float) -> None:
    """Atomically add `delta` to one of: perp_fees_paid, funding_paid,
    lp_fees_earned, bootstrap_slippage."""
    allowed = {"perp_fees_paid", "funding_paid", "lp_fees_earned", "bootstrap_slippage"}
    if field not in allowed:
        raise ValueError(f"field must be one of {allowed}, got {field}")
    await self._conn.execute(
        f"UPDATE operations SET {field} = {field} + ? WHERE id = ?",
        (delta, op_id),
    )
    await self._conn.commit()
```

Modify `insert_fill` signature to accept optional `operation_id`:

```python
async def insert_fill(
    self, *, timestamp, exchange, symbol, side, size, price, fee, fee_currency,
    liquidity, realized_pnl, order_id, operation_id: int | None = None,
) -> int:
    cursor = await self._conn.execute(
        """INSERT INTO fills (timestamp, exchange, symbol, side, size, price,
            fee, fee_currency, liquidity, realized_pnl, order_id, operation_id)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (timestamp, exchange, symbol, side, size, price, fee, fee_currency,
         liquidity, realized_pnl, order_id, operation_id),
    )
    await self._conn.commit()
    return cursor.lastrowid
```

Same for `insert_grid_order` and `insert_order_log` — add optional `operation_id` parameter; existing callers pass None.

- [ ] **Step 5: Rodar tests**

Run: `python -m pytest tests/test_db.py -v`
Expected: PASS (todos)

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat(task-1): operations table with baselines, accumulators, and FK columns"
```

---

### Task 2: state.py — operation fields

**Files:**
- Modify: `state.py`
- Modify: `tests/test_state.py`

- [ ] **Step 1: Escrever teste**

```python
def test_statehub_operation_fields_default():
    from state import StateHub
    s = StateHub()
    assert s.current_operation_id is None
    assert s.operation_state == "none"
    assert s.operation_pnl_breakdown == {}
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_state.py::test_statehub_operation_fields_default -v`
Expected: FAIL com AttributeError

- [ ] **Step 3: Adicionar campos em state.py**

Add to StateHub dataclass (apply by inserting before the `to_dict` method):

```python
# Operation lifecycle
current_operation_id: int | None = None
operation_state: str = "none"  # none/starting/active/stopping/closed/failed
operation_pnl_breakdown: dict = field(default_factory=dict)
```

- [ ] **Step 4: Rodar tests**

Run: `python -m pytest tests/test_state.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add state.py tests/test_state.py
git commit -m "feat(task-2): operation lifecycle fields in StateHub"
```

---

### Task 3: Operation dataclass + state machine

**Files:**
- Create: `engine/operation.py`
- Test: `tests/test_operation.py`

- [ ] **Step 1: Escrever testes**

```python
# tests/test_operation.py
import pytest
from engine.operation import Operation, OperationState, can_transition


def test_operation_initial_state():
    op = Operation(
        id=1, started_at=1000.0, state=OperationState.STARTING,
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0, baseline_collateral=130.0,
    )
    assert op.state == OperationState.STARTING
    assert op.is_active()


def test_can_transition_valid():
    assert can_transition(OperationState.NONE, OperationState.STARTING)
    assert can_transition(OperationState.STARTING, OperationState.ACTIVE)
    assert can_transition(OperationState.ACTIVE, OperationState.STOPPING)
    assert can_transition(OperationState.STOPPING, OperationState.CLOSED)


def test_can_transition_invalid():
    assert not can_transition(OperationState.NONE, OperationState.ACTIVE)
    assert not can_transition(OperationState.CLOSED, OperationState.ACTIVE)
    assert not can_transition(OperationState.STARTING, OperationState.NONE)


def test_failed_transition_from_any_active():
    """Any non-terminal state can transition to FAILED."""
    assert can_transition(OperationState.STARTING, OperationState.FAILED)
    assert can_transition(OperationState.ACTIVE, OperationState.FAILED)
    assert can_transition(OperationState.STOPPING, OperationState.FAILED)


def test_is_active_includes_starting_active_stopping():
    for st in (OperationState.STARTING, OperationState.ACTIVE, OperationState.STOPPING):
        op = Operation(
            id=1, started_at=1000.0, state=st,
            baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
            baseline_amount0=0.05, baseline_amount1=150.0, baseline_collateral=130.0,
        )
        assert op.is_active()


def test_is_active_excludes_terminal():
    for st in (OperationState.NONE, OperationState.CLOSED, OperationState.FAILED):
        op = Operation(
            id=1, started_at=1000.0, state=st,
            baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
            baseline_amount0=0.05, baseline_amount1=150.0, baseline_collateral=130.0,
        )
        assert not op.is_active()


def test_from_db_row():
    row = {
        "id": 5, "started_at": 1000.0, "ended_at": None,
        "status": "active",
        "baseline_eth_price": 3000.0, "baseline_pool_value_usd": 300.0,
        "baseline_amount0": 0.05, "baseline_amount1": 150.0,
        "baseline_collateral": 130.0,
        "perp_fees_paid": 1.5, "funding_paid": 0.3,
        "lp_fees_earned": 2.1, "bootstrap_slippage": 0.07,
        "final_net_pnl": None, "close_reason": None,
    }
    op = Operation.from_db_row(row)
    assert op.id == 5
    assert op.state == OperationState.ACTIVE
    assert op.perp_fees_paid == 1.5
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_operation.py -v`
Expected: FAIL — module not found

- [ ] **Step 3: Implementar engine/operation.py**

```python
# engine/operation.py
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum


class OperationState(str, Enum):
    NONE = "none"
    STARTING = "starting"
    ACTIVE = "active"
    STOPPING = "stopping"
    CLOSED = "closed"
    FAILED = "failed"


_VALID_TRANSITIONS: dict[OperationState, set[OperationState]] = {
    OperationState.NONE: {OperationState.STARTING},
    OperationState.STARTING: {OperationState.ACTIVE, OperationState.FAILED},
    OperationState.ACTIVE: {OperationState.STOPPING, OperationState.FAILED},
    OperationState.STOPPING: {OperationState.CLOSED, OperationState.FAILED},
    OperationState.CLOSED: set(),
    OperationState.FAILED: set(),
}


def can_transition(from_state: OperationState, to_state: OperationState) -> bool:
    return to_state in _VALID_TRANSITIONS.get(from_state, set())


@dataclass
class Operation:
    id: int
    started_at: float
    state: OperationState
    baseline_eth_price: float
    baseline_pool_value_usd: float
    baseline_amount0: float
    baseline_amount1: float
    baseline_collateral: float
    ended_at: float | None = None
    perp_fees_paid: float = 0.0
    funding_paid: float = 0.0
    lp_fees_earned: float = 0.0
    bootstrap_slippage: float = 0.0
    final_net_pnl: float | None = None
    close_reason: str | None = None

    def is_active(self) -> bool:
        return self.state in (
            OperationState.STARTING, OperationState.ACTIVE, OperationState.STOPPING,
        )

    @classmethod
    def from_db_row(cls, row: dict) -> "Operation":
        return cls(
            id=row["id"],
            started_at=row["started_at"],
            ended_at=row.get("ended_at"),
            state=OperationState(row["status"]),
            baseline_eth_price=row["baseline_eth_price"],
            baseline_pool_value_usd=row["baseline_pool_value_usd"],
            baseline_amount0=row["baseline_amount0"],
            baseline_amount1=row["baseline_amount1"],
            baseline_collateral=row["baseline_collateral"],
            perp_fees_paid=row.get("perp_fees_paid", 0.0) or 0.0,
            funding_paid=row.get("funding_paid", 0.0) or 0.0,
            lp_fees_earned=row.get("lp_fees_earned", 0.0) or 0.0,
            bootstrap_slippage=row.get("bootstrap_slippage", 0.0) or 0.0,
            final_net_pnl=row.get("final_net_pnl"),
            close_reason=row.get("close_reason"),
        )
```

- [ ] **Step 4: Rodar tests**

Run: `python -m pytest tests/test_operation.py -v`
Expected: PASS (7 tests)

- [ ] **Step 5: Commit**

```bash
git add engine/operation.py tests/test_operation.py
git commit -m "feat(task-3): Operation dataclass with state machine and from_db_row"
```

---

### Task 4: PnL — compute_operation_pnl

**Files:**
- Modify: `engine/pnl.py`
- Modify: `tests/test_pnl.py`

- [ ] **Step 1: Escrever teste**

```python
# Adicionar a tests/test_pnl.py
from engine.pnl import compute_operation_pnl
from engine.operation import Operation, OperationState


def test_operation_pnl_breakdown():
    op = Operation(
        id=1, started_at=1000.0, state=OperationState.ACTIVE,
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0,
        baseline_collateral=130.0,
        perp_fees_paid=0.5,
        funding_paid=-1.0,    # bot received +$1 funding
        lp_fees_earned=2.1,
        bootstrap_slippage=0.07,
    )
    breakdown = compute_operation_pnl(
        op,
        current_pool_value_usd=298.0,
        current_eth_price=2950.0,
        hedge_realized_since_baseline=10.0,
        hedge_unrealized_since_baseline=2.0,
    )
    # HODL = baseline_amount0 * current_eth_price + baseline_amount1
    #     = 0.05 * 2950 + 150 = 297.5
    # IL natural = -(HODL - pool) = -(297.5 - 298.0) = +0.5  (pool actually higher than HODL, gain)
    assert abs(breakdown["lp_fees_earned"] - 2.1) < 1e-9
    assert abs(breakdown["beefy_perf_fee"] - (-0.21)) < 1e-9  # 10% of 2.1
    assert abs(breakdown["il_natural"] - 0.5) < 1e-9
    assert abs(breakdown["hedge_pnl"] - 12.0) < 1e-9
    assert abs(breakdown["funding"] - 1.0) < 1e-9  # negative paid means received
    assert abs(breakdown["perp_fees_paid"] - (-0.5)) < 1e-9
    assert abs(breakdown["bootstrap_slippage"] - (-0.07)) < 1e-9
    # net = 2.1 - 0.21 + 0.5 + 12.0 + 1.0 - 0.5 - 0.07 = 14.82
    assert abs(breakdown["net_pnl"] - 14.82) < 0.01
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_pnl.py::test_operation_pnl_breakdown -v`
Expected: FAIL — `ImportError: cannot import name 'compute_operation_pnl'`

- [ ] **Step 3: Implementar compute_operation_pnl**

Add to `engine/pnl.py`:

```python
from engine.operation import Operation


BEEFY_PERF_FEE_RATE = 0.10  # Beefy takes ~10% of fees


def compute_operation_pnl(
    op: Operation,
    *,
    current_pool_value_usd: float,
    current_eth_price: float,
    hedge_realized_since_baseline: float,
    hedge_unrealized_since_baseline: float,
) -> dict:
    """Returns the live PnL breakdown for an active operation.

    Sign convention: positive = profit, negative = loss.
    funding: positive if bot received (longs paid), negative if bot paid.
    op.funding_paid stores it as "paid by us" so we negate to get the breakdown.
    """
    hodl_value = op.baseline_amount0 * current_eth_price + op.baseline_amount1
    # IL natural is the loss vs HODL — express as gain/loss vs baseline pool
    il_natural = current_pool_value_usd - hodl_value

    hedge_pnl = hedge_realized_since_baseline + hedge_unrealized_since_baseline

    beefy_perf = -BEEFY_PERF_FEE_RATE * op.lp_fees_earned

    breakdown = {
        "lp_fees_earned": op.lp_fees_earned,
        "beefy_perf_fee": beefy_perf,
        "il_natural": il_natural,
        "hedge_pnl": hedge_pnl,
        "funding": -op.funding_paid,  # negate: stored as paid, breakdown shows received
        "perp_fees_paid": -op.perp_fees_paid,
        "bootstrap_slippage": -op.bootstrap_slippage,
    }
    breakdown["net_pnl"] = sum(breakdown.values())
    return breakdown
```

- [ ] **Step 4: Rodar tests**

Run: `python -m pytest tests/test_pnl.py -v`
Expected: PASS (todos os antigos + 1 novo)

- [ ] **Step 5: Commit**

```bash
git add engine/pnl.py tests/test_pnl.py
git commit -m "feat(task-4): compute_operation_pnl with full breakdown"
```

---

## Phase B: Engine integration

### Task 5: Engine respeita operation_state

**Files:**
- Modify: `engine/__init__.py`
- Modify: `tests/test_engine_grid.py`

- [ ] **Step 1: Escrever teste**

Adicionar a `tests/test_engine_grid.py`:

```python
@pytest.mark.asyncio
async def test_engine_skips_grid_when_operation_state_none():
    """When operation_state == 'none', engine reads chain but does NOT placea grade."""
    from engine import GridMakerEngine
    from state import StateHub

    state = StateHub(hedge_ratio=1.0)
    state.operation_state = "none"

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.alert_webhook_url = ""
    settings.threshold_aggressive = 0.05
    settings.threshold_recovery = 0.02
    settings.max_open_orders = 50
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    db = MagicMock()
    db.get_active_grid_orders = AsyncMock(return_value=[])

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=0.001))
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.batch_place = AsyncMock(return_value=[])
    exchange.batch_cancel = AsyncMock(return_value=0)
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])

    pool_reader = MagicMock()
    pool_reader.read_price = AsyncMock(return_value=3000.0)
    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.5, amount1=1500.0, share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
    )
    await engine._iterate()

    # Chain state read happened
    assert state.range_lower > 0
    # But NO grid placement
    exchange.batch_place.assert_not_called()
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_engine_grid.py::test_engine_skips_grid_when_operation_state_none -v`
Expected: FAIL — bot still places grid even with operation_state none

- [ ] **Step 3: Modificar `_iterate` em engine/__init__.py**

Inserir guard após `self._hub.out_of_range = False` e ANTES de `# 3. Compute target grid`:

```python
        # 2.5. If no active operation, stop here — read state but skip grid
        if self._hub.operation_state != "active":
            self._hub.last_update = time.time()
            return
```

- [ ] **Step 4: Rodar tests**

Run: `python -m pytest tests/test_engine_grid.py -v`
Expected: PASS (todos)

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_engine_grid.py
git commit -m "feat(task-5): engine skips grid placement when no active operation"
```

---

### Task 6: Engine — start_operation

**Files:**
- Modify: `engine/__init__.py`
- Modify: `tests/test_engine_grid.py`

- [ ] **Step 1: Escrever teste**

```python
@pytest.mark.asyncio
async def test_engine_start_operation(tmp_path):
    """start_operation grava baseline, marca state ACTIVE, e dispara bootstrap."""
    from db import Database
    from engine import GridMakerEngine
    from state import StateHub
    from exchanges.base import Order

    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.alert_webhook_url = ""
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    bootstrap_calls = []

    async def fake_place_long_term_order(**kw):
        bootstrap_calls.append(kw)
        return Order(
            order_id=str(kw["cloid_int"]), symbol=kw["symbol"], side=kw["side"],
            size=kw["size"], price=kw["price"], status="open",
        )

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=0.001))
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.place_long_term_order = AsyncMock(side_effect=fake_place_long_term_order)

    pool_reader = MagicMock()
    pool_reader.read_price = AsyncMock(return_value=3000.0)
    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.5, amount1=1500.0, share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
    )

    op_id = await engine.start_operation()

    assert state.current_operation_id == op_id
    assert state.operation_state == "active"

    # Baseline persisted
    op = await db.get_operation(op_id)
    assert op["status"] == "active"
    assert op["baseline_eth_price"] == 3000.0
    assert op["baseline_pool_value_usd"] > 0

    # Bootstrap order placed (taker for opening short)
    assert len(bootstrap_calls) == 1
    assert bootstrap_calls[0]["side"] == "sell"  # short = sell

    await db.close()


@pytest.mark.asyncio
async def test_engine_start_operation_rejects_when_already_active(tmp_path):
    """Cannot start a new op when one is already active/starting."""
    from db import Database
    from engine import GridMakerEngine
    from state import StateHub

    db = Database(str(tmp_path / "t2.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)
    # Pre-existing active op in DB
    await db.insert_operation(
        started_at=1000.0, status="active",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0, baseline_collateral=130.0,
    )

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.alert_webhook_url = ""
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=MagicMock(), pool_reader=MagicMock(), beefy_reader=MagicMock(),
    )

    with pytest.raises(RuntimeError, match="already active"):
        await engine.start_operation()

    await db.close()
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_engine_grid.py -k start_operation -v`
Expected: FAIL — `AttributeError: 'GridMakerEngine' object has no attribute 'start_operation'`

- [ ] **Step 3: Implementar start_operation**

Add imports at the top of `engine/__init__.py`:

```python
from engine.operation import Operation, OperationState
from engine.curve import compute_x as _compute_x
```

(`_compute_x` may already be imported as `compute_x`; reuse the existing import.)

Add method to `GridMakerEngine`:

```python
async def start_operation(self) -> int:
    """Begin a new operation: snapshot baseline, bootstrap short, mark ACTIVE."""
    existing = await self._db.get_active_operation()
    if existing is not None:
        raise RuntimeError(f"Operation {existing['id']} already active")

    # Snapshot baseline
    p_now = await self._pool_reader.read_price()
    beefy_pos = await self._beefy_reader.read_position()
    my_amount0 = beefy_pos.amount0 * beefy_pos.share
    my_amount1 = beefy_pos.amount1 * beefy_pos.share
    pool_value = my_amount0 * p_now + my_amount1
    try:
        collateral = await self._exchange.get_collateral()
    except Exception:
        collateral = 0.0

    op_id = await self._db.insert_operation(
        started_at=time.time(), status=OperationState.STARTING.value,
        baseline_eth_price=p_now,
        baseline_pool_value_usd=pool_value,
        baseline_amount0=my_amount0,
        baseline_amount1=my_amount1,
        baseline_collateral=collateral,
    )
    self._hub.current_operation_id = op_id
    self._hub.operation_state = OperationState.STARTING.value

    # Bootstrap: open short = my_amount0 * hedge_ratio via taker
    target_short = my_amount0 * self._hub.hedge_ratio
    if target_short > 0:
        meta = await self._exchange.get_market_meta(self._settings.dydx_symbol)
        try:
            await self._exchange.place_long_term_order(
                symbol=self._settings.dydx_symbol,
                side="sell", size=target_short,
                price=p_now * 0.999,  # cross spread (taker)
                cloid_int=self._next_cloid(998),
                ttl_seconds=60,
            )
            # Slippage estimate: 5 bps of notional
            slippage = 0.0005 * target_short * p_now
            await self._db.add_to_operation_accumulator(
                op_id, "bootstrap_slippage", slippage,
            )
        except Exception as e:
            logger.exception(f"Bootstrap failed: {e}")
            await self._db.update_operation_status(op_id, OperationState.FAILED.value)
            self._hub.operation_state = OperationState.FAILED.value
            self._hub.current_operation_id = None
            raise

    await self._db.update_operation_status(op_id, OperationState.ACTIVE.value)
    self._hub.operation_state = OperationState.ACTIVE.value
    logger.info(f"Operation {op_id} started")
    return op_id
```

- [ ] **Step 4: Rodar tests**

Run: `python -m pytest tests/test_engine_grid.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_engine_grid.py
git commit -m "feat(task-6): start_operation snapshots baseline and bootstraps short"
```

---

### Task 7: Engine — stop_operation

**Files:**
- Modify: `engine/__init__.py`
- Modify: `tests/test_engine_grid.py`

- [ ] **Step 1: Escrever teste**

```python
@pytest.mark.asyncio
async def test_engine_stop_operation(tmp_path):
    """stop_operation cancela grade, fecha short via taker, grava final_pnl."""
    from db import Database
    from engine import GridMakerEngine
    from state import StateHub

    db = Database(str(tmp_path / "t3.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)

    # Pre-create active operation
    op_id = await db.insert_operation(
        started_at=1000.0, status="active",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0, baseline_collateral=130.0,
    )
    state.current_operation_id = op_id
    state.operation_state = "active"

    # Pre-seed an active grid order
    await db.insert_grid_order(
        cloid="500", side="sell", target_price=2900.0, size=0.001, placed_at=1100.0,
    )

    cancelled_calls = []

    async def fake_cancel(items):
        cancelled_calls.extend(items)
        return len(items)

    closed_calls = []

    async def fake_place(**kw):
        closed_calls.append(kw)
        from exchanges.base import Order
        return Order(order_id=str(kw["cloid_int"]), symbol=kw["symbol"],
                     side=kw["side"], size=kw["size"], price=kw["price"], status="open")

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.alert_webhook_url = ""
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.batch_cancel = AsyncMock(side_effect=fake_cancel)
    exchange.place_long_term_order = AsyncMock(side_effect=fake_place)
    exchange.get_position = AsyncMock(return_value=MagicMock(
        side="short", size=0.05, entry_price=3000.0, unrealized_pnl=0.0,
    ))
    exchange.get_collateral = AsyncMock(return_value=128.0)

    pool_reader = MagicMock()
    pool_reader.read_price = AsyncMock(return_value=2950.0)
    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.5, amount1=1500.0, share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
    )

    result = await engine.stop_operation()

    assert state.current_operation_id is None
    assert state.operation_state == "none"
    op = await db.get_operation(op_id)
    assert op["status"] == "closed"
    assert op["close_reason"] == "user"
    assert op["final_net_pnl"] is not None
    # Cancelled the grid
    assert len(cancelled_calls) >= 1
    # Closed via taker (buy to cover the short)
    assert len(closed_calls) >= 1
    assert closed_calls[0]["side"] == "buy"

    await db.close()
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_engine_grid.py::test_engine_stop_operation -v`
Expected: FAIL

- [ ] **Step 3: Implementar stop_operation**

Add to `GridMakerEngine`:

```python
async def stop_operation(self, *, close_reason: str = "user") -> dict:
    """Encerra a operação ativa: cancela grade, fecha short, grava final_pnl."""
    op_row = await self._db.get_active_operation()
    if op_row is None:
        raise RuntimeError("No active operation to stop")
    op_id = op_row["id"]

    await self._db.update_operation_status(op_id, OperationState.STOPPING.value)
    self._hub.operation_state = OperationState.STOPPING.value

    # 1. Cancel all active grid orders
    active_orders = await self._db.get_active_grid_orders()
    if active_orders:
        try:
            await self._exchange.batch_cancel([
                dict(symbol=self._settings.dydx_symbol, cloid_int=int(r["cloid"]))
                for r in active_orders
            ])
            for r in active_orders:
                await self._db.mark_grid_order_cancelled(r["cloid"], time.time())
        except Exception as e:
            logger.error(f"Cancel grid during stop failed: {e}")

    # 2. Close short via taker
    pos = await self._exchange.get_position(self._settings.dydx_symbol)
    if pos and pos.size > 0:
        p_now = await self._pool_reader.read_price()
        side = "buy" if pos.side == "short" else "sell"
        # Cross spread for fast fill
        price = p_now * 1.001 if side == "buy" else p_now * 0.999
        try:
            await self._exchange.place_long_term_order(
                symbol=self._settings.dydx_symbol,
                side=side, size=pos.size, price=price,
                cloid_int=self._next_cloid(997), ttl_seconds=60,
            )
            slippage = 0.0005 * pos.size * p_now
            await self._db.add_to_operation_accumulator(
                op_id, "perp_fees_paid", slippage,
            )
        except Exception as e:
            logger.exception(f"Close short during stop failed: {e}")

    # 3. Compute final PnL
    op = Operation.from_db_row(await self._db.get_operation(op_id))
    p_now = await self._pool_reader.read_price()
    beefy_pos = await self._beefy_reader.read_position()
    my_amount0 = beefy_pos.amount0 * beefy_pos.share
    my_amount1 = beefy_pos.amount1 * beefy_pos.share
    pool_value = my_amount0 * p_now + my_amount1

    from engine.pnl import compute_operation_pnl
    breakdown = compute_operation_pnl(
        op,
        current_pool_value_usd=pool_value,
        current_eth_price=p_now,
        hedge_realized_since_baseline=self._hub.hedge_realized_pnl,
        hedge_unrealized_since_baseline=self._hub.hedge_unrealized_pnl,
    )

    await self._db.close_operation(
        op_id, ended_at=time.time(),
        final_net_pnl=breakdown["net_pnl"], close_reason=close_reason,
    )
    self._hub.current_operation_id = None
    self._hub.operation_state = OperationState.NONE.value
    self._hub.operation_pnl_breakdown = {}
    logger.info(f"Operation {op_id} closed; final PnL = {breakdown['net_pnl']:.2f}")
    return {"id": op_id, "final_net_pnl": breakdown["net_pnl"], "breakdown": breakdown}
```

- [ ] **Step 4: Rodar tests**

Run: `python -m pytest tests/test_engine_grid.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_engine_grid.py
git commit -m "feat(task-7): stop_operation cancels grid, closes short, computes final PnL"
```

---

### Task 8: Engine — atribuir fills à operação ativa

**Files:**
- Modify: `engine/__init__.py`
- Modify: `tests/test_engine_grid.py`

- [ ] **Step 1: Escrever teste**

```python
@pytest.mark.asyncio
async def test_engine_fill_attributed_to_active_operation(tmp_path):
    """When a fill arrives during an active operation, it gets operation_id."""
    from db import Database
    from engine import GridMakerEngine
    from state import StateHub
    from exchanges.base import Fill

    db = Database(str(tmp_path / "t4.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)

    op_id = await db.insert_operation(
        started_at=1000.0, status="active",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0, baseline_collateral=130.0,
    )
    state.current_operation_id = op_id
    state.operation_state = "active"

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"

    exchange = MagicMock()
    exchange.name = "dydx"

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=MagicMock(), beefy_reader=MagicMock(),
    )

    fill = Fill(
        fill_id="f1", order_id="100", symbol="ETH-USD", side="sell", size=0.001,
        price=2999.0, fee=0.0003, fee_currency="USDC", liquidity="maker",
        realized_pnl=0.0, timestamp=1500.0,
    )
    await engine._on_fill(fill)

    fills = await db.get_fills()
    assert len(fills) == 1
    assert fills[0]["operation_id"] == op_id

    op = await db.get_operation(op_id)
    assert abs(op["perp_fees_paid"] - 0.0003) < 1e-9

    await db.close()
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_engine_grid.py::test_engine_fill_attributed_to_active_operation -v`
Expected: FAIL — operation_id is None or accumulator not updated

- [ ] **Step 3: Modificar `_on_fill` em GridMakerEngine**

Replace existing `_on_fill` body:

```python
async def _on_fill(self, fill):
    """Handle a fill event from the exchange WS, attribute to active operation."""
    op_id = self._hub.current_operation_id  # may be None

    fill_id = await self._db.insert_fill(
        timestamp=fill.timestamp, exchange=self._exchange.name,
        symbol=fill.symbol, side=fill.side, size=fill.size, price=fill.price,
        fee=fill.fee, fee_currency=fill.fee_currency, liquidity=fill.liquidity,
        realized_pnl=fill.realized_pnl, order_id=fill.order_id,
        operation_id=op_id,
    )

    if fill.order_id:
        try:
            await self._db.mark_grid_order_filled(fill.order_id, fill_id)
        except Exception:
            pass

    if fill.liquidity == "maker":
        self._hub.total_maker_fills += 1
        self._hub.total_maker_volume += fill.size
    else:
        self._hub.total_taker_fills += 1
        self._hub.total_taker_volume += fill.size
    self._hub.total_fees_paid += fill.fee
    self._hub.hedge_realized_pnl += fill.realized_pnl
    self._hub.last_update = time.time()

    # Attribute fee to the active operation
    if op_id is not None and fill.fee > 0:
        await self._db.add_to_operation_accumulator(op_id, "perp_fees_paid", fill.fee)

    await self._db.insert_order_log(
        timestamp=time.time(), exchange=self._exchange.name,
        action="fill", side=fill.side, size=fill.size, price=fill.price,
        reason=fill.liquidity, operation_id=op_id,
    )
```

Also modify `_iterate` and any `db.insert_grid_order` / `db.insert_order_log` calls inside engine to pass `operation_id=self._hub.current_operation_id`. Search for those calls and add the kwarg.

- [ ] **Step 4: Rodar tests**

Run: `python -m pytest tests/test_engine_grid.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_engine_grid.py
git commit -m "feat(task-8): attribute fills/grid_orders/order_log to active operation"
```

---

### Task 9: Engine — live PnL breakdown no hub

**Files:**
- Modify: `engine/__init__.py`
- Modify: `tests/test_engine_grid.py`

- [ ] **Step 1: Escrever teste**

```python
@pytest.mark.asyncio
async def test_engine_updates_live_pnl_breakdown(tmp_path):
    """During _iterate, hub.operation_pnl_breakdown is updated when op is active."""
    from db import Database
    from engine import GridMakerEngine
    from state import StateHub

    db = Database(str(tmp_path / "t5.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)

    op_id = await db.insert_operation(
        started_at=1000.0, status="active",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0, baseline_collateral=130.0,
    )
    state.current_operation_id = op_id
    state.operation_state = "active"

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.alert_webhook_url = ""
    settings.threshold_aggressive = 0.05
    settings.threshold_recovery = 0.02
    settings.max_open_orders = 50
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=0.001))
    exchange.get_position = AsyncMock(return_value=MagicMock(
        side="short", size=0.05, entry_price=3000.0, unrealized_pnl=0.0,
    ))
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.batch_place = AsyncMock(return_value=[])
    exchange.batch_cancel = AsyncMock(return_value=0)
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])

    pool_reader = MagicMock()
    pool_reader.read_price = AsyncMock(return_value=3000.0)
    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.5, amount1=1500.0, share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
    )

    await engine._iterate()

    assert "lp_fees_earned" in state.operation_pnl_breakdown
    assert "net_pnl" in state.operation_pnl_breakdown
    assert isinstance(state.operation_pnl_breakdown["net_pnl"], (int, float))

    await db.close()
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_engine_grid.py::test_engine_updates_live_pnl_breakdown -v`
Expected: FAIL — `operation_pnl_breakdown` empty

- [ ] **Step 3: Adicionar update no `_iterate`**

In `_iterate`, after computing `pool_value` and before the out-of-range short-circuits, add the breakdown update if operation is active. Insert just before `# 6. Update margin/collateral`:

```python
        # Live PnL breakdown for active operation
        if self._hub.current_operation_id is not None:
            try:
                op_row = await self._db.get_operation(self._hub.current_operation_id)
                if op_row:
                    from engine.operation import Operation
                    from engine.pnl import compute_operation_pnl
                    op = Operation.from_db_row(op_row)
                    self._hub.operation_pnl_breakdown = compute_operation_pnl(
                        op,
                        current_pool_value_usd=my_value,
                        current_eth_price=p_now,
                        hedge_realized_since_baseline=self._hub.hedge_realized_pnl,
                        hedge_unrealized_since_baseline=self._hub.hedge_unrealized_pnl,
                    )
            except Exception as e:
                logger.error(f"PnL breakdown update failed: {e}")
```

- [ ] **Step 4: Rodar tests**

Run: `python -m pytest tests/test_engine_grid.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_engine_grid.py
git commit -m "feat(task-9): publish operation_pnl_breakdown live in StateHub"
```

---

### Task 10: Recovery — retomar operação ativa após restart

**Files:**
- Modify: `engine/__init__.py`
- Modify: `tests/test_engine_grid.py`

- [ ] **Step 1: Escrever teste**

```python
@pytest.mark.asyncio
async def test_engine_recovery_restores_active_operation(tmp_path):
    """On start(), if DB has an active operation, restore it to hub."""
    from db import Database
    from engine import GridMakerEngine
    from state import StateHub

    db = Database(str(tmp_path / "t6.db"))
    await db.initialize()

    op_id = await db.insert_operation(
        started_at=1000.0, status="active",
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0, baseline_collateral=130.0,
    )

    state = StateHub()
    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"

    exchange = MagicMock()
    exchange.connect = AsyncMock()
    exchange.disconnect = AsyncMock()
    exchange.subscribe_fills = AsyncMock()
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])
    exchange.name = "dydx"

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=MagicMock(), beefy_reader=MagicMock(),
    )
    engine._iterate = AsyncMock()
    await engine.start()

    assert state.current_operation_id == op_id
    assert state.operation_state == "active"

    await engine.stop()
    await db.close()
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_engine_grid.py::test_engine_recovery_restores_active_operation -v`
Expected: FAIL — current_operation_id stays None

- [ ] **Step 3: Adicionar recovery no start()**

In `start()`, after the initial reconciliation block, add:

```python
        # Restore active operation, if any
        active_op = await self._db.get_active_operation()
        if active_op is not None:
            self._hub.current_operation_id = active_op["id"]
            self._hub.operation_state = active_op["status"]
            logger.info(f"Restored active operation {active_op['id']} (status={active_op['status']})")
```

- [ ] **Step 4: Rodar tests**

Run: `python -m pytest tests/test_engine_grid.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_engine_grid.py
git commit -m "feat(task-10): restore active operation in StateHub on engine start"
```

---

## Phase C: API + UI

### Task 11: REST endpoints — /operations

**Files:**
- Modify: `web/routes.py`
- Modify: `app.py`
- Modify: `tests/test_web.py`

- [ ] **Step 1: Escrever teste**

Add to `tests/test_web.py`:

```python
def test_operations_endpoints_exist(app):
    import base64
    from starlette.testclient import TestClient
    creds = base64.b64encode(b"admin:secret").decode()
    headers = {"Authorization": f"Basic {creds}"}

    client = TestClient(app)
    # GET /operations should return 200 with empty list
    resp = client.get("/operations", headers=headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)

    # GET /operations/current should return 204 when none active
    resp = client.get("/operations/current", headers=headers)
    assert resp.status_code == 204
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_web.py::test_operations_endpoints_exist -v`
Expected: FAIL — 404 since routes don't exist

- [ ] **Step 3: Adicionar endpoints em web/routes.py**

```python
from starlette.responses import JSONResponse, Response


async def list_operations(request: Request):
    db = request.app.state.db
    limit = int(request.query_params.get("limit", "20"))
    rows = await db.get_operations(limit=limit)
    return JSONResponse(rows)


async def get_current_operation(request: Request):
    db = request.app.state.db
    hub = request.app.state.hub
    op = await db.get_active_operation()
    if op is None:
        return Response(status_code=204)
    return JSONResponse({
        "id": op["id"],
        "status": op["status"],
        "started_at": op["started_at"],
        "baseline": {
            "eth_price": op["baseline_eth_price"],
            "pool_value_usd": op["baseline_pool_value_usd"],
            "amount0": op["baseline_amount0"],
            "amount1": op["baseline_amount1"],
            "collateral": op["baseline_collateral"],
        },
        "accumulators": {
            "perp_fees_paid": op["perp_fees_paid"],
            "funding_paid": op["funding_paid"],
            "lp_fees_earned": op["lp_fees_earned"],
            "bootstrap_slippage": op["bootstrap_slippage"],
        },
        "current_pnl_breakdown": dict(hub.operation_pnl_breakdown),
    })


async def start_operation(request: Request):
    if not hasattr(request.app.state, "engine"):
        return JSONResponse(
            {"error": "Engine not running (set START_ENGINE=true)"}, status_code=503,
        )
    engine = request.app.state.engine
    try:
        op_id = await engine.start_operation()
        return JSONResponse({"id": op_id, "status": "active"}, status_code=201)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=409)


async def stop_operation(request: Request):
    if not hasattr(request.app.state, "engine"):
        return JSONResponse(
            {"error": "Engine not running"}, status_code=503,
        )
    engine = request.app.state.engine
    try:
        result = await engine.stop_operation(close_reason="user")
        return JSONResponse(result, status_code=200)
    except RuntimeError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
```

In `app.py`, register the routes (find the `routes = [...]` list in `create_app` and add):

```python
from web.routes import (
    dashboard, sse_state, sse_logs, update_settings, get_config,
    list_operations, get_current_operation, start_operation, stop_operation,
)

# In the routes list:
Route("/operations", list_operations),
Route("/operations/current", get_current_operation),
Route("/operations/start", start_operation, methods=["POST"]),
Route("/operations/stop", stop_operation, methods=["POST"]),
```

- [ ] **Step 4: Rodar tests**

Run: `python -m pytest tests/test_web.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/routes.py app.py tests/test_web.py
git commit -m "feat(task-11): /operations REST endpoints (list, current, start, stop)"
```

---

### Task 12: Operation card no dashboard

**Files:**
- Create: `web/templates/partials/operation.html`
- Modify: `web/templates/dashboard.html`
- Modify: `web/static/app.js`

- [ ] **Step 1: Criar partial operation.html**

```html
<!-- web/templates/partials/operation.html -->
<div class="card">
    <div class="flex items-center justify-between mb-3">
        <p class="card-title">Operação</p>
        <div x-show="state.operation_state === 'none'" x-cloak>
            <span class="text-xs text-slate-400">Nenhuma operação ativa</span>
        </div>
        <div x-show="state.operation_state !== 'none'" x-cloak class="flex items-center gap-2">
            <span class="text-xs px-2 py-0.5 rounded-full"
                  :class="{
                    'bg-amber-100 text-amber-700': state.operation_state === 'starting' || state.operation_state === 'stopping',
                    'bg-emerald-100 text-emerald-700': state.operation_state === 'active',
                  }"
                  x-text="state.operation_state.toUpperCase()"></span>
            <span class="text-xs text-slate-400" x-text="op.elapsed"></span>
        </div>
    </div>

    <!-- PnL breakdown when active -->
    <div x-show="state.operation_state === 'active'" x-cloak class="space-y-1 text-sm">
        <template x-for="row in op.breakdown" :key="row.label">
            <div class="flex justify-between">
                <span class="text-slate-500" x-text="row.label"></span>
                <span class="font-mono" :class="row.value >= 0 ? 'text-emerald-600' : 'text-red-500'"
                      x-text="(row.value >= 0 ? '+$' : '-$') + Math.abs(row.value).toFixed(2)"></span>
            </div>
        </template>
        <div class="flex justify-between border-t border-slate-200 pt-2 mt-2 font-semibold">
            <span>Net PnL</span>
            <span class="font-mono" :class="op.netPnl >= 0 ? 'text-emerald-600' : 'text-red-500'"
                  x-text="(op.netPnl >= 0 ? '+$' : '-$') + Math.abs(op.netPnl).toFixed(2)"></span>
        </div>
    </div>

    <div class="flex justify-end mt-3 gap-2">
        <button x-show="state.operation_state === 'none'"
                @click="startOperation()"
                class="px-4 py-1.5 text-sm bg-emerald-600 text-white rounded-lg hover:bg-emerald-700">
            Iniciar operação
        </button>
        <button x-show="state.operation_state === 'active'" x-cloak
                @click="if (confirm('Encerrar operação? Vai cancelar grade e fechar short.')) stopOperation()"
                class="px-4 py-1.5 text-sm bg-red-500 text-white rounded-lg hover:bg-red-600">
            Encerrar operação
        </button>
    </div>
</div>
```

- [ ] **Step 2: Incluir partial no dashboard.html**

In `web/templates/dashboard.html`, add the operation card at the top of the Painel tab. Find the `<!-- Tab: Painel -->` section and insert just inside, before the `<!-- PnL destaque -->` div:

```html
<!-- Operação atual -->
{% include "partials/operation.html" %}
```

- [ ] **Step 3: Adicionar state + actions em app.js**

Open `web/static/app.js` and:

In the `state` object, add:
```javascript
current_operation_id: null,
operation_state: "none",
operation_pnl_breakdown: {},
```

Add a computed `op` property in the dashboard component:

```javascript
get op() {
    const b = this.state.operation_pnl_breakdown || {};
    const netPnl = b.net_pnl || 0;
    const breakdown = [
        { label: "LP fees recebidas", value: b.lp_fees_earned || 0 },
        { label: "Beefy perf fee", value: b.beefy_perf_fee || 0 },
        { label: "IL natural", value: b.il_natural || 0 },
        { label: "Hedge PnL", value: b.hedge_pnl || 0 },
        { label: "Funding", value: b.funding || 0 },
        { label: "Perp fees", value: b.perp_fees_paid || 0 },
        { label: "Bootstrap slippage", value: b.bootstrap_slippage || 0 },
    ];
    return {
        elapsed: this._formatElapsed(),
        breakdown: breakdown,
        netPnl: netPnl,
    };
},

_formatElapsed() {
    if (!this._opStartedAt) return "";
    const sec = Math.max(0, (Date.now() / 1000) - this._opStartedAt);
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    return h + "h " + m + "min";
},

async startOperation() {
    try {
        const resp = await fetch("/operations/start", { method: "POST" });
        if (!resp.ok) {
            const err = await resp.json();
            alert("Erro ao iniciar: " + (err.error || resp.status));
        }
    } catch (e) {
        alert("Erro: " + e);
    }
},

async stopOperation() {
    try {
        const resp = await fetch("/operations/stop", { method: "POST" });
        if (!resp.ok) {
            const err = await resp.json();
            alert("Erro ao encerrar: " + (err.error || resp.status));
        }
    } catch (e) {
        alert("Erro: " + e);
    }
},
```

Also: in `init()`, fetch the current operation once and set `_opStartedAt`:

```javascript
fetch('/operations/current')
    .then(r => r.status === 204 ? null : r.json())
    .then(data => {
        if (data) this._opStartedAt = data.started_at;
    })
    .catch(() => {});
```

Add `_opStartedAt: null,` to the component's data.

- [ ] **Step 4: Verificar tests**

Run: `python -m pytest tests/test_web.py -v`
Expected: PASS (todos)

- [ ] **Step 5: Commit**

```bash
git add web/templates/partials/operation.html web/templates/dashboard.html web/static/app.js
git commit -m "feat(task-12): operation card with start/stop actions and live PnL breakdown"
```

---

### Task 13: Aba Histórico

**Files:**
- Create: `web/templates/partials/history.html`
- Modify: `web/templates/dashboard.html`
- Modify: `web/static/app.js`

- [ ] **Step 1: Criar partial history.html**

```html
<!-- web/templates/partials/history.html -->
<div class="card">
    <p class="card-title">Histórico de operações</p>
    <div x-show="history.length === 0" class="text-sm text-slate-400 py-4 text-center">
        Nenhuma operação encerrada ainda
    </div>
    <div class="space-y-2">
        <template x-for="op in history" :key="op.id">
            <div class="border border-slate-200 rounded-lg p-3 hover:bg-slate-50">
                <div class="flex justify-between items-center">
                    <div>
                        <span class="font-medium text-slate-700" x-text="'Op #' + op.id"></span>
                        <span class="text-xs text-slate-400 ml-2"
                              x-text="new Date(op.started_at * 1000).toLocaleString()"></span>
                    </div>
                    <span class="font-mono font-semibold"
                          :class="(op.final_net_pnl || 0) >= 0 ? 'text-emerald-600' : 'text-red-500'"
                          x-text="((op.final_net_pnl || 0) >= 0 ? '+$' : '-$') + Math.abs(op.final_net_pnl || 0).toFixed(2)"></span>
                </div>
                <div class="text-xs text-slate-400 mt-1">
                    <span x-text="op.status"></span>
                    <span x-show="op.ended_at" x-text="' • ' + Math.round((op.ended_at - op.started_at) / 60) + ' min'"></span>
                </div>
            </div>
        </template>
    </div>
</div>
```

- [ ] **Step 2: Adicionar nova tab no dashboard**

Modify `web/templates/dashboard.html`. Add a new tab button in the tab-bar:

```html
<button class="tab-btn" :class="activeTab === 'historico' && 'tab-active'" @click="activeTab = 'historico'; loadHistory()">Histórico</button>
```

Add the tab section:

```html
<!-- Tab: Historico -->
<div x-show="activeTab === 'historico'" x-cloak class="space-y-5">
    {% include "partials/history.html" %}
</div>
```

- [ ] **Step 3: Adicionar history state + loader em app.js**

In the dashboard component data:
```javascript
history: [],
```

Add method:

```javascript
async loadHistory() {
    try {
        const resp = await fetch("/operations?limit=50");
        if (resp.ok) this.history = await resp.json();
    } catch (e) {
        console.error("Failed to load history:", e);
    }
},
```

- [ ] **Step 4: Verificar tests**

Run: `python -m pytest tests/test_web.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add web/templates/partials/history.html web/templates/dashboard.html web/static/app.js
git commit -m "feat(task-13): historico tab listing closed operations"
```

---

## Phase D: Cleanup legacy

### Task 14: Cleanup — remover engine/hedge.py, chains/evm.py, exchanges/hyperliquid.py

**Files:**
- Delete: `engine/hedge.py`, `tests/test_hedge.py`
- Delete: `chains/evm.py`, `tests/test_evm.py`
- Delete: `exchanges/hyperliquid.py`
- Modify: `tests/test_exchanges.py` (remove hyperliquid-specific asserts if any)
- Search-and-modify: any imports of the deleted modules

- [ ] **Step 1: Verificar imports**

Run: `grep -rn "from engine.hedge\|import engine.hedge" --include="*.py" .`
Run: `grep -rn "from chains.evm\|import chains.evm" --include="*.py" .`
Run: `grep -rn "from exchanges.hyperliquid\|import exchanges.hyperliquid" --include="*.py" .`

Each grep should reveal usages. Plan: if anywhere in production code (engine/__init__.py, app.py, etc.), remove or replace with new modules.

Expected results from past phases: `engine/__init__.py` still imports `compute_hedge_action` from `engine.hedge` (should be unused now — remove import), and possibly `chains/evm.py` is unused entirely.

- [ ] **Step 2: Remover imports não usados**

If `engine/__init__.py` has `from engine.hedge import compute_hedge_action` and `compute_hedge_action` isn't called anywhere, delete that import line.

If `app.py` imports `chains.evm` or `exchanges.hyperliquid`, replace with no-op or remove.

- [ ] **Step 3: Deletar arquivos**

```bash
rm engine/hedge.py
rm tests/test_hedge.py
rm chains/evm.py
rm tests/test_evm.py
rm exchanges/hyperliquid.py
```

- [ ] **Step 4: Verificar tests**

Run: `python -m pytest -v`
Expected: PASS — no test imports the deleted modules

If any test fails because it referenced deleted code, fix the test (delete the dependent test or update its imports).

- [ ] **Step 5: Smoke test**

Run: `python -c "from app import app; print('app loaded:', type(app).__name__)"`
Expected: `app loaded: Starlette`

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "chore(task-14): remove legacy modules (engine/hedge, chains/evm, exchanges/hyperliquid)"
```

---

## Phase E: Integration test

### Task 15: Full lifecycle integration test

**Files:**
- Create: `tests/test_integration_operation.py`

- [ ] **Step 1: Escrever teste**

```python
# tests/test_integration_operation.py
import pytest
from unittest.mock import AsyncMock, MagicMock
from state import StateHub


@pytest.mark.asyncio
async def test_full_operation_lifecycle(tmp_path):
    """End-to-end: start → fill → stop → history."""
    from db import Database
    from engine import GridMakerEngine
    from exchanges.base import Order, Fill

    db = Database(str(tmp_path / "t.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"
    settings.alert_webhook_url = ""
    settings.threshold_aggressive = 0.05
    settings.threshold_recovery = 0.02
    settings.max_open_orders = 50
    settings.pool_token0_symbol = "WETH"
    settings.pool_token1_symbol = "USDC"

    placed = []

    async def fake_place(**kw):
        placed.append(kw)
        return Order(order_id=str(kw["cloid_int"]), symbol=kw["symbol"],
                     side=kw["side"], size=kw["size"], price=kw["price"], status="open")

    exchange = MagicMock()
    exchange.name = "dydx"
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=0.001))
    exchange.get_position = AsyncMock(return_value=None)  # initially no position
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.place_long_term_order = AsyncMock(side_effect=fake_place)
    exchange.batch_place = AsyncMock(return_value=[])
    exchange.batch_cancel = AsyncMock(return_value=0)
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])

    pool_reader = MagicMock()
    pool_reader.read_price = AsyncMock(return_value=3000.0)
    beefy_reader = MagicMock()
    beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.5, amount1=1500.0, share=0.01, raw_balance=10**16,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool_reader, beefy_reader=beefy_reader,
    )

    # Phase 1: start
    op_id = await engine.start_operation()
    assert state.operation_state == "active"
    assert len(placed) == 1  # bootstrap

    # Phase 2: simulate a maker fill during active op
    fill = Fill(
        fill_id="f1", order_id="100", symbol="ETH-USD", side="sell", size=0.001,
        price=2999.0, fee=0.0003, fee_currency="USDC", liquidity="maker",
        realized_pnl=0.0, timestamp=1500.0,
    )
    await engine._on_fill(fill)
    fills = await db.get_fills()
    assert any(f["operation_id"] == op_id for f in fills)

    # Phase 3: stop
    # Need a position to close — patch get_position to return one
    exchange.get_position = AsyncMock(return_value=MagicMock(
        side="short", size=0.005, entry_price=3000.0, unrealized_pnl=0.0,
    ))
    result = await engine.stop_operation()
    assert state.operation_state == "none"
    assert "final_net_pnl" in result

    # Phase 4: history
    history = await db.get_operations(limit=10)
    assert len(history) == 1
    assert history[0]["id"] == op_id
    assert history[0]["status"] == "closed"

    # Phase 5: cannot start when none active — should succeed
    op_id_2 = await engine.start_operation()
    assert op_id_2 != op_id
    assert state.operation_state == "active"

    await db.close()
```

- [ ] **Step 2: Rodar teste**

Run: `python -m pytest tests/test_integration_operation.py -v`
Expected: PASS

- [ ] **Step 3: Rodar suite completa**

Run: `python -m pytest -v`
Expected: PASS — todos os tests da Fase 1.1 + Fase 1.2

- [ ] **Step 4: Commit**

```bash
git add tests/test_integration_operation.py
git commit -m "test(task-15): full operation lifecycle integration test"
```

---

## Self-review

### Spec coverage

| Spec section | Task |
|---|---|
| Operation state machine | T3 |
| operations DB table + FKs | T1 |
| StateHub fields | T2 |
| compute_operation_pnl breakdown | T4 |
| Engine respects operation_state | T5 |
| start_operation (baseline + bootstrap) | T6 |
| stop_operation (cancel grid + close short) | T7 |
| Fill attribution to operation | T8 |
| Live PnL breakdown in hub | T9 |
| Recovery after restart | T10 |
| REST endpoints | T11 |
| Operation card UI | T12 |
| History tab | T13 |
| Cleanup legacy | T14 |
| Integration test | T15 |

Coverage complete.

### Type / signature consistency

- `OperationState` enum used consistently as string values matching DB `status` column.
- `Operation.from_db_row()` defined T3, used T7/T9.
- `compute_operation_pnl` signature stable across T4/T7/T9.
- `current_operation_id` and `operation_state` field names match across state.py, engine, routes, app.js.
- `add_to_operation_accumulator` field whitelist (perp_fees_paid, funding_paid, lp_fees_earned, bootstrap_slippage) consistent.

### No placeholders

All tasks contain executable code, exact paths, exact commands. The "verify ABI" risk in the spec is handled in the spec's risk section (LP fees fallback strategy) — tasks themselves don't reference unresolved details. The Beefy `Harvest` listener is explicitly deferred from this plan (LP fees come from accumulator, populated by future polling — for now lp_fees_earned stays 0 unless Task 11 adds the polling, which it doesn't).

**Note on LP fees:** Plan as written does NOT implement the Beefy Harvest event listener. This is a known gap — `lp_fees_earned` stays 0 in this Phase 1.2. Document as follow-up. Per the spec's fallback note, an alternate path would be inferring LP fees from `pool_value − baseline − IL − hedge_pnl`, but this is implicit and may be added later.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-27-operation-lifecycle.md`.

Two execution options:

**1. Subagent-Driven (recommended)** — Eu disparo um subagent fresh por task, revejo entre tasks, iteração rápida. 15 tasks total.

**2. Inline Execution** — Executo tasks em batches na sessão atual com checkpoints.

Which approach?
