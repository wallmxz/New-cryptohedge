# Observability + Cleanup Implementation Plan (Phase 1.3)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Adicionar Prometheus metrics, structured JSON logs, latency tracing por step e dashboard health card; remover campos legacy de `Settings`.

**Architecture:** Modulo `engine/metrics.py` com Prometheus registry global. Modulo `web/logging_config.py` substitui `logging.basicConfig` com handler condicional (JSON ou plain). Engine instrumenta `_iterate` com timing + counters. Endpoint `/metrics` no Starlette retorna texto Prometheus. Hub publica `last_iter_timings` que dashboard renderiza.

**Tech Stack:** Python 3.14, prometheus-client, python-json-logger, Starlette, Alpine.js (existing).

**Spec:** [`docs/superpowers/specs/2026-04-28-observability-design.md`](../specs/2026-04-28-observability-design.md)

---

## File Structure

### New
- `engine/metrics.py` — Prometheus registry + factory helpers (counter/gauge/histogram)
- `web/logging_config.py` — `setup_logging()` baseado em LOG_FORMAT env var
- `web/templates/partials/health.html` — card de saúde do loop
- `tests/test_metrics.py`
- `tests/test_logging_config.py`

### Modified
- `requirements.txt` — adiciona `prometheus-client`, `python-json-logger`
- `app.py` — `setup_logging()` no startup, route `/metrics`, exclude `/metrics` do auth middleware
- `engine/__init__.py` — instrumenta `_iterate`, `_on_fill`, `_aggressive_correct`, `start_operation`, `stop_operation`, `_check_margin_and_alert`
- `state.py` — campo `last_iter_timings: dict`
- `web/routes.py` — handler `/metrics`; remove fallback `hyperliquid_symbol`
- `web/templates/dashboard.html` — inclui partial health.html
- `web/static/app.js` — state field + healthSteps getter
- `config.py` — remove `hyperliquid_*` fields
- `tests/test_config.py` — remove asserts hyperliquid
- `.env.example` — remove bloco legacy

### Deleted
- (none)

---

## Phase A: Foundation

### Task 1: Adicionar deps + setup_logging

**Files:**
- Modify: `requirements.txt`
- Create: `web/logging_config.py`
- Test: `tests/test_logging_config.py`

- [ ] **Step 1: Atualizar requirements.txt**

Add these two lines (after existing deps, before pytest):

```
prometheus-client>=0.21,<1.0
python-json-logger>=2.0,<3.0
```

Run: `pip install -r requirements.txt`
Expected: install OK; verify with `python -c "import prometheus_client, pythonjsonlogger; print('ok')"`

- [ ] **Step 2: Escrever teste**

Create `tests/test_logging_config.py`:

```python
import logging
import os
from web.logging_config import setup_logging


def _reset_root():
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)


def test_setup_logging_plain_default(monkeypatch):
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    _reset_root()
    setup_logging()
    root = logging.getLogger()
    assert len(root.handlers) == 1
    formatter = root.handlers[0].formatter
    assert isinstance(formatter, logging.Formatter)
    assert "JsonFormatter" not in type(formatter).__name__


def test_setup_logging_json_when_env_set(monkeypatch):
    monkeypatch.setenv("LOG_FORMAT", "json")
    _reset_root()
    setup_logging()
    root = logging.getLogger()
    assert len(root.handlers) == 1
    formatter = root.handlers[0].formatter
    assert "JsonFormatter" in type(formatter).__name__


def test_setup_logging_replaces_existing_handlers(monkeypatch):
    monkeypatch.delenv("LOG_FORMAT", raising=False)
    _reset_root()
    root = logging.getLogger()
    root.addHandler(logging.StreamHandler())
    root.addHandler(logging.StreamHandler())
    assert len(root.handlers) == 2
    setup_logging()
    assert len(root.handlers) == 1
```

- [ ] **Step 3: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_logging_config.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'web.logging_config'`

- [ ] **Step 4: Implementar web/logging_config.py**

```python
# web/logging_config.py
from __future__ import annotations
import logging
import os
from pythonjsonlogger import jsonlogger


def setup_logging() -> None:
    """Configure root logger based on LOG_FORMAT env var.

    LOG_FORMAT=json -> JSON output (one object per line, suitable for Fly.io / log aggregators)
    LOG_FORMAT=plain (default) -> human-readable single-line text
    """
    fmt = os.environ.get("LOG_FORMAT", "plain").lower()
    handler = logging.StreamHandler()

    if fmt == "json":
        formatter = jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={
                "levelname": "level",
                "name": "logger",
                "asctime": "timestamp",
            },
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        )

    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
```

- [ ] **Step 5: Rodar tests**

Run: `python -m pytest tests/test_logging_config.py -v`
Expected: PASS (3 tests)

- [ ] **Step 6: Commit**

```bash
git add requirements.txt web/logging_config.py tests/test_logging_config.py
git commit -m "feat(task-1): add prometheus-client + python-json-logger; setup_logging helper"
```

---

### Task 2: engine/metrics.py — Prometheus registry + helpers

**Files:**
- Create: `engine/metrics.py`
- Test: `tests/test_metrics.py`

- [ ] **Step 1: Escrever testes**

```python
# tests/test_metrics.py
from engine import metrics


def test_metrics_module_exposes_expected_symbols():
    """All counters/gauges/histograms documented in the spec are present."""
    expected = [
        "fills_total", "alerts_total", "operations_total", "aggressive_corrections_total",
        "margin_ratio", "pool_value_usd", "hedge_position_size", "grid_orders_open",
        "operation_state", "out_of_range",
        "loop_duration",
    ]
    for name in expected:
        assert hasattr(metrics, name), f"missing metric: {name}"


def test_fills_total_is_counter_with_labels():
    from prometheus_client import Counter
    assert isinstance(metrics.fills_total, Counter)
    # Counter with labels can't be observed without specifying them
    metrics.fills_total.labels(liquidity="maker", side="sell").inc()
    metrics.fills_total.labels(liquidity="taker", side="buy").inc()


def test_loop_duration_histogram_buckets():
    from prometheus_client import Histogram
    assert isinstance(metrics.loop_duration, Histogram)
    metrics.loop_duration.labels(step="chain_read").observe(0.123)
    metrics.loop_duration.labels(step="total").observe(0.5)


def test_render_metrics_returns_text():
    """render_metrics() returns Prometheus exposition text."""
    metrics.margin_ratio.set(1.25)
    body = metrics.render_metrics()
    assert b"bot_margin_ratio" in body
    assert b"1.25" in body


def test_render_content_type():
    """render_content_type() returns the Prometheus mimetype."""
    ct = metrics.render_content_type()
    assert "text/plain" in ct
    assert "version=0.0.4" in ct
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_metrics.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'engine.metrics'`

- [ ] **Step 3: Implementar engine/metrics.py**

```python
# engine/metrics.py
"""Prometheus metrics registry for AutoMoney bot.

Uses prometheus_client's default global registry. Helpers `render_metrics()` and
`render_content_type()` produce the response body and MIME type for the
/metrics HTTP endpoint.
"""
from __future__ import annotations
from prometheus_client import Counter, Gauge, Histogram, generate_latest, CONTENT_TYPE_LATEST


# Counters --------------------------------------------------------------

fills_total = Counter(
    "bot_fills_total",
    "Total fills observed by the engine.",
    ["liquidity", "side"],
)

alerts_total = Counter(
    "bot_alerts_total",
    "Webhook alerts fired by the margin monitor.",
    ["level"],
)

operations_total = Counter(
    "bot_operations_total",
    "Operation lifecycle events.",
    ["status"],  # started, closed, failed
)

aggressive_corrections_total = Counter(
    "bot_aggressive_corrections_total",
    "Number of aggressive (taker) corrections fired by the engine.",
)

# Gauges ----------------------------------------------------------------

margin_ratio = Gauge(
    "bot_margin_ratio",
    "Current margin ratio (collateral / required). 999 means no position.",
)

pool_value_usd = Gauge(
    "bot_pool_value_usd",
    "Current LP pool value in USD.",
)

hedge_position_size = Gauge(
    "bot_hedge_position_size",
    "Current short position size in base units (e.g., WETH).",
)

grid_orders_open = Gauge(
    "bot_grid_orders_open",
    "Currently-open grid orders on the exchange.",
)

operation_state = Gauge(
    "bot_operation_state",
    "1 if an operation is active, 0 otherwise.",
)

out_of_range = Gauge(
    "bot_out_of_range",
    "1 if pool price is outside its range, 0 otherwise.",
)

# Histograms ------------------------------------------------------------

loop_duration = Histogram(
    "bot_loop_duration_seconds",
    "Duration of the main engine loop, broken down by step.",
    ["step"],
    buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0],
)


# Helpers ---------------------------------------------------------------

def render_metrics() -> bytes:
    """Return the current Prometheus exposition text as bytes."""
    return generate_latest()


def render_content_type() -> str:
    """Return the MIME type for the Prometheus exposition format."""
    return CONTENT_TYPE_LATEST
```

- [ ] **Step 4: Rodar tests**

Run: `python -m pytest tests/test_metrics.py -v`
Expected: PASS (5 tests)

- [ ] **Step 5: Commit**

```bash
git add engine/metrics.py tests/test_metrics.py
git commit -m "feat(task-2): Prometheus metrics registry with counters, gauges, histograms"
```

---

### Task 3: state.py — last_iter_timings field

**Files:**
- Modify: `state.py`
- Modify: `tests/test_state.py`

- [ ] **Step 1: Escrever teste**

Add to `tests/test_state.py`:

```python
def test_statehub_last_iter_timings_default():
    from state import StateHub
    s = StateHub()
    assert s.last_iter_timings == {}
    assert isinstance(s.last_iter_timings, dict)
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_state.py::test_statehub_last_iter_timings_default -v`
Expected: FAIL — AttributeError

- [ ] **Step 3: Adicionar campo em state.py**

In the StateHub dataclass, add after the operation lifecycle block (before the `to_dict` method):

```python
# Observability — populated by engine each iteration
last_iter_timings: dict = field(default_factory=dict)  # {"chain_read": 250.5, "total": 442.1, ...} ms
```

- [ ] **Step 4: Rodar tests**

Run: `python -m pytest tests/test_state.py -v`
Expected: PASS (all)

- [ ] **Step 5: Commit**

```bash
git add state.py tests/test_state.py
git commit -m "feat(task-3): last_iter_timings field on StateHub for latency tracing"
```

---

## Phase B: Engine instrumentation

### Task 4: Instrumentar _iterate com timings

**Files:**
- Modify: `engine/__init__.py`
- Modify: `tests/test_engine_grid.py`

- [ ] **Step 1: Escrever teste**

Add to `tests/test_engine_grid.py`:

```python
@pytest.mark.asyncio
async def test_engine_populates_last_iter_timings(tmp_path):
    """After _iterate runs, hub.last_iter_timings has the expected step keys."""
    from db import Database
    from engine import GridMakerEngine

    db = Database(str(tmp_path / "tobs.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)
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
        side="short", size=0.00476, entry_price=3000.0, unrealized_pnl=0.0,
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

    timings = state.last_iter_timings
    assert "chain_read" in timings
    assert "grid_compute" in timings
    assert "grid_diff_apply" in timings
    assert "total" in timings
    # All values should be non-negative ms
    for k, v in timings.items():
        assert v >= 0, f"{k} should be >= 0, got {v}"

    await db.close()
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_engine_grid.py::test_engine_populates_last_iter_timings -v`
Expected: FAIL — `last_iter_timings` is empty

- [ ] **Step 3: Instrumentar `_iterate`**

In `engine/__init__.py`, add import at top:

```python
from engine import metrics
```

Refactor `_iterate` to time each major step. The exact change is to replace the body of `_iterate` so each step is wrapped with `time.monotonic()` deltas accumulated into a local `timings` dict, then assigned to `self._hub.last_iter_timings` at the end.

Example pattern around the existing logic (apply throughout the method):

```python
async def _iterate(self):
    """One cycle of the main loop."""
    iter_start = time.monotonic()
    self._iter_count += 1
    timings: dict[str, float] = {}
    await self._maybe_reconcile()

    # 1. Chain read
    t = time.monotonic()
    beefy_pos = await self._beefy_reader.read_position()
    p_now = await self._pool_reader.read_price()
    timings["chain_read"] = (time.monotonic() - t) * 1000
    metrics.loop_duration.labels(step="chain_read").observe(timings["chain_read"] / 1000)

    p_a = tick_to_price(beefy_pos.tick_lower, self._decimals0, self._decimals1)
    p_b = tick_to_price(beefy_pos.tick_upper, self._decimals0, self._decimals1)
    my_amount0 = beefy_pos.amount0 * beefy_pos.share
    my_amount1 = beefy_pos.amount1 * beefy_pos.share
    my_value = my_amount0 * p_now + my_amount1
    if my_value <= 0:
        timings["total"] = (time.monotonic() - iter_start) * 1000
        self._hub.last_iter_timings = timings
        self._hub.last_update = time.time()
        return

    # ... (existing range/out-of-range logic) ...

    # When in active path, after reaching the grid_compute section:
    t = time.monotonic()
    meta = await self._exchange.get_market_meta(self._settings.dydx_symbol)
    target = compute_target_grid(
        L=L_user, p_a=p_a, p_b=p_b, p_now=p_now,
        hedge_ratio=self._hub.hedge_ratio,
        min_notional_usd=meta.min_notional * p_now,
        max_orders=self._settings.max_open_orders,
    )
    timings["grid_compute"] = (time.monotonic() - t) * 1000
    metrics.loop_duration.labels(step="grid_compute").observe(timings["grid_compute"] / 1000)

    # ... reconcile current short, exposure check ...

    # Around the diff/place/cancel block:
    t = time.monotonic()
    active = await self._db.get_active_grid_orders()
    # ... existing diff + batch_cancel + batch_place code ...
    timings["grid_diff_apply"] = (time.monotonic() - t) * 1000
    metrics.loop_duration.labels(step="grid_diff_apply").observe(timings["grid_diff_apply"] / 1000)

    # ... margin check, PnL breakdown ...

    timings["total"] = (time.monotonic() - iter_start) * 1000
    metrics.loop_duration.labels(step="total").observe(timings["total"] / 1000)
    self._hub.last_iter_timings = timings
    self._hub.last_update = time.time()
```

**Important:** every code path that returns from `_iterate` must populate `timings["total"]` and assign to `self._hub.last_iter_timings`. To minimize boilerplate, refactor with a try/finally:

```python
async def _iterate(self):
    iter_start = time.monotonic()
    self._iter_count += 1
    timings: dict[str, float] = {}
    try:
        await self._maybe_reconcile()
        # ... existing body unchanged except for timing helpers below ...
    finally:
        timings["total"] = (time.monotonic() - iter_start) * 1000
        metrics.loop_duration.labels(step="total").observe(timings["total"] / 1000)
        self._hub.last_iter_timings = timings
        self._hub.last_update = time.time()
```

Within the try block, set `timings["chain_read"]`, `timings["grid_compute"]`, `timings["grid_diff_apply"]` at the appropriate points (and observe to histogram). Steps not reached (e.g., `grid_diff_apply` on out-of-range path) simply don't appear in the dict.

The existing `last_update = time.time()` calls in early-return paths can be removed (the finally handles it).

- [ ] **Step 4: Rodar tests**

Run: `python -m pytest tests/test_engine_grid.py -v`
Expected: PASS — all (existing + new)

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_engine_grid.py
git commit -m "feat(task-4): instrument _iterate with per-step timings + histogram"
```

---

### Task 5: Instrumentar gauges (margin/pool/hedge/grid)

**Files:**
- Modify: `engine/__init__.py`
- Modify: `tests/test_engine_grid.py`

- [ ] **Step 1: Escrever teste**

```python
@pytest.mark.asyncio
async def test_engine_updates_gauge_metrics(tmp_path):
    """After _iterate, gauges in metrics module reflect current state."""
    from db import Database
    from engine import GridMakerEngine, metrics
    from prometheus_client import REGISTRY

    db = Database(str(tmp_path / "tobs2.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)
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
        side="short", size=0.00476, entry_price=3000.0, unrealized_pnl=0.0,
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

    # pool_value_usd should be > 0 after iterate
    assert metrics.pool_value_usd._value.get() > 0
    # hedge_position_size should equal current short
    assert abs(metrics.hedge_position_size._value.get() - 0.00476) < 1e-9
    # operation_state == 1 since active
    assert metrics.operation_state._value.get() == 1.0
    # out_of_range == 0 since in range
    assert metrics.out_of_range._value.get() == 0.0

    await db.close()
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_engine_grid.py::test_engine_updates_gauge_metrics -v`
Expected: FAIL — gauges not yet updated

- [ ] **Step 3: Adicionar gauge updates em `_iterate`**

Inside the try-block of `_iterate`, after computing `my_value` and at appropriate points, set the gauges. Place these after the relevant data is computed:

```python
# After my_value computed:
metrics.pool_value_usd.set(my_value)

# After out_of_range branches set the flag:
metrics.out_of_range.set(1 if self._hub.out_of_range else 0)

# After fetching position (in the in-range / active path):
if pos:
    metrics.hedge_position_size.set(pos.size)
else:
    metrics.hedge_position_size.set(0.0)

# After active grid orders fetched (count):
metrics.grid_orders_open.set(len(active))

# Operation state (always at end of iterate, in finally):
metrics.operation_state.set(1.0 if self._hub.operation_state == "active" else 0.0)
```

For `margin_ratio`, this is already updated inside `_check_margin_and_alert` — modify that method to also call `metrics.margin_ratio.set(ratio)` after computing it.

- [ ] **Step 4: Rodar tests**

Run: `python -m pytest tests/test_engine_grid.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_engine_grid.py
git commit -m "feat(task-5): update gauges (margin, pool, hedge, grid, operation_state, out_of_range)"
```

---

### Task 6: Instrumentar counters (fills, alerts, operations, aggressive)

**Files:**
- Modify: `engine/__init__.py`
- Modify: `tests/test_engine_grid.py`

- [ ] **Step 1: Escrever teste**

```python
@pytest.mark.asyncio
async def test_engine_increments_counters(tmp_path):
    """Fills, alerts, operations counters increment on relevant events."""
    from db import Database
    from engine import GridMakerEngine, metrics
    from exchanges.base import Fill

    db = Database(str(tmp_path / "tcounter.db"))
    await db.initialize()
    state = StateHub(hedge_ratio=1.0)

    settings = MagicMock()
    settings.dydx_symbol = "ETH-USD"

    exchange = MagicMock()
    exchange.name = "dydx"

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=MagicMock(), beefy_reader=MagicMock(),
    )

    # Snapshot counter before
    before = metrics.fills_total.labels(liquidity="maker", side="sell")._value.get()

    fill = Fill(
        fill_id="f1", order_id="100", symbol="ETH-USD", side="sell", size=0.001,
        price=2999.0, fee=0.0003, fee_currency="USDC", liquidity="maker",
        realized_pnl=0.0, timestamp=1500.0,
    )
    await engine._on_fill(fill)

    after = metrics.fills_total.labels(liquidity="maker", side="sell")._value.get()
    assert after == before + 1

    await db.close()
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_engine_grid.py::test_engine_increments_counters -v`
Expected: FAIL — counter does not increment

- [ ] **Step 3: Adicionar counter increments**

In `engine/__init__.py`:

In `_on_fill`, after the existing aggregate updates, add:
```python
metrics.fills_total.labels(liquidity=fill.liquidity, side=fill.side).inc()
```

In `_aggressive_correct`, at the start (after computing delta but before placing), add:
```python
metrics.aggressive_corrections_total.inc()
```

In `_check_margin_and_alert`, after `if level != "healthy" and level != self._last_alert_level:` block (i.e., right when an alert fires), add:
```python
metrics.alerts_total.labels(level=level).inc()
```

In `start_operation`, after `update_operation_status(op_id, ACTIVE)` add:
```python
metrics.operations_total.labels(status="started").inc()
```

In `start_operation`, in the bootstrap-failure path (just after `update_operation_status(op_id, FAILED)`), add:
```python
metrics.operations_total.labels(status="failed").inc()
```

In `stop_operation`, after `db.close_operation(...)`:
```python
metrics.operations_total.labels(status="closed").inc()
```

- [ ] **Step 4: Rodar tests**

Run: `python -m pytest tests/test_engine_grid.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_engine_grid.py
git commit -m "feat(task-6): increment counters on fills, alerts, ops, aggressive corrections"
```

---

## Phase C: HTTP integration

### Task 7: /metrics endpoint + auth bypass

**Files:**
- Modify: `web/routes.py`
- Modify: `web/auth.py`
- Modify: `app.py`
- Modify: `tests/test_web.py`

- [ ] **Step 1: Escrever teste**

Add to `tests/test_web.py`:

```python
def test_metrics_endpoint_no_auth(app):
    """GET /metrics returns 200 with Prometheus content-type, NO auth required."""
    from starlette.testclient import TestClient
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    assert "version=0.0.4" in resp.headers["content-type"]
    # The body should contain at least one of the registered metric names
    body = resp.text
    assert "bot_loop_duration_seconds" in body or "bot_margin_ratio" in body
```

- [ ] **Step 2: Rodar pra confirmar falha**

Run: `python -m pytest tests/test_web.py::test_metrics_endpoint_no_auth -v`
Expected: FAIL — 404 (no route)

- [ ] **Step 3: Adicionar handler em web/routes.py**

Add to `web/routes.py`:

```python
from engine import metrics as engine_metrics


async def metrics(request: Request):
    body = engine_metrics.render_metrics()
    return Response(body, media_type=engine_metrics.render_content_type())
```

(Note: existing `Response` import is already present from Task 11 of Phase 1.2.)

- [ ] **Step 4: Registrar rota em app.py + bypass auth**

In `app.py`, update the `web.routes` import to include `metrics`:

```python
from web.routes import (
    dashboard, sse_state, sse_logs, update_settings, get_config,
    list_operations, get_current_operation, start_operation, stop_operation,
    metrics,
)
```

Add the route to the routes list:

```python
Route("/metrics", metrics),
```

Add `/metrics` to the BasicAuthMiddleware exclude list:

```python
app.add_middleware(
    BasicAuthMiddleware,
    username=settings.auth_user,
    password=settings.auth_pass,
    exclude=["/health", "/metrics"],
)
```

- [ ] **Step 5: Rodar tests**

Run: `python -m pytest tests/test_web.py -v`
Expected: PASS — all 5 (4 existing + new)

- [ ] **Step 6: Commit**

```bash
git add web/routes.py app.py tests/test_web.py
git commit -m "feat(task-7): /metrics endpoint with Prometheus content-type, auth bypassed"
```

---

### Task 8: Setup logging na inicialização do app

**Files:**
- Modify: `app.py`

- [ ] **Step 1: Modificar app.py**

In `app.py`, replace the existing `logging.basicConfig(...)` call near the top with a call to `setup_logging()`. Locate the line:

```python
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
```

Replace with:

```python
from web.logging_config import setup_logging
setup_logging()
```

- [ ] **Step 2: Smoke test**

Run: `python -c "from app import app; print('ok')"`
Expected: prints `ok` (with a single log line possibly emitted by import-time setup)

Run: `LOG_FORMAT=json python -c "import logging; from app import app; logging.getLogger('test').info('hello', extra={'foo': 'bar'})"`
Expected: prints a single JSON line containing `"foo": "bar"` (along with level, logger, timestamp, message).

- [ ] **Step 3: Rodar tests**

Run: `python -m pytest tests/test_web.py tests/test_logging_config.py -v`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat(task-8): setup_logging() on app startup; LOG_FORMAT env var controls output"
```

---

## Phase D: UI

### Task 9: Health card no dashboard

**Files:**
- Create: `web/templates/partials/health.html`
- Modify: `web/templates/dashboard.html`
- Modify: `web/static/app.js`

- [ ] **Step 1: Criar partial health.html**

```html
<!-- web/templates/partials/health.html -->
<div class="card">
    <p class="card-title">Saúde do loop</p>
    <div x-show="healthSteps.length === 0" x-cloak class="text-sm text-slate-400 py-2">
        Sem dados ainda — aguardando primeira iteração do engine
    </div>
    <div class="grid grid-cols-2 md:grid-cols-4 gap-3" x-show="healthSteps.length > 0" x-cloak>
        <template x-for="step in healthSteps" :key="step.name">
            <div>
                <p class="text-xs text-slate-400 mb-1" x-text="step.label"></p>
                <p class="text-sm font-mono"
                   :class="step.ms > 1000 ? 'text-red-500' : step.ms > 500 ? 'text-amber-600' : 'text-slate-700'"
                   x-text="step.ms.toFixed(0) + 'ms'"></p>
            </div>
        </template>
    </div>
</div>
```

- [ ] **Step 2: Adicionar campo + getter em app.js**

In `web/static/app.js` `state` object, add:

```javascript
last_iter_timings: {},
```

Add a getter to the dashboard component (alongside `op`, `pnl`, etc.):

```javascript
get healthSteps() {
    const t = this.state.last_iter_timings || {};
    const order = [
        ["chain_read", "Read chain"],
        ["margin_check", "Margin check"],
        ["grid_compute", "Compute grid"],
        ["grid_diff_apply", "Place/cancel"],
        ["pnl_breakdown", "PnL breakdown"],
        ["total", "Total"],
    ];
    const out = [];
    for (const [name, label] of order) {
        if (name in t) out.push({ name, label, ms: t[name] });
    }
    return out;
},
```

- [ ] **Step 3: Incluir partial no dashboard**

In `web/templates/dashboard.html`, in the Painel tab, add the health card AFTER the "Status da grade" card and BEFORE the chart include. Locate this comment:

```html
        <!-- Chart -->
        {% include "partials/chart.html" %}
```

Insert before it:

```html
        <!-- Saúde do loop -->
        {% include "partials/health.html" %}
```

- [ ] **Step 4: Verificar tests**

Run: `python -m pytest tests/test_web.py -v`
Expected: PASS (5 tests still)

- [ ] **Step 5: Commit**

```bash
git add web/templates/partials/health.html web/templates/dashboard.html web/static/app.js
git commit -m "feat(task-9): dashboard health card with per-step latency"
```

---

## Phase E: Cleanup legacy

### Task 10: Remover hyperliquid_* fields

**Files:**
- Modify: `config.py`
- Modify: `web/routes.py`
- Modify: `tests/test_config.py`
- Modify: `.env.example`

- [ ] **Step 1: Atualizar tests/test_config.py**

Open `tests/test_config.py`. Search for `hyperliquid` (case-insensitive) and remove any assertions that mention `hyperliquid_api_key`, `hyperliquid_api_secret`, or `hyperliquid_symbol`. Specifically:

- In `test_settings_loads_from_env`: remove `monkeypatch.setenv("HYPERLIQUID_*", ...)` lines and any assertions on `s.hyperliquid_*`.
- In `test_settings_defaults`: remove similar references.

Resulting test should focus on dydx_*, pool_*, hedge_ratio, etc.

- [ ] **Step 2: Modificar config.py**

In `config.py`, remove these three lines from the `Settings` dataclass field declarations:

```python
hyperliquid_api_key: str
hyperliquid_api_secret: str
hyperliquid_symbol: str
```

And remove the corresponding three lines in `from_env()`:

```python
hyperliquid_api_key=os.environ.get("HYPERLIQUID_API_KEY", ""),
hyperliquid_api_secret=os.environ.get("HYPERLIQUID_API_SECRET", ""),
hyperliquid_symbol=os.environ.get("HYPERLIQUID_SYMBOL", "ARB"),
```

- [ ] **Step 3: Modificar web/routes.py**

In `web/routes.py::get_config`, replace:

```python
"symbol": settings.hyperliquid_symbol if settings.active_exchange == "hyperliquid" else settings.dydx_symbol,
```

with:

```python
"symbol": settings.dydx_symbol,
```

- [ ] **Step 4: Atualizar .env.example**

Open `.env.example` and remove the three lines (commented or not):

```
# HYPERLIQUID_API_KEY=
# HYPERLIQUID_API_SECRET=
# HYPERLIQUID_SYMBOL=ARB
```

Also remove the section header comment if it now leaves an empty section. If the file had a "Legacy / alternative exchange" block, remove it entirely.

- [ ] **Step 5: Rodar tests**

Run: `python -m pytest tests/test_config.py tests/test_web.py -v`
Expected: PASS

Run: `python -m pytest -v` (full suite, in batches if Windows hangs)
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add config.py web/routes.py tests/test_config.py .env.example
git commit -m "chore(task-10): remove legacy hyperliquid_* fields from Settings"
```

---

## Phase F: Final integration

### Task 11: Smoke test + tag

**Files:** (verification only)

- [ ] **Step 1: Verificar /metrics retorna conteúdo significativo**

```bash
START_ENGINE=false python -m uvicorn app:app --host 127.0.0.1 --port 8765 &
sleep 2
curl -s http://127.0.0.1:8765/metrics | head -30
kill %1 2>/dev/null || true
```

Expected: text Prometheus exposition with metric definitions for `bot_loop_duration_seconds`, `bot_margin_ratio`, etc.

- [ ] **Step 2: Verificar JSON logging**

```bash
LOG_FORMAT=json python -c "import logging; from app import app; logging.getLogger('eng').info('test event', extra={'op_id': 5, 'iter': 12})"
```

Expected: a JSON line with `"message": "test event", "op_id": 5, "iter": 12, "level": "INFO", "logger": "eng"`.

- [ ] **Step 3: Rodar suite completa em batches**

```bash
python -m pytest tests/test_curve.py tests/test_grid.py tests/test_db.py tests/test_state.py tests/test_config.py tests/test_pnl.py tests/test_orderbook.py tests/test_alerts.py tests/test_margin.py tests/test_operation.py tests/test_metrics.py tests/test_logging_config.py -v
```

Expected: ~85 PASS

```bash
python -m pytest tests/test_uniswap.py tests/test_beefy.py tests/test_dydx.py tests/test_reconciler.py tests/test_engine_grid.py tests/test_web.py tests/test_exchanges.py tests/test_integration_grid.py tests/test_integration_operation.py -v
```

Expected: ~40 PASS

Combined: ~125 tests (111 prior + ~14 new).

- [ ] **Step 4: Tag e log**

```bash
git tag fase-1.3-completa
git log --oneline | head -20
```

- [ ] **Step 5: Atualizar CLAUDE.md**

Locate the "Concluído" section in `CLAUDE.md` and add:

```markdown
- ✅ **Phase 1.3 — Observability + Cleanup** (tag `fase-1.3-completa`, master)
  - 11 tasks, ~125 testes
  - Prometheus metrics em `/metrics` (counters, gauges, histograms)
  - Logs JSON estruturados via `LOG_FORMAT=json`
  - Latency tracing por step exposto em `hub.last_iter_timings` + dashboard
  - Cleanup: removidos `hyperliquid_api_key/secret/symbol` do `Settings`
  - Spec: `docs/superpowers/specs/2026-04-28-observability-design.md`
  - Plan: `docs/superpowers/plans/2026-04-28-observability.md`
```

Also update the "Não iniciado" list to remove "Phase 1.3" and emphasize Phase 1.4 (backtesting), Pré-produção (testnet rehearsal).

- [ ] **Step 6: Commit final**

```bash
git add CLAUDE.md
git commit -m "docs(task-11): mark Phase 1.3 complete in CLAUDE.md"
```

---

## Self-Review

### Spec coverage

| Spec section | Task |
|---|---|
| prometheus-client + python-json-logger deps | 1 |
| `web/logging_config.py::setup_logging` | 1 |
| `engine/metrics.py` registry + helpers | 2 |
| StateHub `last_iter_timings` | 3 |
| Engine `_iterate` instrumentation (timings) | 4 |
| Gauge updates (margin, pool, hedge, grid, op_state, out_of_range) | 5 |
| Counter increments (fills, alerts, ops, aggressive) | 6 |
| `/metrics` endpoint + auth bypass | 7 |
| `setup_logging()` on app startup | 8 |
| Health card on dashboard | 9 |
| Cleanup `hyperliquid_*` fields | 10 |
| Smoke test + tag | 11 |

Coverage complete.

### Placeholder scan

No "TBD"/"TODO". Each task has executable code, exact file paths, and exact commands. Risk note about Prometheus global registry resolved by using `prometheus_client.generate_latest()` (default registry, simplest path).

### Type / signature consistency

- `metrics.fills_total` — `Counter` with labels `["liquidity", "side"]` — used consistently across T2 spec and T6 invocation.
- `metrics.loop_duration` — `Histogram` with label `["step"]` — consistent T2/T4.
- `metrics.render_metrics() -> bytes` and `render_content_type() -> str` — used T2/T7.
- `setup_logging() -> None` — defined T1, called T8.
- `hub.last_iter_timings: dict` — added T3, populated T4, read T9.
- Step name keys (`chain_read`, `grid_compute`, `grid_diff_apply`, `total`) — consistent across T4 instrumentation, T5 test, T9 UI getter.

All consistent.

---

## Execution Handoff

Plan complete and saved to `docs/superpowers/plans/2026-04-28-observability.md`.

Two execution options:

**1. Subagent-Driven (recommended)** — Disparo subagent fresco por task, revejo entre tasks. 11 tasks total. Mesma cadência das fases anteriores.

**2. Inline Execution** — Executo na sessão atual com checkpoints.

Qual?
