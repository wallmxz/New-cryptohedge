# Phantom Cloid Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `_safety_reconcile::missing` from treating phantom cloids (Lighter silent-rejections) as fills. Add post-verify to `_post_initial_grid` so phantoms are cleaned up at the source.

**Architecture:** `_local_grid` becomes a cache that gets reconciled against Lighter's `get_open_orders` (source-of-truth). Fill detection is moved entirely to position-delta in `_grid_event_loop` (the existing path). `_safety_reconcile` becomes pop-only for missing cloids.

**Tech Stack:** Python 3.12 (prod) / 3.13 (Windows dev), asyncio, pytest + pytest-asyncio.

**Spec:** `docs/superpowers/specs/2026-05-15-phantom-cloid-cleanup-design.md`

---

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `engine/__init__.py` | Modify line 1807-1813 (`_safety_reconcile`) | Replace missing→fill branch with pop-only |
| `engine/__init__.py` | Modify line 1554-1560 (`_post_initial_grid` tail) | Insert post-verify block before setting `_last_known_position` |
| `tests/test_engine_event_driven_grid.py` | Modify line 365-397 | Delete `test_safety_reconcile_steady_state_detects_missing_as_fill` (validates old behavior) |
| `tests/test_engine_event_driven_grid.py` | Append | Add 3 new tests: pop-only missing, log message, post-initial-grid phantom drop |

No new files. All changes localized to engine + its tests.

---

### Task 1: Replace existing test that validates the OLD behavior

**Files:**
- Modify: `tests/test_engine_event_driven_grid.py:366-397`

- [ ] **Step 1: Delete the obsolete test**

In `tests/test_engine_event_driven_grid.py`, locate the test starting at line 366:

```python
@pytest.mark.asyncio
async def test_safety_reconcile_steady_state_detects_missing_as_fill():
    """Steady-state: cloid in local_grid not on Lighter → treat as filled,
    re-trigger _apply_fills_to_grid via the missing path."""
    ...
    total_calls = (
        engine._exchange.cancel_stop_order.call_count
        + engine._exchange.place_stop_market.call_count
    )
    assert total_calls >= 1
```

Delete the entire function (lines 365-397, from the blank line above the `@pytest.mark.asyncio` decorator through the `assert total_calls >= 1` line, AND the blank line below it). Be careful not to delete the next test (`test_grid_event_loop_iter_no_position_change_no_writes`).

- [ ] **Step 2: Verify the suite passes without that test**

Run: `python -m pytest tests/test_engine_event_driven_grid.py -v 2>&1 | tail -15`

Expected: 19 tests pass (was 20; we removed 1). No new failures.

- [ ] **Step 3: Commit**

```bash
git add tests/test_engine_event_driven_grid.py
git commit -m "test(engine): remove test validating phantom-as-fill behavior

That test asserted _safety_reconcile invokes _apply_fills_to_grid for
missing cloids. New design (spec 2026-05-15-phantom-cloid-cleanup):
missing cloids are phantoms or fills-already-caught-by-event-loop; no
re-trigger of fill processing from safety_reconcile. Replaced by new
'pop-only' tests in subsequent tasks."
```

---

### Task 2: Failing test — `_safety_reconcile::missing` pops phantoms without calling `_apply_fills_to_grid`

**Files:**
- Modify: `tests/test_engine_event_driven_grid.py` (append at end)

- [ ] **Step 1: Add the failing test**

Append at the end of `tests/test_engine_event_driven_grid.py`:

```python
@pytest.mark.asyncio
async def test_safety_reconcile_missing_drops_phantoms_without_calling_apply_fills():
    """Spec D3: missing cloids (in local_grid but not on Lighter) are treated
    as phantoms (silent-rejections from Lighter) or fills-already-caught by
    _grid_event_loop. Either way, _safety_reconcile must NOT call
    _apply_fills_to_grid (which would post fake replacements). Just pop them
    from _local_grid.

    Spec: docs/superpowers/specs/2026-05-15-phantom-cloid-cleanup-design.md
    """
    engine = _make_engine()
    engine._exchange = MagicMock()
    # Lighter has only 2 orders live (cloid 200, 300); local has 5 (100, 101, 200, 201, 300).
    # So 100, 101, 201 are "missing" (phantoms or already-processed fills).
    engine._exchange.get_open_orders = AsyncMock(return_value=[
        {"cloid": "200", "side": "buy", "trigger_price": 0.130, "size": 3.0, "order_index": 7200},
        {"cloid": "300", "side": "sell", "trigger_price": 0.135, "size": 3.0, "order_index": 7300},
    ])
    engine._exchange.cancel_stop_order = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
    # Patch _apply_fills_to_grid as an AsyncMock so we can detect any invocation.
    engine._apply_fills_to_grid = AsyncMock()

    engine._local_grid = {
        100: GridStop(100, "sell", 0.140, 3.0),
        101: GridStop(101, "sell", 0.142, 3.0),
        200: GridStop(200, "buy", 0.130, 3.0),
        201: GridStop(201, "buy", 0.128, 3.0),
        300: GridStop(300, "sell", 0.135, 3.0),
    }

    await engine._safety_reconcile()

    # The new behavior: missing cloids are popped, _apply_fills_to_grid not called.
    engine._apply_fills_to_grid.assert_not_called()
    assert set(engine._local_grid.keys()) == {200, 300}, (
        f"expected only live cloids in local_grid, got {set(engine._local_grid.keys())}"
    )
    # Also: no extra place_stop_market or cancel_stop_order from the missing branch.
    # (Orphan branch may still cancel — but there are no orphans here.)
    engine._exchange.place_stop_market.assert_not_called()
```

- [ ] **Step 2: Run, verify FAIL**

Run: `python -m pytest tests/test_engine_event_driven_grid.py::test_safety_reconcile_missing_drops_phantoms_without_calling_apply_fills -v`

Expected: FAIL — `_apply_fills_to_grid` IS called by the current implementation (asserts `assert_not_called` fails).

---

### Task 3: Implement `_safety_reconcile::missing` pop-only

**Files:**
- Modify: `engine/__init__.py:1807-1813`

- [ ] **Step 1: Replace the missing branch**

Find in `engine/__init__.py` around lines 1807-1813 (use Read to confirm exact contents before Edit):

```python
        # Missing on Lighter (in local but not live) → assumed filled
        missing = local_cloids - live_cloids
        if missing:
            step = self._estimate_grid_step()
            await self._apply_fills_to_grid(
                filled_cloids=missing, step=step, live_by_cloid=live_by_cloid,
            )
```

Replace with:

```python
        # Missing on Lighter (in local but not live) → either phantoms from
        # Lighter silent-rejections OR real fills already caught by
        # _grid_event_loop via position-delta. Don't process as fills here;
        # just drop from local_grid. Spec D3 in
        # docs/superpowers/specs/2026-05-15-phantom-cloid-cleanup-design.md.
        missing = local_cloids - live_cloids
        for cloid in missing:
            self._local_grid.pop(cloid, None)
        if missing:
            logger.info(
                f"_safety_reconcile dropped {len(missing)} missing cloid(s) from "
                f"local_grid (phantoms or already-processed fills)"
            )
```

- [ ] **Step 2: Verify the test passes**

Run: `python -m pytest tests/test_engine_event_driven_grid.py::test_safety_reconcile_missing_drops_phantoms_without_calling_apply_fills -v`

Expected: PASS.

- [ ] **Step 3: Run full file to confirm no regression**

Run: `python -m pytest tests/test_engine_event_driven_grid.py -v 2>&1 | tail -25`

Expected: all tests pass (the orphan-cancel test, bootstrap test, event-loop tests all still green).

- [ ] **Step 4: Commit**

```bash
git add tests/test_engine_event_driven_grid.py engine/__init__.py
git commit -m "fix(engine): _safety_reconcile::missing pop-only (no fake fills)

Previously _safety_reconcile treated every local cloid not on Lighter
as a fill, calling _apply_fills_to_grid which posted replacements.
For phantom cloids (Lighter silent-rejections), this created fake-fill
cascades that drained the grid every 90s. Observed in prod 2026-05-15
after the cloid 32-bit fix: buys shifted upward each cycle while
sells stayed frozen.

New behavior: just pop missing cloids from local_grid. Real fills are
authoritative-detected by _grid_event_loop via position-delta (a fill
changes the position; a phantom doesn't).

Spec: docs/superpowers/specs/2026-05-15-phantom-cloid-cleanup-design.md"
```

---

### Task 4: Failing test — `_post_initial_grid` drops phantoms after post-verify

**Files:**
- Modify: `tests/test_engine_event_driven_grid.py` (append at end)

- [ ] **Step 1: Inspect existing test infrastructure for _post_initial_grid**

Run: `grep -n "_post_initial_grid\|compute_grid_from_pool_ticks" tests/test_engine_event_driven_grid.py tests/test_engine_grid.py tests/test_engine_maintain_grid.py 2>&1 | head -20`

You may or may not find existing fixtures. The test below uses `_post_initial_grid` with mock dependencies; if there's a helper, prefer it. Otherwise the test stands alone.

- [ ] **Step 2: Add the failing test**

Append at the end of `tests/test_engine_event_driven_grid.py`:

```python
@pytest.mark.asyncio
async def test_post_initial_grid_drops_phantoms_after_verify():
    """Spec D4: after _post_initial_grid posts N stops, it verifies via
    get_open_orders 500ms later and drops cloids that didn't land
    (Lighter silent-rejections). Without this, phantoms sit in _local_grid
    and trigger fake-fill processing in _safety_reconcile or _grid_event_loop.

    Spec: docs/superpowers/specs/2026-05-15-phantom-cloid-cleanup-design.md D4
    """
    import asyncio as _asyncio
    from unittest.mock import patch

    engine = _make_engine()
    engine._exchange = MagicMock()
    # All 5 place_stop_market calls "succeed" (SDK doesn't raise).
    engine._exchange.place_stop_market = AsyncMock()
    engine._exchange.get_position = AsyncMock(return_value=None)
    # But Lighter actually persisted only 2 of them (cloids 9101 and 9105).
    engine._exchange.get_open_orders = AsyncMock(return_value=[
        {"cloid": "9101", "side": "sell", "trigger_price": 0.140, "size": 3.0, "order_index": 1},
        {"cloid": "9105", "side": "buy", "trigger_price": 0.130, "size": 3.0, "order_index": 5},
    ])
    engine._db = MagicMock()
    engine._db.insert_grid_order = AsyncMock()

    # Stub _next_cloid_for_leg to deterministic values matching the live response.
    cloid_seq = iter([9101, 9102, 9103, 9104, 9105])
    engine._next_cloid_for_leg = MagicMock(side_effect=lambda sym: next(cloid_seq))

    # Stub compute_grid_from_pool_ticks to return a simple 3 sells + 2 buys grid.
    class FakeLevel:
        def __init__(self, side, price, size):
            self.side = side
            self.price = price
            self.size = size

    fake_grid = [
        FakeLevel("sell", 0.140, 3.0),
        FakeLevel("sell", 0.142, 3.0),
        FakeLevel("sell", 0.144, 3.0),
        FakeLevel("buy", 0.130, 3.0),
        FakeLevel("buy", 0.128, 3.0),
    ]

    # Mock cache + beefy_pos sufficient for _post_initial_grid to run.
    cache = MagicMock()
    cache.L_main = 1e18
    cache.tick_lower_main = -100
    cache.tick_upper_main = 100
    beefy_pos = MagicMock(share=1.0)
    engine._settings.token0_decimals = 18
    engine._settings.token1_decimals = 6
    engine._settings.uniswap_v3_pool_fee = 500
    engine._settings.hedge_ratio = 1.0
    engine._settings.dydx_symbol_token0 = "ARB-USD"
    engine._settings.max_open_orders = 10
    engine._settings.grid_anticipation_buffer = 0.0
    engine._hub.hedge_ratio = 1.0
    engine._hub.current_operation_id = 42
    engine._hub.grid_health_metrics = {}

    # Patch the grid-math helper to return our fake levels.
    with patch("engine.curve.compute_grid_from_pool_ticks", return_value=fake_grid):
        # Patch asyncio.sleep so the test doesn't actually wait 500ms.
        with patch("engine.asyncio.sleep", new=AsyncMock()):
            await engine._post_initial_grid(beefy_pos=beefy_pos, p_now=0.135, cache=cache)

    # After post-verify: _local_grid should only contain 9101 and 9105 (the survivors).
    # Phantoms 9102, 9103, 9104 should have been dropped.
    assert set(engine._local_grid.keys()) == {9101, 9105}, (
        f"expected only live cloids in local_grid after verify, got {set(engine._local_grid.keys())}"
    )
```

- [ ] **Step 3: Verify imports in test file**

Check the top of `tests/test_engine_event_driven_grid.py`. If `patch` is not in the imports, the test will fail at collection. Confirm `from unittest.mock import MagicMock, AsyncMock` is there. If `patch` is missing, the test imports it locally via `from unittest.mock import patch`; that should work.

- [ ] **Step 4: Run, verify FAIL**

Run: `python -m pytest tests/test_engine_event_driven_grid.py::test_post_initial_grid_drops_phantoms_after_verify -v`

Expected: FAIL — `_post_initial_grid` currently adds ALL 5 cloids to `_local_grid` (no verify step yet).

---

### Task 5: Implement `_post_initial_grid` post-verify

**Files:**
- Modify: `engine/__init__.py` around line 1553-1560 (just before setting `_last_known_position`)

- [ ] **Step 1: Read current state**

Use Read to confirm the area. The current tail of `_post_initial_grid` is:

```python
        metrics.grid_levels_active.set(posted_count)
        metrics.grid_rebuild_total.labels(reason="initial").inc()
        gh = self._hub.grid_health_metrics
        gh["levels_active"] = posted_count
        gh["last_rebuild_reason"] = "initial"
        gh["last_rebuild_ts"] = time.time()
        gh["stops_placed_total"] = gh.get("stops_placed_total", 0) + posted_count
        gh["rebuilds_total"] = gh.get("rebuilds_total", 0) + 1

        # Set last_known_position to current so the event loop doesn't misinterpret
        # the initial post as a fill event.
        try:
            self._last_known_position = await self._exchange.get_position(symbol)
        except Exception:
            pass
```

- [ ] **Step 2: Insert the verify block**

Insert BEFORE the `# Set last_known_position` block (after the `gh["rebuilds_total"]` line) the following:

```python

        # Post-verify (spec D4): some place_stop_market calls may have been
        # silently rejected by Lighter (trigger past market, etc) despite the
        # SDK returning err=None. Wait briefly for Lighter to settle, then
        # reconcile _local_grid against live and drop cloids that didn't land.
        # Without this, phantoms cause fake-fill cascades in _safety_reconcile
        # / _grid_event_loop. Best-effort: a verify failure logs warning and
        # proceeds (_safety_reconcile next cycle pops phantoms via D3).
        await asyncio.sleep(0.5)
        try:
            live = await self._exchange.get_open_orders(symbol)
            live_cloids = {int(o["cloid"]) for o in live}
            phantoms = set(self._local_grid.keys()) - live_cloids
            if phantoms:
                for c in phantoms:
                    self._local_grid.pop(c, None)
                logger.warning(
                    f"_post_initial_grid dropped {len(phantoms)} phantom "
                    f"cloid(s) (Lighter silent-rejected): {sorted(phantoms)}"
                )
                # Adjust the levels_active metric to reflect reality.
                metrics.grid_levels_active.set(len(self._local_grid))
                gh["levels_active"] = len(self._local_grid)
        except Exception as e:
            logger.warning(
                f"_post_initial_grid verify failed ({e!r}); proceeding. "
                f"_safety_reconcile next cycle will clean up any phantoms."
            )

```

(The leading blank line keeps separation from the metrics block. The trailing blank line keeps separation from `# Set last_known_position`.)

Also, at the top of `engine/__init__.py`, confirm `import asyncio` is already present. It should be — many tasks use it. If not, add it.

- [ ] **Step 3: Run the failing test, confirm PASS**

Run: `python -m pytest tests/test_engine_event_driven_grid.py::test_post_initial_grid_drops_phantoms_after_verify -v`

Expected: PASS.

- [ ] **Step 4: Run full file**

Run: `python -m pytest tests/test_engine_event_driven_grid.py -v 2>&1 | tail -25`

Expected: all tests pass (we now have 21 tests in this file).

- [ ] **Step 5: Commit**

```bash
git add tests/test_engine_event_driven_grid.py engine/__init__.py
git commit -m "feat(engine): _post_initial_grid verify-after-batch drops phantoms

After place_stop_market calls return, wait 500ms and query
get_open_orders. Drop cloids from _local_grid that didn't land
(Lighter silent-rejected for triggers past market etc.). Without
this, phantoms sit in _local_grid and trigger fake-fill processing
elsewhere.

Spec: docs/superpowers/specs/2026-05-15-phantom-cloid-cleanup-design.md D4"
```

---

### Task 6: Full suite + deploy

**Files:**
- N/A (verification + deploy)

- [ ] **Step 1: Run full test suite**

Run: `python -m pytest tests/test_engine_cloid.py tests/test_engine_event_driven_grid.py tests/test_engine_grid.py tests/test_engine_maintain_grid.py tests/test_engine_dual_leg.py tests/test_lighter_stop_orders.py tests/test_health_engine.py -v 2>&1 | tail -25`

Expected: all green. If anything failed, diagnose and fix (likely the existing pollution issue in test_config.py, NOT relevant here).

- [ ] **Step 2: Push branch + push to master**

```bash
git push origin claude/clever-liskov-0bbd80
git push origin claude/clever-liskov-0bbd80:master
```

(Same fast-forward pattern used in the previous fix since master worktree is locked.)

- [ ] **Step 3: Pull on prod + restart**

```bash
ssh -o StrictHostKeyChecking=no -i ~/.ssh/id_ed25519 root@104.248.44.6 'cd /opt/automoney && git fetch && git checkout master && git pull --ff-only && git log --oneline -5 && systemctl restart automoney && sleep 5 && systemctl is-active automoney'
```

Expected output: `active`.

- [ ] **Step 4: Watch logs for 90s (first safety_reconcile tick)**

```bash
ssh -o StrictHostKeyChecking=no -i ~/.ssh/id_ed25519 root@104.248.44.6 "sleep 100 && tail -200 /var/log/automoney.log | grep -E '(initial post|dropped|phantom|safety_reconcile|orphan|event-driven)' | tail -40"
```

Expected:
- 13-16 `initial post` log lines (8 sells + 8 buys, minus the safety clamp drops).
- One `_post_initial_grid dropped N phantom cloid(s)` log line (with N expected = 1-3 based on pre-fix observation).
- ZERO `_safety_reconcile cancelled orphan` for the fresh cloids in the first 90s. (Some early `event-driven cancel skipped` is OK — startup race.)

- [ ] **Step 5: Check Lighter live state**

```bash
ssh -o StrictHostKeyChecking=no -i ~/.ssh/id_ed25519 root@104.248.44.6 '/opt/automoney/venv/bin/python /tmp/sg.py'
```

Expected: 12-16 stops, balanced around market (e.g., 6-8 sells / 6-8 buys). All cloids in the `0xA00000XX..0xA00000FF` range (32-bit, from this run).

- [ ] **Step 6: Verify grid stays stable for 5+ min**

```bash
ssh -o StrictHostKeyChecking=no -i ~/.ssh/id_ed25519 root@104.248.44.6 'A=$(curl -s http://127.0.0.1:8000/metrics | grep "^bot_grid_writes_total{reason=\"fill\"}" | awk "{print \$2}"); echo "snap1 fill=$A"; sleep 300; B=$(curl -s http://127.0.0.1:8000/metrics | grep "^bot_grid_writes_total{reason=\"fill\"}" | awk "{print \$2}"); echo "snap2 fill=$B (delta=$(echo \"$B-$A\" | bc))"'
```

Expected: delta ≤ 2-3 over 5 min (vs pre-fix: ~25 in 5 min). If position changed during the window due to real fills, delta could be higher — interpret with context.

- [ ] **Step 7: Re-check Lighter state vs initial snapshot**

```bash
ssh -o StrictHostKeyChecking=no -i ~/.ssh/id_ed25519 root@104.248.44.6 '/opt/automoney/venv/bin/python /tmp/sg.py'
```

Expected: same cloids as step 5 (or close to it — only changes if real fills happened). NO drift where buys shift upward while sells stay frozen.

- [ ] **Step 8: Update WORKING_ON.md**

Add a new section "### Noite — Phantom cloid cleanup (fix #2)" referencing the spec/plan/commits. Move related items from "Bugs remanescentes" to "Bugs resolvidos na sessão". Commit + push + pull on prod.

```bash
git add WORKING_ON.md
git commit -m "docs: WORKING_ON post phantom-cloid-cleanup deploy"
git push origin claude/clever-liskov-0bbd80
git push origin claude/clever-liskov-0bbd80:master
ssh -o StrictHostKeyChecking=no -i ~/.ssh/id_ed25519 root@104.248.44.6 'cd /opt/automoney && git pull --ff-only'
```

---

## Failure-mode recovery

If step 6 shows the grid is STILL drifting (cascade pattern persists), the root-cause analysis missed something. Stop, gather data:

1. Check `bot_grid_writes_total{reason}` breakdown:
   - If `safety` grows: D3 fix not working. Check the `_safety_reconcile` code change actually deployed.
   - If `fill` grows without position changing: `_grid_event_loop` is firing fake fills. Approach C insufficient; need Approach A (position-delta fill detection).
2. Check `_post_initial_grid dropped N` log: if N=0 but grid still vanishing, phantoms aren't the issue — different cascade source.

Don't auto-rollback. Stop the bot via `systemctl stop automoney`, gather logs/metrics, and escalate.
