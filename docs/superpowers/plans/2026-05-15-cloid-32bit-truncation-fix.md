# Cloid 32-bit Truncation Fix — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 64-bit cloid generated locally vs 32-bit cloid stored by Lighter, so `_safety_reconcile` and `_grid_event_loop` stop seeing every live order as orphan and every local cloid as filled.

**Architecture:** Truncate cloids at the source (`_next_cloid`, `_next_cloid_for_leg`) so the in-memory key always matches what `get_open_orders` returns. Add a one-time `cancel_all_stops` at the top of `engine.start()` (gated on active operation + connected exchange) so a restart leaves no orphan stops from a previous run with a different cloid namespace.

**Tech Stack:** Python 3.12 (prod) / 3.13 (Windows dev), asyncio, pytest + pytest-asyncio, unittest.mock.

**Spec:** `docs/superpowers/specs/2026-05-15-cloid-32bit-truncation-fix-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `engine/__init__.py` | Modify line 818-832 | Truncate `_next_cloid` to 32 bits |
| `engine/__init__.py` | Modify line 2058-2072 | Truncate `_next_cloid_for_leg` to 32 bits |
| `engine/__init__.py` | Modify `start()` around line 767 | Call `cancel_all_stops` before launching loops when operation is active |
| `tests/test_engine_cloid.py` | Modify | Add stricter 32-bit assertion test |
| `tests/test_engine_event_driven_grid.py` | Modify | Add round-trip reconciler test + cancel-on-start test |

No new files. All changes are localized to engine + its tests.

---

### Task 1: Failing test — `_next_cloid_for_leg` must fit in 32 bits

**Files:**
- Test: `tests/test_engine_cloid.py`

- [ ] **Step 1: Add the failing test**

Append at the end of `tests/test_engine_cloid.py`:

```python
def test_next_cloid_for_leg_fits_in_int32():
    """Cloid must fit in unsigned int32 — Lighter SDK truncates
    client_order_index to 32 bits when sending the SL order. If we
    let the engine store the untruncated value in _local_grid, the
    reconciler will never match cloids returned by get_open_orders.
    Regression for the 2026-05-15 bug where _local_grid kept 64-bit
    cloids (run_id<<32 | leg<<24 | seq) while Lighter returned only
    the low 32 bits, causing _safety_reconcile to treat every live
    order as orphan + every local cloid as filled.
    """
    engine = _make_engine()
    engine._cloid_seq = 10_000_000  # exercise high seq values
    for _ in range(100):
        c = engine._next_cloid_for_leg("ARB-USD")
        assert 0 <= c < 2**32, f"cloid {c} ({c:#x}) does not fit in uint32"


def test_next_cloid_fits_in_int32():
    """Same invariant for the level-based `_next_cloid` used by non-grid
    paths (legacy rebalance taker, ttl orders)."""
    engine = _make_engine()
    engine._cloid_seq = 10_000_000
    for i in range(100):
        c = engine._next_cloid(level_idx=i % 16)
        assert 0 <= c < 2**32, f"_next_cloid {c} ({c:#x}) does not fit in uint32"
```

- [ ] **Step 2: Run the new tests, confirm they FAIL**

Run: `python -m pytest tests/test_engine_cloid.py::test_next_cloid_for_leg_fits_in_int32 tests/test_engine_cloid.py::test_next_cloid_fits_in_int32 -v`

Expected: both tests FAIL with `AssertionError: cloid <big-number> (0x6a07a0c3a0989681) does not fit in uint32` (or similar — value depends on time).

---

### Task 2: Truncate `_next_cloid_for_leg` to 32 bits

**Files:**
- Modify: `engine/__init__.py:2058-2072`

- [ ] **Step 1: Apply the fix**

Replace the existing `_next_cloid_for_leg` method body (lines 2058-2072 of `engine/__init__.py`):

```python
    def _next_cloid_for_leg(self, symbol: str) -> int:
        """Generate a cloid scoped per leg so concurrent fires from different
        legs never collide.

        Layout (32 bits): leg_byte (8) | seq (24). The Lighter SDK truncates
        client_order_index to 32 bits on the wire; storing more than that in
        `_local_grid` makes the reconciler unable to match the values
        returned by `get_open_orders`. See spec
        `docs/superpowers/specs/2026-05-15-cloid-32bit-truncation-fix-design.md`.
        """
        self._cloid_seq += 1
        leg_byte = 0xA0 if symbol == self._settings.dydx_symbol_token0 else 0xA1
        return (
            (leg_byte << 24) |
            (self._cloid_seq & 0xFFFFFF)
        )
```

- [ ] **Step 2: Run the failing test, confirm it PASSES**

Run: `python -m pytest tests/test_engine_cloid.py::test_next_cloid_for_leg_fits_in_int32 -v`

Expected: PASS.

- [ ] **Step 3: Run the full `test_engine_cloid.py` suite to confirm no regression**

Run: `python -m pytest tests/test_engine_cloid.py -v`

Expected: all 5 tests pass (2 new + 3 preexisting). `test_cloid_fits_in_int64` still passes (32-bit fits inside int64).

- [ ] **Step 4: Commit**

```bash
git add tests/test_engine_cloid.py engine/__init__.py
git commit -m "fix(engine): truncate _next_cloid_for_leg to 32 bits

Lighter SDK truncates client_order_index to 32 bits. Keeping the
64-bit value in _local_grid made the reconciler unable to intersect
the in-memory cloid set with the set returned by get_open_orders.
Symptom in prod 2026-05-15: every live order seen as orphan +
every local cloid seen as filled every 90s."
```

---

### Task 3: Truncate `_next_cloid` to 32 bits

**Files:**
- Modify: `engine/__init__.py:818-832`

- [ ] **Step 1: Apply the fix**

Replace the existing `_next_cloid` method body (lines 818-832 of `engine/__init__.py`):

```python
    def _next_cloid(self, level_idx: int) -> int:
        """Generate unique cloid as int.

        Layout (32 bits): level_idx (8) | seq (24). Truncated to match what
        the Lighter SDK actually stores (client_order_index is 32 bits on
        the wire). 24-bit seq = 16M unique cloids per (level) per run —
        effectively unlimited. The 256-cloid wraparound that motivated the
        2026-05-15-morning fix is still resolved here: seq is 24 bits, not
        8. Only the inert `run_id<<32` was dropped.
        """
        self._cloid_seq += 1
        return (
            ((level_idx & 0xFF) << 24) |
            (self._cloid_seq & 0xFFFFFF)
        )
```

- [ ] **Step 2: Run the failing test, confirm it PASSES**

Run: `python -m pytest tests/test_engine_cloid.py::test_next_cloid_fits_in_int32 -v`

Expected: PASS.

- [ ] **Step 3: Run the full `test_engine_cloid.py` suite to confirm no regression**

Run: `python -m pytest tests/test_engine_cloid.py -v`

Expected: all 5 tests pass.

- [ ] **Step 4: Commit**

```bash
git add engine/__init__.py
git commit -m "fix(engine): truncate _next_cloid to 32 bits

Same invariant as the previous commit: the Lighter SDK only persists
32 bits of client_order_index. Drops the inert run_id high half;
keeps the 24-bit seq (16M unique cloids per run, well above the
2026-05-15-morning fix's wraparound threshold of 256)."
```

---

### Task 4: Regression test — reconciler matches `_local_grid` against `get_open_orders` after initial post

**Files:**
- Modify: `tests/test_engine_event_driven_grid.py`

- [ ] **Step 1: Add the test**

Append at the end of `tests/test_engine_event_driven_grid.py`:

```python
@pytest.mark.asyncio
async def test_local_grid_keys_intersect_live_cloids_after_post():
    """Regression: after `_post_initial_grid` (or any place_stop_market
    flow), the cloid stored in `_local_grid` must equal the cloid that
    `get_open_orders` returns. Pre-fix the engine kept 64-bit values
    locally while Lighter stored only the low 32 bits, so
    `set(_local_grid) & live_cloids` was always empty.

    This is a regression guard for the spec
    `docs/superpowers/specs/2026-05-15-cloid-32bit-truncation-fix-design.md`.
    """
    engine = _make_engine()

    # Simulate the engine generating a cloid the same way _post_initial_grid does.
    cloid = engine._next_cloid_for_leg("ARB-USD")

    # Simulate the engine storing in _local_grid (what _post_initial_grid does).
    engine._local_grid[cloid] = GridStop(cloid, "sell", 0.130, 3.0)

    # Simulate Lighter returning what it actually persisted: the 32-bit truncated cloid.
    lighter_persisted_cloid = cloid & 0xFFFFFFFF
    live_by_cloid = {lighter_persisted_cloid: {
        "cloid": str(lighter_persisted_cloid), "side": "sell",
        "trigger_price": 0.130, "size": 3.0, "order_index": 999,
    }}

    # Reconciler's set logic:
    local_cloids = set(engine._local_grid.keys())
    live_cloids = set(live_by_cloid.keys())
    orphans = live_cloids - local_cloids
    missing = local_cloids - live_cloids

    assert orphans == set(), (
        f"reconciler should see zero orphans; got {orphans}. "
        f"local={local_cloids} live={live_cloids}"
    )
    assert missing == set(), (
        f"reconciler should see zero missing; got {missing}. "
        f"local={local_cloids} live={live_cloids}"
    )
```

- [ ] **Step 2: Run the new test, confirm it PASSES**

Run: `python -m pytest tests/test_engine_event_driven_grid.py::test_local_grid_keys_intersect_live_cloids_after_post -v`

Expected: PASS. (This test must pass only because tasks 2 + 3 already truncated `_next_cloid_for_leg`. If it fails, the previous fixes did not take effect.)

- [ ] **Step 3: Commit**

```bash
git add tests/test_engine_event_driven_grid.py
git commit -m "test(engine): regression guard for cloid set intersection

Asserts that the cloid stored in _local_grid equals the cloid that
Lighter returns via get_open_orders. Catches the class of bug where
the engine generates cloids wider than the SDK persists."
```

---

### Task 5: Failing test — `engine.start()` cancels existing stops when an op is active

**Files:**
- Modify: `tests/test_engine_event_driven_grid.py`

- [ ] **Step 1: Add the failing test**

Append at the end of `tests/test_engine_event_driven_grid.py`:

```python
@pytest.mark.asyncio
async def test_engine_start_cancels_existing_stops_when_op_active():
    """On startup with an active operation, the engine cancels any
    pre-existing stop orders for the active symbol before launching the
    grid loops. This prevents the new (post-fix 32-bit) cloid namespace
    from colliding with leftover stops from a previous run.

    No-op when there is no active operation (engine doesn't own a grid).
    """
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.connect = AsyncMock()
    engine._exchange.disconnect = AsyncMock()
    engine._exchange.subscribe_fills = AsyncMock()
    engine._exchange.cancel_all_stops = AsyncMock()
    engine._db.get_active_operation = AsyncMock(
        return_value={"id": 42, "status": "active"},
    )
    # Bypass chain readers (engine.start() builds them when None — let
    # those exist as MagicMocks so the construction branch is skipped).
    engine._pool_reader = MagicMock()
    engine._beefy_reader = MagicMock()
    # Skip the reconciler path (predictive_grid_v2 setting suppresses it).
    engine._settings.predictive_grid_v2 = True

    # Stub out the long-running loops so start() returns quickly.
    async def _noop():
        await asyncio.sleep(0)
    engine._main_loop = _noop
    engine._grid_event_loop = _noop

    await engine.start()
    try:
        engine._exchange.cancel_all_stops.assert_called_once_with(
            symbol=engine._settings.dydx_symbol_token0,
        )
    finally:
        await engine.stop()


@pytest.mark.asyncio
async def test_engine_start_skips_cancel_when_no_op_active():
    """No active op -> engine doesn't own a grid, must not touch Lighter."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.connect = AsyncMock()
    engine._exchange.disconnect = AsyncMock()
    engine._exchange.subscribe_fills = AsyncMock()
    engine._exchange.cancel_all_stops = AsyncMock()
    engine._db.get_active_operation = AsyncMock(return_value=None)
    engine._pool_reader = MagicMock()
    engine._beefy_reader = MagicMock()
    engine._settings.predictive_grid_v2 = True

    async def _noop():
        await asyncio.sleep(0)
    engine._main_loop = _noop
    engine._grid_event_loop = _noop

    await engine.start()
    try:
        engine._exchange.cancel_all_stops.assert_not_called()
    finally:
        await engine.stop()
```

Also ensure `import asyncio` is at the top of the file (it should already be present; if not, add `import asyncio` near the other imports).

- [ ] **Step 2: Run the new tests, confirm `test_engine_start_cancels_existing_stops_when_op_active` FAILS**

Run: `python -m pytest tests/test_engine_event_driven_grid.py::test_engine_start_cancels_existing_stops_when_op_active tests/test_engine_event_driven_grid.py::test_engine_start_skips_cancel_when_no_op_active -v`

Expected: the `cancels_existing_stops_when_op_active` test fails with `AssertionError: Expected 'cancel_all_stops' to be called once. Called 0 times.` The `skips_cancel_when_no_op_active` test should already pass (cancel_all_stops never called today).

---

### Task 6: Add `cancel_all_stops` call to `engine.start()`

**Files:**
- Modify: `engine/__init__.py` around line 752 (after the `Restore active operation` block, before `subscribe_fills`)

- [ ] **Step 1: Find the insertion point**

Read `engine/__init__.py` lines 746-770. The current block is:

```python
        # Restore active operation, if any
        active_op = await self._db.get_active_operation()
        if active_op is not None:
            self._hub.current_operation_id = active_op["id"]
            self._hub.operation_state = active_op["status"]
            logger.info(f"Restored active operation {active_op['id']} (status={active_op['status']})")

        # subscribe_fills is also best-effort. ...
        if self._hub.connected_exchange:
            try:
                await self._exchange.subscribe_fills(
                    self._settings.dydx_symbol, self._on_fill,
                )
            except Exception as e:
                ...

        self._running = True
        self._task = asyncio.create_task(self._main_loop())
        ...
```

- [ ] **Step 2: Insert the cancel-all block**

Replace the `Restore active operation` block (around lines 747-751) plus the immediately-following blank line with this expanded version:

```python
        # Restore active operation, if any
        active_op = await self._db.get_active_operation()
        if active_op is not None:
            self._hub.current_operation_id = active_op["id"]
            self._hub.operation_state = active_op["status"]
            logger.info(f"Restored active operation {active_op['id']} (status={active_op['status']})")

            # Cancel any leftover stop orders from a previous run on the
            # active symbol. After 2026-05-15's cloid 32-bit fix, the new
            # run's cloid namespace starts at seq=0 and could collide with
            # leftovers from a previous run that wasn't cleanly torn down.
            # No-op when Lighter has nothing live for this symbol.
            # Best-effort: a network failure here logs warning + proceeds;
            # _safety_reconcile (90s cadence) adopts whatever survives.
            if self._hub.connected_exchange:
                try:
                    await self._exchange.cancel_all_stops(
                        symbol=self._settings.dydx_symbol_token0,
                    )
                    logger.info(
                        f"engine.start cancel_all_stops cleared pre-existing "
                        f"stops for {self._settings.dydx_symbol_token0}"
                    )
                except Exception as e:
                    logger.warning(
                        f"engine.start cancel_all_stops failed (proceeding; "
                        f"_safety_reconcile bootstrap will recover): {e}"
                    )
```

- [ ] **Step 3: Run the failing test, confirm it PASSES**

Run: `python -m pytest tests/test_engine_event_driven_grid.py::test_engine_start_cancels_existing_stops_when_op_active tests/test_engine_event_driven_grid.py::test_engine_start_skips_cancel_when_no_op_active -v`

Expected: both PASS.

- [ ] **Step 4: Re-run the full event-driven grid suite for regression**

Run: `python -m pytest tests/test_engine_event_driven_grid.py -v`

Expected: every test passes. The pre-existing `test_engine_start_creates_grid_event_loop_task` bypasses real `start()` body, so the new code does not affect it.

- [ ] **Step 5: Commit**

```bash
git add tests/test_engine_event_driven_grid.py engine/__init__.py
git commit -m "feat(engine): cancel_all_stops on engine.start when op active

Pre-existing stop orders from a previous run would collide with the
new 32-bit cloid namespace (seq restarts at 0). Cancel them at
startup before the grid loops post a fresh 8+8 grid. Best-effort:
network failures log warning and proceed; safety reconcile recovers."
```

---

### Task 7: Full suite + commit if any regressions

**Files:**
- N/A (verification)

- [ ] **Step 1: Run the full test suite**

Run: `python -m pytest tests/ -v 2>&1 | tail -40`

Expected: every test passes. Pre-fix baseline was 408 passing.

- [ ] **Step 2: If any test fails, diagnose**

Read the failure. The cloid changes touch any test that:
- Hardcoded an expected 64-bit cloid in mock assertions
- Used `_next_cloid*` and inspected the run_id high bits

Likely none — the existing `test_engine_cloid.py` already had `0 <= c < 2**63` which is satisfied by 32-bit values; and the grid tests stub `_next_cloid_for_leg` with `side_effect=[9001, 9002]` style overrides that don't care about width.

If a test fails because of the truncation, fix the test (it was asserting a no-longer-true invariant) — do NOT loosen the truncation in source.

- [ ] **Step 3: No-op commit if nothing changed**

If step 1 was green and no edits were needed, skip. If step 2 required test edits, commit:

```bash
git add tests/
git commit -m "test(engine): align expectations with 32-bit cloid invariant"
```

---

### Task 8: Push branch + merge + deploy

**Files:**
- N/A (deploy)

- [ ] **Step 1: Push branch to origin**

```bash
git push -u origin claude/clever-liskov-0bbd80
```

- [ ] **Step 2: Merge to master locally (fast-forward)**

```bash
git checkout master
git merge --no-ff claude/clever-liskov-0bbd80 -m "Merge: cloid 32-bit truncation fix (event-driven reconciler)"
git push origin master
```

- [ ] **Step 3: Deploy to production**

```bash
ssh -o StrictHostKeyChecking=no -i ~/.ssh/id_ed25519 root@104.248.44.6 'cd /opt/automoney && git fetch && git checkout master && git pull --ff-only && systemctl restart automoney && sleep 3 && systemctl is-active automoney'
```

Expected output: `active`.

- [ ] **Step 4: Verify deploy via logs**

```bash
ssh -o StrictHostKeyChecking=no -i ~/.ssh/id_ed25519 root@104.248.44.6 'journalctl -u automoney --since "1 min ago" | grep -E "(cancel_all_stops|initial post|GridMakerEngine started|orphan)" | head -30'
```

Expected:
- One line `engine.start cancel_all_stops cleared pre-existing stops for ARB-USD` (or the active symbol).
- Several lines `initial post sell @ $0.xxx trigger $0.xxx` and `initial post buy @ $0.xxx trigger $0.xxx` (8 of each = 16 total ideally; sometimes 15 due to safety clamp on a level that lands too close to market — cosmetic per spec).
- ZERO lines containing `cancelled orphan` in the first 90 seconds. If any appear after the cancel_all_stops line, the fix did not take effect — rollback and investigate.

- [ ] **Step 5: Verify live grid via Lighter query**

```bash
ssh -o StrictHostKeyChecking=no -i ~/.ssh/id_ed25519 root@104.248.44.6 '/opt/automoney/venv/bin/python /tmp/sg.py 2>&1'
```

(The `/tmp/sg.py` is the live-state probe — already created earlier in this session. If missing, recreate via the snippet at the bottom of this plan.)

Expected:
- ~15-16 stop orders, balanced 8 buy + 8 sell (or 7/8 with the safety clamp).
- Triggers symmetric around `p_now`.

- [ ] **Step 6: Watch metrics for 5 minutes**

```bash
ssh -o StrictHostKeyChecking=no -i ~/.ssh/id_ed25519 root@104.248.44.6 'curl -s http://127.0.0.1:8000/metrics | grep -E "^bot_grid_writes_total|^bot_grid_stops"'
```

Wait 90s for `_safety_reconcile` to fire. Re-run.

Expected:
- `bot_grid_writes_total{reason="safety"}` stays small (≤ 2-3 from bootstrap path) instead of monotonically growing by N per 90s.
- `bot_grid_writes_total{reason="initial"}` = ~15-16 (one-shot).
- `bot_grid_writes_total{reason="fill"}` = 0 while ARB doesn't move through any trigger (real fills only).
- `bot_grid_stops_placed_total` ~16; `bot_grid_stops_cancelled_total` = 0 (no orphan cascade).

- [ ] **Step 7: Update `WORKING_ON.md`**

Edit `WORKING_ON.md`: move the "Cascading fill imbalance" bug to "Bugs resolvidos" section with note that it was a downstream symptom of the cloid mismatch. Add fix commit hash. Update timestamp.

```bash
# After editing WORKING_ON.md
git add WORKING_ON.md
git commit -m "docs: WORKING_ON post cloid-32bit deploy"
git push origin master
ssh -o StrictHostKeyChecking=no -i ~/.ssh/id_ed25519 root@104.248.44.6 'cd /opt/automoney && git pull --ff-only'
```

---

## Live-state probe `/tmp/sg.py` (for reference if missing on prod)

```python
import asyncio, sys
sys.path.insert(0, '/opt/automoney')
from dotenv import load_dotenv
load_dotenv('/opt/automoney/.env')
from exchanges.lighter import LighterAdapter
from config import Settings

async def main():
    cfg = Settings.from_env()
    a = LighterAdapter(
        url=cfg.lighter_url,
        account_index=cfg.lighter_account_index,
        api_private_key=cfg.lighter_api_private_key,
        api_key_index=cfg.lighter_api_key_index,
    )
    await a.connect()
    sym = 'ARB-USD'  # adjust if active op uses a different symbol
    pos = await a.get_position(sym)
    print('POSITION:', pos)
    orders = await a.get_open_orders(sym)
    print('ORDERS:', len(orders))
    for o in sorted(orders, key=lambda x: float(x.get('trigger_price',0) or 0)):
        print(' ', o)
    await a.disconnect()

asyncio.run(main())
```
