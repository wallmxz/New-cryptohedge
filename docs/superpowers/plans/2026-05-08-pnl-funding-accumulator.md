# PnL Funding Accumulator Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Populate `funding_paid_token0/1` for active operations on Lighter, so the panel's `Funding` row reflects real funding payments instead of `+$0.00`.

**Architecture:** Background poller in `LighterAdapter` calls `AccountApi.position_funding()` every 60 s, emits each entry to a callback the engine registers at init. Engine resolves market_id → leg via cached `_token0_mid`/`_token1_mid`, dedupes by `funding_id` per operation, filters by `op.started_at`, and writes to `db.add_to_operation_accumulator` preserving the existing pnl.py sign convention (positive in DB = user paid).

**Tech Stack:** Python 3.13 asyncio, lighter-sdk 1.0.9 (`AccountApi.position_funding`), aiosqlite (db.add_to_operation_accumulator), pytest-asyncio.

---

## File Structure

| File | Responsibility |
|---|---|
| `exchanges/base.py` | Add default-no-op `subscribe_funding` to `ExchangeAdapter`. |
| `exchanges/lighter.py` | `_fetch_position_funding` HTTP wrapper, `_funding_callback` slot, `subscribe_funding`, `_funding_poller_loop` background task wired in `connect`/`disconnect`. |
| `engine/__init__.py` | Resolve `_token0_mid`/`_token1_mid` at startup, register callback, implement `_on_funding_payment` handler with sign + dedup + op filtering. |
| `tests/test_lighter_adapter.py` | Unit tests for adapter pieces (already has `_install_lighter_stub`, `_make_adapter`, `_meta` fixtures). |
| `tests/test_engine_funding.py` | NEW — engine-side handler tests + integration. |

---

### Task 1: ExchangeAdapter base — default no-op `subscribe_funding`

**Files:**
- Modify: `exchanges/base.py:49-69`
- Test: `tests/test_lighter_adapter.py` (one inline test using existing `_make_adapter`)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lighter_adapter.py`:

```python
def test_subscribe_funding_is_callable_on_base_adapter():
    """Default no-op satisfies the ABC and accepts a coroutine callback."""
    _install_lighter_stub()
    a = _make_adapter()
    called = []
    async def cb(entry):
        called.append(entry)
    # Should not raise even before connect/poller is wired.
    a.subscribe_funding(cb)
    assert callable(getattr(a, "subscribe_funding"))
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_lighter_adapter.py::test_subscribe_funding_is_callable_on_base_adapter -v`
Expected: FAIL with `AttributeError: 'LighterAdapter' object has no attribute 'subscribe_funding'`

- [ ] **Step 3: Add default no-op in base.py**

In `exchanges/base.py`, after the existing `subscribe_fills` abstract method, add:

```python
    def subscribe_funding(
        self, callback: "Callable[..., Awaitable[None]]",
    ) -> None:
        """Register a callback fired once per funding payment received from
        the exchange. Default: no-op (adapters that support funding history
        override this). Engine relies on the override to populate the
        operation's funding_paid_token0/1 accumulators.
        """
        return None
```

This is **not** abstract — providing a default means existing tests that mock `ExchangeAdapter` don't break.

- [ ] **Step 4: Run test to verify pass**

Run: `python -m pytest tests/test_lighter_adapter.py::test_subscribe_funding_is_callable_on_base_adapter -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add exchanges/base.py tests/test_lighter_adapter.py
git commit -m "feat(base): subscribe_funding default no-op on ExchangeAdapter

Per spec 2026-05-08-pnl-funding-accumulator-design. Concrete adapters
(LighterAdapter) override; other adapters (mock, dydx) inherit the
no-op so funding-aware engine wiring is harmless when wired against
non-supporting backends.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: LighterAdapter — `_funding_callback` slot + `subscribe_funding` override

**Files:**
- Modify: `exchanges/lighter.py:111-180` (add slots), `exchanges/lighter.py:629-642` (add subscribe_funding near subscribe_orderbook)
- Test: `tests/test_lighter_adapter.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lighter_adapter.py`:

```python
def test_subscribe_funding_stores_callback_on_lighter():
    """LighterAdapter override persists the callback for the poller."""
    _install_lighter_stub()
    a = _make_adapter()
    async def cb(entry): pass
    a.subscribe_funding(cb)
    assert a._funding_callback is cb
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_lighter_adapter.py::test_subscribe_funding_stores_callback_on_lighter -v`
Expected: FAIL with `AttributeError: '_funding_callback'`

- [ ] **Step 3: Add slots and override**

In `exchanges/lighter.py`, inside `__init__` after the existing `_reconcile_task` slot at ~line 177, add:

```python
        # Funding poller (Lighter-specific): periodic HTTP poll of
        # AccountApi.position_funding emits each entry to the callback
        # registered via subscribe_funding. Engine uses this to populate
        # funding_paid_token0/1 on the active operation.
        self._funding_callback: Callable[..., Awaitable[None]] | None = None
        self._funding_task: asyncio.Task | None = None
```

In `exchanges/lighter.py`, near `subscribe_orderbook` at ~line 629, add a new method:

```python
    def subscribe_funding(
        self, callback: Callable[..., Awaitable[None]],
    ) -> None:
        """Register a callback invoked per funding payment. Engine uses
        this to populate funding_paid_token0/1 on the active operation.
        The poller (started in connect) drives invocations."""
        self._funding_callback = callback
```

- [ ] **Step 4: Run test to verify pass**

Run: `python -m pytest tests/test_lighter_adapter.py::test_subscribe_funding_stores_callback_on_lighter -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add exchanges/lighter.py tests/test_lighter_adapter.py
git commit -m "feat(lighter): _funding_callback slot + subscribe_funding override

Stores the engine's funding handler on the adapter. Poller (next task)
will fire it per entry. Slots also include _funding_task for the
background poller wired in connect/disconnect later.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: LighterAdapter — `_fetch_position_funding` HTTP wrapper

**Files:**
- Modify: `exchanges/lighter.py` (add method near `_fetch_short_size_via_http` at line 1061)
- Test: `tests/test_lighter_adapter.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lighter_adapter.py`:

```python
@pytest.mark.asyncio
async def test_fetch_position_funding_returns_entries():
    """_fetch_position_funding wraps account_api.position_funding and
    returns the entries list (or empty on error)."""
    _install_lighter_stub()
    a = _make_adapter()
    # Mock SDK response
    a._account_api = MagicMock()
    fake_entries = [
        MagicMock(funding_id=1, market_id=0, timestamp=1700000000,
                  change="0.10", rate="0.0001", position_size="0.0148",
                  position_side="short"),
        MagicMock(funding_id=2, market_id=50, timestamp=1700000005,
                  change="-0.05", rate="-0.0001", position_size="100.0",
                  position_side="short"),
    ]
    a._account_api.position_funding = AsyncMock(return_value=MagicMock(
        position_fundings=fake_entries,
        next_cursor=None,
    ))
    entries = await a._fetch_position_funding(limit=100)
    assert len(entries) == 2
    assert entries[0].funding_id == 1
    assert entries[1].market_id == 50

@pytest.mark.asyncio
async def test_fetch_position_funding_returns_empty_on_error():
    """HTTP errors must not crash the poller — return empty list."""
    _install_lighter_stub()
    a = _make_adapter()
    a._account_api = MagicMock()
    a._account_api.position_funding = AsyncMock(side_effect=RuntimeError("boom"))
    entries = await a._fetch_position_funding(limit=100)
    assert entries == []
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_lighter_adapter.py::test_fetch_position_funding_returns_entries tests/test_lighter_adapter.py::test_fetch_position_funding_returns_empty_on_error -v`
Expected: FAIL with `AttributeError: '_fetch_position_funding'`

- [ ] **Step 3: Implement the wrapper**

In `exchanges/lighter.py`, after `_fetch_short_size_via_http` at ~line 1090, add:

```python
    async def _fetch_position_funding(
        self, *, limit: int = 100,
    ) -> list:
        """Fetch the most-recent funding payments for this account from
        Lighter's position_funding endpoint. Returns the SDK's typed
        PositionFunding objects (or empty list on HTTP/parse failure
        so the poller doesn't crash).

        We do not paginate via cursor here — the poll cadence is 60 s
        and the page size (100) covers far more than one cycle on any
        reasonable funding-history rate. If we ever lag enough that
        100 entries don't cover the gap, the next poll catches up.
        """
        try:
            resp = await self._account_api.position_funding(
                account_index=self._account_index,
                limit=limit,
            )
        except Exception as e:
            logger.warning(f"_fetch_position_funding failed: {e}")
            return []
        return list(getattr(resp, "position_fundings", None) or [])
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_lighter_adapter.py::test_fetch_position_funding_returns_entries tests/test_lighter_adapter.py::test_fetch_position_funding_returns_empty_on_error -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add exchanges/lighter.py tests/test_lighter_adapter.py
git commit -m "feat(lighter): _fetch_position_funding HTTP wrapper

Wraps AccountApi.position_funding with logging + safe-empty on errors
so the poller can call it in a tight loop without a transient HTTP
failure killing the background task.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: LighterAdapter — `_funding_poller_iteration` (one pass: fetch + emit per entry)

**Files:**
- Modify: `exchanges/lighter.py` (add method near `_reconcile_once` at ~line 525)
- Test: `tests/test_lighter_adapter.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lighter_adapter.py`:

```python
@pytest.mark.asyncio
async def test_funding_poller_iteration_fires_callback_per_entry():
    """One poller iteration fetches entries and fires the callback per
    entry. Dedup happens engine-side, so adapter emits everything."""
    _install_lighter_stub()
    a = _make_adapter()
    a._account_api = MagicMock()
    e1 = MagicMock(funding_id=1, market_id=0, timestamp=1700000000,
                   change="0.10", rate="0.0001", position_size="0.0148",
                   position_side="short")
    e2 = MagicMock(funding_id=2, market_id=50, timestamp=1700000005,
                   change="-0.05", rate="-0.0001", position_size="100.0",
                   position_side="short")
    a._account_api.position_funding = AsyncMock(return_value=MagicMock(
        position_fundings=[e1, e2], next_cursor=None,
    ))
    received: list = []
    async def cb(entry): received.append(entry)
    a._funding_callback = cb
    await a._funding_poller_iteration()
    assert received == [e1, e2]

@pytest.mark.asyncio
async def test_funding_poller_iteration_noop_when_no_callback():
    """If subscribe_funding hasn't been called yet, iteration still
    fetches but doesn't crash."""
    _install_lighter_stub()
    a = _make_adapter()
    a._account_api = MagicMock()
    a._account_api.position_funding = AsyncMock(return_value=MagicMock(
        position_fundings=[MagicMock()], next_cursor=None,
    ))
    a._funding_callback = None
    await a._funding_poller_iteration()  # must not raise
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_lighter_adapter.py::test_funding_poller_iteration_fires_callback_per_entry tests/test_lighter_adapter.py::test_funding_poller_iteration_noop_when_no_callback -v`
Expected: FAIL with `AttributeError: '_funding_poller_iteration'`

- [ ] **Step 3: Implement iteration**

In `exchanges/lighter.py`, after `_reconcile_once` at ~line 590, add:

```python
    async def _funding_poller_iteration(self) -> None:
        """One pass: fetch recent funding entries and dispatch each to
        the engine callback. Dedup + ts filtering live on the engine
        side (it knows the active op's started_at and what funding_ids
        have been counted)."""
        if self._funding_callback is None:
            # Still fetch (cheap and forces an API health probe), then
            # drop on the floor — engine hasn't subscribed yet.
            await self._fetch_position_funding(limit=100)
            return
        entries = await self._fetch_position_funding(limit=100)
        for entry in entries:
            try:
                await self._funding_callback(entry)
            except Exception as e:
                logger.warning(
                    f"funding callback raised on entry "
                    f"{getattr(entry, 'funding_id', '?')}: {e}"
                )
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_lighter_adapter.py::test_funding_poller_iteration_fires_callback_per_entry tests/test_lighter_adapter.py::test_funding_poller_iteration_noop_when_no_callback -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add exchanges/lighter.py tests/test_lighter_adapter.py
git commit -m "feat(lighter): _funding_poller_iteration emits entries to callback

Dedup + filter responsibility moved to engine (where the active op's
started_at and funding_id tracking naturally live). Adapter's job is
just to fetch + emit. A callback exception is caught so one bad entry
doesn't kill the loop.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: LighterAdapter — `_funding_poller_loop` background task wired in connect/disconnect

**Files:**
- Modify: `exchanges/lighter.py` — `_funding_poller_loop` after `_reconciler_loop` (~line 597), wire in `connect()` (~line 261) and `disconnect()`.
- Test: `tests/test_lighter_adapter.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lighter_adapter.py`:

```python
@pytest.mark.asyncio
async def test_funding_poller_loop_runs_iteration_then_sleeps():
    """The loop body calls _funding_poller_iteration once, then awaits
    the sleep — patched to fire CancelledError so we exit deterministically."""
    _install_lighter_stub()
    a = _make_adapter()
    iters = 0
    async def fake_iteration():
        nonlocal iters
        iters += 1
    a._funding_poller_iteration = fake_iteration
    # Patch sleep to short-circuit and exit after first iter
    a._ws_closing = False
    sleep_calls = 0
    real_sleep = asyncio.sleep
    async def fake_sleep(d):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls == 1:
            a._ws_closing = True  # exit loop on next while check
        await real_sleep(0)
    import asyncio as _aio
    orig = _aio.sleep
    _aio.sleep = fake_sleep
    try:
        await a._funding_poller_loop()
    finally:
        _aio.sleep = orig
    assert iters >= 1
    assert sleep_calls >= 1
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_lighter_adapter.py::test_funding_poller_loop_runs_iteration_then_sleeps -v`
Expected: FAIL with `AttributeError: '_funding_poller_loop'`

- [ ] **Step 3: Implement the loop and wire in connect/disconnect**

In `exchanges/lighter.py`, after `_reconciler_loop` (~line 597), add:

```python
    async def _funding_poller_loop(self) -> None:
        """Background task: every 60 s, run one funding poller iteration.
        Started in connect(), cancelled in disconnect(). Catches
        per-iteration exceptions so a transient HTTP error doesn't crash
        the loop — next sleep retries.
        """
        while not self._ws_closing:
            try:
                await self._funding_poller_iteration()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(
                    f"Funding poller iteration failed: {type(e).__name__}: {e}"
                )
            try:
                await asyncio.sleep(60.0)
            except asyncio.CancelledError:
                return
```

In `exchanges/lighter.py`, locate the `connect()` method and find the line that starts the reconciler task (~line 261):

```python
        self._reconcile_task = asyncio.create_task(self._reconciler_loop())
```

Add immediately after:

```python
        self._funding_task = asyncio.create_task(self._funding_poller_loop())
```

In `exchanges/lighter.py`, find the `disconnect()` method (search for `_reconcile_task` cancellation, ~line 271). Where it cancels `_reconcile_task`, add a symmetric cancellation for `_funding_task`:

```python
        if self._funding_task and not self._funding_task.done():
            self._funding_task.cancel()
            try:
                await self._funding_task
            except (asyncio.CancelledError, Exception):
                pass
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_lighter_adapter.py::test_funding_poller_loop_runs_iteration_then_sleeps -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add exchanges/lighter.py tests/test_lighter_adapter.py
git commit -m "feat(lighter): _funding_poller_loop wired in connect/disconnect

Mirror of _reconciler_loop pattern. 60 s sleep between iterations
(funding cycle is hourly, no need for tighter cadence). Disconnect
cancels both reconciler and funding tasks symmetrically.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Engine — resolve `_token0_mid`/`_token1_mid` at startup + register callback

**Files:**
- Modify: `engine/__init__.py` — `__init__` (~line 70+ where settings/exchange are stored)
- Test: `tests/test_engine_funding.py` (NEW)

- [ ] **Step 1: Write the failing test**

Create `tests/test_engine_funding.py`:

```python
"""Engine funding handler — wires LighterAdapter funding callback into
the active operation's funding_paid_token0/1 accumulator."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from state import StateHub


@pytest.mark.asyncio
async def test_engine_resolves_market_ids_at_init_for_dual_leg():
    """For a cross-pair (dual-leg) op, engine resolves token0_mid and
    token1_mid via exchange.get_market_meta during __init__. These are
    needed by _on_funding_payment to route per-leg."""
    from engine import GridMakerEngine

    state = StateHub(hedge_ratio=1.0)
    settings = MagicMock()
    settings.dydx_symbol_token0 = "ETH-USD"
    settings.dydx_symbol_token1 = "ARB-USD"
    settings.threshold_aggressive = 0.01
    settings.max_open_orders = 200
    settings.pool_token0_symbol = "ETH"
    settings.pool_token1_symbol = "ARB"
    settings.alert_webhook_url = ""
    settings.dydx_symbol = "ETH-USD"
    settings.min_rebalance_notional_usd = 0.50

    db = MagicMock()
    exchange = MagicMock()
    exchange.name = "lighter"
    # Two market metas — tests the per-leg resolution.
    def market_meta_for(symbol):
        m = MagicMock()
        m.market_index = 0 if symbol == "ETH-USD" else 50
        return m
    exchange.get_market_meta = AsyncMock(side_effect=lambda s: market_meta_for(s))
    exchange.subscribe_funding = MagicMock()

    pool = MagicMock(); beefy = MagicMock()
    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=18,
    )
    # __init__ doesn't await; the async resolve runs in a startup hook
    # the engine exposes as resolve_market_ids_for_funding.
    await engine.resolve_market_ids_for_funding()
    assert engine._token0_mid == 0
    assert engine._token1_mid == 50
    # And the callback was registered.
    exchange.subscribe_funding.assert_called_once()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_engine_funding.py::test_engine_resolves_market_ids_at_init_for_dual_leg -v`
Expected: FAIL — engine has no `resolve_market_ids_for_funding`, no `_token0_mid`, etc.

- [ ] **Step 3: Implement resolution + registration**

In `engine/__init__.py`, locate the `GridMakerEngine.__init__` method. After existing `self._settings`, `self._hub`, etc. assignments, add slots:

```python
        # Per-leg market IDs (resolved post-init via
        # resolve_market_ids_for_funding(); may be None until that
        # awaitable completes — handler tolerates).
        self._token0_mid: int | None = None
        self._token1_mid: int | None = None
```

At the end of `__init__`, register the callback (synchronous):

```python
        # Funding accumulator: adapter calls our handler per payment.
        # Default no-op on adapters that don't implement it.
        self._exchange.subscribe_funding(self._on_funding_payment)
```

Add a new method:

```python
    async def resolve_market_ids_for_funding(self) -> None:
        """Resolve the market_index for token0 and token1 perp symbols.
        Called once from the app startup path after the adapter is
        connected (and metadata cached). Stored mids are used by
        _on_funding_payment to route per-leg DB writes.
        """
        try:
            t0 = self._settings.dydx_symbol_token0
            if t0:
                m0 = await self._exchange.get_market_meta(t0)
                self._token0_mid = int(m0.market_index)
        except Exception as e:
            logger.warning(f"resolve_market_ids_for_funding token0 failed: {e}")
        try:
            t1 = self._settings.dydx_symbol_token1
            if t1:
                m1 = await self._exchange.get_market_meta(t1)
                self._token1_mid = int(m1.market_index)
        except Exception as e:
            logger.warning(f"resolve_market_ids_for_funding token1 failed: {e}")
```

Also add a stub `_on_funding_payment` so the engine can register it before Task 7 implements the body:

```python
    async def _on_funding_payment(self, entry) -> None:
        """Handle one funding payment from the exchange. Stub —
        implemented in Task 7."""
        return None
```

- [ ] **Step 4: Run test to verify pass**

Run: `python -m pytest tests/test_engine_funding.py::test_engine_resolves_market_ids_at_init_for_dual_leg -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_engine_funding.py
git commit -m "feat(engine): resolve_market_ids_for_funding + callback registration

Engine __init__ wires the funding callback onto the adapter (no-op on
adapters without subscribe_funding). The async resolve runs from the
app startup hook (next change to app.py) so meta is fetched after the
adapter connects.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Engine — `_on_funding_payment` handler with sign + dedup + op filter

**Files:**
- Modify: `engine/__init__.py` — replace stub from Task 6 with full handler
- Test: `tests/test_engine_funding.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_engine_funding.py`:

```python
import time
from engine.operation import Operation, OperationState


def _make_engine_with_funding_state(token0_mid=0, token1_mid=50):
    """Build an engine ready to test _on_funding_payment in isolation."""
    from engine import GridMakerEngine
    state = StateHub(hedge_ratio=1.0)
    state.operation_state = "active"
    state.current_operation_id = 42

    settings = MagicMock()
    settings.dydx_symbol_token0 = "ETH-USD"
    settings.dydx_symbol_token1 = "ARB-USD"
    settings.threshold_aggressive = 0.01
    settings.max_open_orders = 200
    settings.pool_token0_symbol = "ETH"
    settings.pool_token1_symbol = "ARB"
    settings.alert_webhook_url = ""
    settings.dydx_symbol = "ETH-USD"
    settings.min_rebalance_notional_usd = 0.50

    db = MagicMock()
    db.add_to_operation_accumulator = AsyncMock()
    db.get_operation = AsyncMock(return_value={
        "id": 42, "started_at": 1700000000.0, "status": "active",
        "baseline_eth_price": 2000.0, "baseline_pool_value_usd": 50.0,
        "baseline_amount0": 0.01, "baseline_amount1": 100.0,
        "baseline_collateral": 100.0,
    })

    exchange = MagicMock()
    exchange.name = "lighter"
    exchange.subscribe_funding = MagicMock()
    exchange.get_market_meta = AsyncMock()

    pool = MagicMock(); beefy = MagicMock()
    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=18,
    )
    engine._token0_mid = token0_mid
    engine._token1_mid = token1_mid
    return engine, db


@pytest.mark.asyncio
async def test_on_funding_payment_writes_token0_when_market_id_matches():
    """Funding entry for token0_mid -> writes funding_paid_token0."""
    engine, db = _make_engine_with_funding_state(token0_mid=0, token1_mid=50)
    entry = MagicMock(
        funding_id=1, market_id=0, timestamp=1700001000,
        change="0.10",  # user received +$0.10
    )
    await engine._on_funding_payment(entry)
    db.add_to_operation_accumulator.assert_awaited_once_with(
        42, "funding_paid_token0", -0.10,
    )


@pytest.mark.asyncio
async def test_on_funding_payment_writes_token1_when_market_id_matches():
    """Funding entry for token1_mid -> writes funding_paid_token1."""
    engine, db = _make_engine_with_funding_state(token0_mid=0, token1_mid=50)
    entry = MagicMock(
        funding_id=2, market_id=50, timestamp=1700001000,
        change="-0.25",  # user paid $0.25
    )
    await engine._on_funding_payment(entry)
    db.add_to_operation_accumulator.assert_awaited_once_with(
        42, "funding_paid_token1", 0.25,
    )


@pytest.mark.asyncio
async def test_on_funding_payment_skips_when_no_active_op():
    """No active op -> no DB write, no error."""
    engine, db = _make_engine_with_funding_state()
    engine._hub.current_operation_id = None
    entry = MagicMock(funding_id=3, market_id=0, timestamp=1700001000, change="0.10")
    await engine._on_funding_payment(entry)
    db.add_to_operation_accumulator.assert_not_called()


@pytest.mark.asyncio
async def test_on_funding_payment_skips_when_market_id_unmatched():
    """Funding for a market we're not hedging is ignored."""
    engine, db = _make_engine_with_funding_state(token0_mid=0, token1_mid=50)
    entry = MagicMock(funding_id=4, market_id=999, timestamp=1700001000, change="0.10")
    await engine._on_funding_payment(entry)
    db.add_to_operation_accumulator.assert_not_called()


@pytest.mark.asyncio
async def test_on_funding_payment_skips_entries_before_op_started():
    """Entries with timestamp < op.started_at are ignored (backfill bound)."""
    engine, db = _make_engine_with_funding_state(token0_mid=0, token1_mid=50)
    entry = MagicMock(funding_id=5, market_id=0, timestamp=1699999999, change="0.10")
    await engine._on_funding_payment(entry)
    db.add_to_operation_accumulator.assert_not_called()


@pytest.mark.asyncio
async def test_on_funding_payment_dedupes_by_funding_id():
    """Same funding_id seen twice -> writes only once."""
    engine, db = _make_engine_with_funding_state(token0_mid=0, token1_mid=50)
    entry = MagicMock(funding_id=6, market_id=0, timestamp=1700001000, change="0.10")
    await engine._on_funding_payment(entry)
    await engine._on_funding_payment(entry)  # second call same funding_id
    assert db.add_to_operation_accumulator.await_count == 1


@pytest.mark.asyncio
async def test_on_funding_payment_skips_when_market_ids_unresolved():
    """If _token0_mid/_token1_mid haven't loaded yet, skip without
    marking funding_id seen — next call (after metadata loads) retries."""
    engine, db = _make_engine_with_funding_state(token0_mid=None, token1_mid=None)
    entry = MagicMock(funding_id=7, market_id=0, timestamp=1700001000, change="0.10")
    await engine._on_funding_payment(entry)
    db.add_to_operation_accumulator.assert_not_called()
    # And not in the seen set:
    assert 7 not in engine._seen_funding_ids
```

- [ ] **Step 2: Run to verify failures**

Run: `python -m pytest tests/test_engine_funding.py -v`
Expected: All 7 new tests FAIL because `_on_funding_payment` is currently a no-op.

- [ ] **Step 3: Implement the handler**

In `engine/__init__.py`, replace the stub `_on_funding_payment` from Task 6 with the full implementation. Also add a `_seen_funding_ids` slot in `__init__`:

In `__init__`, near the `_token0_mid` slot:

```python
        # Funding payments already attributed to the current op (dedup
        # against the poller emitting the same entry on consecutive
        # iterations). Cleared on op transitions.
        self._seen_funding_ids: set[int] = set()
        self._seen_funding_ids_op_id: int | None = None
```

Replace the `_on_funding_payment` method body with:

```python
    async def _on_funding_payment(self, entry) -> None:
        """Handle one funding payment from the exchange.

        Skips:
          - no active op (op_id is None) → entry will be picked up by
            the next op if/when one starts (engine doesn't carry funding
            across op boundaries).
          - market_id not in our hedged legs.
          - market_ids unresolved (don't mark seen — next call retries).
          - timestamp before op.started_at (backfill bound).
          - funding_id already counted for this op (dedup).

        Writes signed amount to the appropriate per-leg DB column,
        respecting pnl.py's convention that 'positive in DB = we paid':
          entry.change > 0 (user received) → DB delta = -change
          entry.change < 0 (user paid)     → DB delta = -change (= +|change|)
        """
        op_id = self._hub.current_operation_id
        if op_id is None:
            return
        # Reset dedup set on op transitions — funding from prior ops
        # was already attributed to those ops.
        if self._seen_funding_ids_op_id != op_id:
            self._seen_funding_ids = set()
            self._seen_funding_ids_op_id = op_id

        try:
            mid = int(getattr(entry, "market_id"))
            funding_id = int(getattr(entry, "funding_id"))
            ts = float(getattr(entry, "timestamp"))
            change = float(getattr(entry, "change") or 0)
        except (TypeError, ValueError, AttributeError) as e:
            logger.warning(f"funding payment parse failed: {e}")
            return

        # Resolve the leg.
        if self._token0_mid is None and self._token1_mid is None:
            # Metadata hasn't loaded yet — defer (don't mark seen).
            return
        if mid == self._token0_mid:
            field = "funding_paid_token0"
        elif mid == self._token1_mid:
            field = "funding_paid_token1"
        else:
            return  # not a leg we're hedging

        # Filter by op start.
        try:
            op_row = await self._db.get_operation(op_id)
            op_started_at = float((op_row or {}).get("started_at") or 0)
        except Exception:
            op_started_at = 0.0
        if ts < op_started_at:
            return

        # Dedup.
        if funding_id in self._seen_funding_ids:
            return
        self._seen_funding_ids.add(funding_id)

        delta = -change
        try:
            await self._db.add_to_operation_accumulator(op_id, field, delta)
        except Exception as e:
            logger.warning(
                f"funding accumulator write failed (op={op_id}, field={field}, "
                f"delta={delta}): {e}"
            )
            self._seen_funding_ids.discard(funding_id)  # allow retry
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_engine_funding.py -v`
Expected: All 7 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_engine_funding.py
git commit -m "feat(engine): _on_funding_payment with sign + dedup + ts filter

Routes per-leg: market_id matches token0_mid -> funding_paid_token0,
token1_mid -> funding_paid_token1, else skip. Sign convention preserved
from pnl.py (positive in DB = we paid; user-received entry.change > 0
becomes negative DB delta -> displays positive in breakdown). Dedup
keyed per-op so a poller emitting the same entry repeatedly only
counts once. Backfill bound: entries with ts < op.started_at skipped.
Unresolved market_ids defer (don't mark seen) so a metadata-late race
doesn't lose payments.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: App startup wiring — call `resolve_market_ids_for_funding` after adapter connects

**Files:**
- Modify: `app.py` — startup hook after `await exchange.connect()` and before engine starts

- [ ] **Step 1: Identify the startup path**

Read `app.py` to find where `exchange.connect()` is awaited and the engine starts iterating. The resolve must happen between those two so the handler has mids populated by the time the first WS funding-callback fires.

Run: `grep -n "exchange.connect\|create_app\|GridMakerEngine\|start_engine" app.py | head -20`

Identify the line where `await exchange.connect()` (or equivalent) completes. The engine instance is `engine` (or whatever name in scope). Insert immediately after.

- [ ] **Step 2: Add the resolve call**

In the startup path of `create_app` in `app.py`, after `await exchange.connect()` succeeds and the `GridMakerEngine` instance has been constructed, add:

```python
            # Resolve token0/token1 market_ids for funding handler.
            # Adapter metadata is populated by connect() — this is when
            # we can ask for market_index per symbol.
            try:
                await engine.resolve_market_ids_for_funding()
            except Exception as e:
                logger.warning(f"resolve_market_ids_for_funding skipped: {e}")
```

Make sure `logger` is in scope at this site (look at existing logger usage above).

- [ ] **Step 3: Run targeted tests**

Funding-related tests don't exercise app.py end-to-end, but ensure existing app tests still pass:

Run: `python -m pytest tests/ -q --no-header -x 2>&1 | tail -10`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add app.py
git commit -m "feat(app): resolve funding market_ids after exchange connect

Final wiring: after the adapter connects (and caches market metadata),
the engine resolves the per-leg market_index needed by the funding
handler. Wrapped in try/except so a single missing symbol doesn't
prevent the rest of the boot.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 9: Final integration check — run full suite + doc note

**Files:**
- Verify: full pytest suite passes
- Modify (optional): `docs/STATUS.md` to remove the "funding gap" if it's mentioned, or add the new mechanism

- [ ] **Step 1: Run full suite**

Run: `python -m pytest tests/ -q --no-header 2>&1 | tail -5`
Expected: all pass (existing 270 + ~12 new = ~282).

- [ ] **Step 2: Verify the spec's behavior with a one-shot script**

Create `scripts/_verify_funding_handler.py` (temporary, deleted in next step):

```python
"""One-shot: feed a synthetic funding entry into the engine handler and
confirm it would write to the DB column. Doesn't touch the live adapter
or exchange — pure handler test."""
import asyncio
from unittest.mock import AsyncMock, MagicMock
from state import StateHub
from engine import GridMakerEngine


async def main():
    state = StateHub(hedge_ratio=1.0)
    state.operation_state = "active"
    state.current_operation_id = 28  # current op

    settings = MagicMock()
    settings.dydx_symbol_token0 = "ETH-USD"
    settings.dydx_symbol_token1 = "ARB-USD"
    settings.threshold_aggressive = 0.01
    settings.max_open_orders = 200
    settings.pool_token0_symbol = "ETH"
    settings.pool_token1_symbol = "ARB"
    settings.alert_webhook_url = ""
    settings.dydx_symbol = "ETH-USD"
    settings.min_rebalance_notional_usd = 0.50

    db = MagicMock()
    db.add_to_operation_accumulator = AsyncMock()
    db.get_operation = AsyncMock(return_value={
        "id": 28, "started_at": 1700000000.0, "status": "active",
        "baseline_eth_price": 2280.0, "baseline_pool_value_usd": 50.0,
        "baseline_amount0": 0.0166, "baseline_amount1": 95.85,
        "baseline_collateral": 100.0,
    })

    exchange = MagicMock()
    exchange.name = "lighter"
    exchange.subscribe_funding = MagicMock()

    pool = MagicMock(); beefy = MagicMock()
    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=6,
    )
    engine._token0_mid = 0   # ETH on Lighter
    engine._token1_mid = 50  # ARB on Lighter

    received = MagicMock(
        funding_id=999, market_id=0, timestamp=1700001000,
        change="0.07",  # user received $0.07 funding
    )
    await engine._on_funding_payment(received)
    print(
        f"DB write call: {db.add_to_operation_accumulator.await_args}"
    )


if __name__ == "__main__":
    asyncio.run(main())
```

Run: `"C:/Users/Wallace/Python313/python.exe" scripts/_verify_funding_handler.py`
Expected output: `DB write call: call(28, 'funding_paid_token0', -0.07)`

- [ ] **Step 3: Delete the verification script**

```bash
rm scripts/_verify_funding_handler.py
```

- [ ] **Step 4: Update CLAUDE.md note**

In `CLAUDE.md`, find the section "Limitações conhecidas" and the line:

```
- **LP fees attribution:** Phase 1.2 NÃO implementa listener de Beefy `Harvest` — `lp_fees_earned` fica em 0 até a gente adicionar isso (ficou como gap conhecido)
```

Add a sibling line right below:

```
- **Funding attribution (Lighter):** `funding_paid_token0/1` populated via background poller (60 s) introduced 2026-05-08 — see spec `docs/superpowers/specs/2026-05-08-pnl-funding-accumulator-design.md`. Restart uvicorn to pick up the poller after pulling the change.
```

- [ ] **Step 5: Commit + push**

```bash
git add CLAUDE.md
git commit -m "docs: note funding accumulator wiring + restart requirement

Records that funding now flows from Lighter HTTP poller into the
operation accumulators. LP fees gap line stays as-is (separate spec).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
git push
```

---

## Self-Review

**Spec coverage:**
- §Architecture flow boxes (1) Lighter API → (2) `_fetch_position_funding` → (3) `_funding_poller_loop` → (4) callback → (5) `_on_funding_payment` → (6) DB accumulator: covered by Tasks 3, 4, 5, 6, 7 respectively. ✓
- §Sign convention (Lighter `change > 0` user received → DB delta = `-change`): Task 7 implements + tests both directions. ✓
- §Backfill at startup: Task 7 filters by `op.started_at` (effectively backfilling on first poll iteration when adapter goes live). ✓
- §Dedup via `funding_id`: Task 7 implements per-op set. ✓
- §Poll interval 60 s: Task 5 hardcoded. ✓
- §Background task lifecycle (started in `connect`, cancelled in `disconnect`): Task 5. ✓
- §Engine wiring (`subscribe_funding(callback)`, mid resolution): Tasks 2, 6, 8. ✓
- §Edge case "callback before metadata loads": Task 7 handles via `_token0_mid is None` check (don't mark seen). ✓
- §"API ordering assumption" risk note: handler is order-agnostic (filters per entry by `ts < op.started_at`), no assumption baked into code. ✓
- §Out of scope LP fees: not implemented, CLAUDE.md note in Task 9 mentions only funding. ✓

**Placeholder scan:** all task steps have concrete code or commands. No "TBD" or "add error handling".

**Type consistency:**
- `_token0_mid`/`_token1_mid` typed `int | None`, used as `int` after the `is None` guard.
- `_funding_callback` typed `Callable[..., Awaitable[None]] | None` consistently across Tasks 2, 4, 5.
- `_seen_funding_ids` typed `set[int]` consistently across Tasks 7.
- `_funding_task` typed `asyncio.Task | None` consistently across Tasks 2, 5.

Plan ready for execution.
