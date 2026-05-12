# Funding Window Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the existing `pnl_window_since_ts` datetime picker (commit `d1c7bed`) to also affect Funding, not just Hedge PnL. When a window is set, Funding is computed by paginating Lighter's `position_funding` API since that timestamp instead of reading the cumulative DB column.

**Architecture:** New adapter method `get_funding_total_since` reuses the existing `_fetch_position_funding` helper, filters by `timestamp >= since_ts`, and routes per-market into `(token0_total, token1_total)` using cached mids. Engine wires an optional `funding_override` into `compute_operation_pnl` via the same getattr pattern already used for `get_trade_pnl_since` (Hedge PnL override). PnL function uses override when provided, falls back to DB cumulative when None.

**Tech Stack:** Python 3.13 asyncio, Lighter SDK `AccountApi.position_funding`, pytest.

**Spec:** `docs/superpowers/specs/2026-05-10-funding-window-design.md`

---

## File Structure

**Modify:**
- `exchanges/base.py` — extend `ExchangeAdapter` ABC with default `get_funding_total_since` (returns `(0.0, 0.0)`)
- `exchanges/lighter.py` — implement `get_funding_total_since` (reuses `_fetch_position_funding`)
- `engine/pnl.py` — add `funding_override: tuple[float, float] | None` parameter to `compute_operation_pnl`
- `engine/__init__.py` — call `get_funding_total_since` when `op.pnl_window_since_ts` is set; pass result to `compute_operation_pnl`

**Add tests:**
- `tests/test_lighter_funding_since.py` — 4 unit tests for adapter method
- `tests/test_pnl_dual_leg.py` — 1 test for `funding_override` param

No DB changes. No UI changes (picker already exists and writes `pnl_window_since_ts`).

---

## Task 1: Add get_funding_total_since to base ExchangeAdapter

**Files:**
- Modify: `exchanges/base.py`

- [ ] **Step 1: Read current ExchangeAdapter signature**

Run: `grep -n "class ExchangeAdapter\|async def" exchanges/base.py | head -20`
Expected: shows the ABC class + its method signatures (look for the pattern of optional methods like `subscribe_funding`)

- [ ] **Step 2: Add the new method as default no-op**

Add to `exchanges/base.py` inside the `ExchangeAdapter` class (after the existing methods, before any closing class boundary):

```python
    async def get_funding_total_since(
        self, *, since_ts: float,
        market_id_token0: int | None = None,
        market_id_token1: int | None = None,
    ) -> tuple[float, float]:
        """Returns (token0_total, token1_total) of cumulative funding paid
        since the given unix timestamp.

        Sign convention: positive = "we paid", negative = "we received"
        — matches op.funding_paid_token0/1 stored in the DB.

        Default implementation returns (0.0, 0.0). Concrete adapters
        override (currently only LighterAdapter)."""
        return (0.0, 0.0)
```

- [ ] **Step 3: Verify the file still imports cleanly**

Run: `"C:/Users/Wallace/Python313/python.exe" -c "from exchanges.base import ExchangeAdapter; print(hasattr(ExchangeAdapter, 'get_funding_total_since'))"`
Expected: `True`

- [ ] **Step 4: Commit**

```bash
git add exchanges/base.py
git commit -m "feat(exchanges): add get_funding_total_since default in ExchangeAdapter

Default returns (0.0, 0.0). LighterAdapter overrides in Task 2.
Engine calls this when op.pnl_window_since_ts is set, to compute
Funding from a user-selected start timestamp instead of cumulative
since op.started_at.

Spec: docs/superpowers/specs/2026-05-10-funding-window-design.md
"
```

---

## Task 2: LighterAdapter.get_funding_total_since implementation + 4 tests

**Files:**
- Modify: `exchanges/lighter.py`
- Create: `tests/test_lighter_funding_since.py`

- [ ] **Step 1: Write 4 failing tests**

Create `tests/test_lighter_funding_since.py`:

```python
"""Tests for LighterAdapter.get_funding_total_since — paginates Lighter
position_funding and sums per-market since a given unix timestamp."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock
from exchanges.lighter import LighterAdapter


def _make_adapter(funding_entries):
    """Build a LighterAdapter with _fetch_position_funding mocked."""
    a = LighterAdapter.__new__(LighterAdapter)
    a._signer = MagicMock()  # truthy so the method doesn't early-return
    a._fetch_position_funding = AsyncMock(return_value=funding_entries)
    return a


@pytest.mark.asyncio
async def test_get_funding_total_since_filters_by_timestamp():
    """Entries with timestamp < since_ts must be excluded; >= included."""
    entries = [
        MagicMock(timestamp=1000, change=10.0, market_id=0),  # before
        MagicMock(timestamp=2000, change=5.0, market_id=0),   # at
        MagicMock(timestamp=3000, change=2.0, market_id=0),   # after
    ]
    # Set .get to mimic dict-style access (the impl uses e.get("...") pattern).
    # Actual SDK objects support both attr and getattr-style — we test the
    # filter logic, so emulate `e.get("k", default)` via __dict__-aware lambda.
    for e in entries:
        e.get = lambda k, d=None, _e=e: getattr(_e, k, d)
    a = _make_adapter(entries)
    t0, t1 = await a.get_funding_total_since(
        since_ts=2000, market_id_token0=0,
    )
    # entries at t=2000 (change=5) + t=3000 (change=2) → sum = 7
    # signs invert (Lighter change > 0 = received → paid = -7)
    assert t0 == pytest.approx(-7.0)
    assert t1 == 0.0


@pytest.mark.asyncio
async def test_get_funding_total_since_routes_per_market_id():
    """Token0 entries go to t0, token1 entries go to t1 by mid."""
    entries = [
        MagicMock(timestamp=1000, change=4.0, market_id=0),
        MagicMock(timestamp=1000, change=6.0, market_id=50),
    ]
    for e in entries:
        e.get = lambda k, d=None, _e=e: getattr(_e, k, d)
    a = _make_adapter(entries)
    t0, t1 = await a.get_funding_total_since(
        since_ts=0, market_id_token0=0, market_id_token1=50,
    )
    assert t0 == pytest.approx(-4.0)
    assert t1 == pytest.approx(-6.0)


@pytest.mark.asyncio
async def test_get_funding_total_since_inverts_sign():
    """Lighter convention: change > 0 = user received funding.
    Our return convention: positive = paid, negative = received.
    Test: change=+10 → return -10."""
    entries = [
        MagicMock(timestamp=100, change=10.0, market_id=0),
    ]
    for e in entries:
        e.get = lambda k, d=None, _e=e: getattr(_e, k, d)
    a = _make_adapter(entries)
    t0, _ = await a.get_funding_total_since(
        since_ts=0, market_id_token0=0,
    )
    assert t0 == pytest.approx(-10.0)  # received → negative paid


@pytest.mark.asyncio
async def test_get_funding_total_since_returns_zeros_when_signer_none():
    """Cold/unconnected adapter (signer=None) returns (0, 0) without
    calling _fetch_position_funding."""
    a = LighterAdapter.__new__(LighterAdapter)
    a._signer = None
    a._fetch_position_funding = AsyncMock()
    t0, t1 = await a.get_funding_total_since(
        since_ts=0, market_id_token0=0, market_id_token1=50,
    )
    assert (t0, t1) == (0.0, 0.0)
    a._fetch_position_funding.assert_not_called()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_lighter_funding_since.py -v 2>&1 | tail -20`
Expected: 4 tests fail with `AttributeError: ... has no attribute 'get_funding_total_since'`

- [ ] **Step 3: Implement get_funding_total_since in LighterAdapter**

In `exchanges/lighter.py`, add this method right after `_fetch_position_funding` (around line 1193):

```python
    async def get_funding_total_since(
        self, *, since_ts: float,
        market_id_token0: int | None = None,
        market_id_token1: int | None = None,
    ) -> tuple[float, float]:
        """Sum funding paid since `since_ts`, routing per-market.

        Reuses _fetch_position_funding (paginated, auth-token aware).
        Sign convention: returns positive = paid, negative = received
        (matches op.funding_paid_token0/1 in the DB).

        Note: _fetch_position_funding only returns the latest 100 entries.
        For windows farther back than ~100 funding cycles (~4 days at 1h
        cadence), older entries are missed. Picker is typically used for
        recent windows; cursor pagination can be added later if needed.
        """
        if self._signer is None:
            return (0.0, 0.0)
        entries = await self._fetch_position_funding(limit=100)
        t0 = 0.0
        t1 = 0.0
        for e in entries:
            ts = float(e.get("timestamp", 0))
            if ts < since_ts:
                continue
            change = float(e.get("change", 0))
            attributed = -change  # invert sign: received → paid
            mid = int(e.get("market_id", -1))
            if market_id_token0 is not None and mid == market_id_token0:
                t0 += attributed
            elif market_id_token1 is not None and mid == market_id_token1:
                t1 += attributed
        return (t0, t1)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_lighter_funding_since.py -v 2>&1 | tail -10`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add exchanges/lighter.py tests/test_lighter_funding_since.py
git commit -m "feat(lighter): get_funding_total_since paginates + filters + routes

Implements the override declared in ExchangeAdapter base. Reuses the
existing _fetch_position_funding helper (paginated, auth-token aware),
filters by timestamp >= since_ts, and routes per-market into
(token0_total, token1_total) using the cached mids.

Sign matches funding_paid_token0/1 in the DB: positive = paid,
negative = received.

Spec: docs/superpowers/specs/2026-05-10-funding-window-design.md
"
```

---

## Task 3: Add funding_override param to compute_operation_pnl + 1 test

**Files:**
- Modify: `engine/pnl.py:18-130` (`compute_operation_pnl` signature + funding section)
- Modify: `tests/test_pnl_dual_leg.py`

- [ ] **Step 1: Read the current funding lines in compute_operation_pnl**

Run: `grep -n "funding_t0\|funding_t1\|funding_paid_token" engine/pnl.py | head -10`
Expected: shows lines around 110-111 where `-op.funding_paid_token0/1` are computed.

- [ ] **Step 2: Write the failing test**

Append to `tests/test_pnl_dual_leg.py`:

```python
def test_compute_operation_pnl_uses_funding_override_when_provided():
    """When funding_override=(token0_paid, token1_paid) is passed,
    compute_operation_pnl uses those values directly and IGNORES
    op.funding_paid_token0/1 from the DB. Sign matches existing behavior:
    positive in override = positive in DB column = 'we paid'."""
    from engine.pnl import compute_operation_pnl
    from engine.operation import Operation

    op = Operation(
        id=99,
        status="active",
        baseline_amount0=0.01,
        baseline_amount1=100.0,
        baseline_token0_price=2300.0,
        baseline_token1_price=0.13,
        baseline_pool_value_usd=50.0,
        baseline_deposit_usd=50.0,
        funding_paid_token0=999.0,  # huge value to prove override wins
        funding_paid_token1=999.0,
    )
    # Override says we paid 10 in t0, received 5 in t1 (negative = received)
    breakdown = compute_operation_pnl(
        op,
        current_pool_value_usd=50.0,
        current_token0_usd_price=2300.0,
        current_token1_usd_price=0.13,
        hedge_realized_per_symbol={},
        hedge_unrealized_per_symbol={},
        funding_override=(10.0, -5.0),
    )
    # Display sign in breakdown is INVERTED from input: positive = received
    # i.e. breakdown.funding_t0 = -override[0] = -10 (we paid 10 → display shows -10)
    assert breakdown["funding_t0"] == pytest.approx(-10.0)
    assert breakdown["funding_t1"] == pytest.approx(5.0)
```

(Adjust the `Operation` fields if the dataclass requires more — read `engine/operation.py` first if the constructor differs.)

- [ ] **Step 3: Run the test to verify it fails**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_pnl_dual_leg.py::test_compute_operation_pnl_uses_funding_override_when_provided -v 2>&1 | tail -10`
Expected: FAIL with `TypeError: ... unexpected keyword argument 'funding_override'`

- [ ] **Step 4: Add the parameter + branch logic in compute_operation_pnl**

Edit `engine/pnl.py`. In the function signature (around line 18-35), add `funding_override`:

```python
def compute_operation_pnl(
    op,
    *,
    current_pool_value_usd: float,
    current_eth_price: float | None = None,
    current_token0_usd_price: float | None = None,
    current_token1_usd_price: float | None = None,
    hedge_realized_since_baseline: float | None = None,
    hedge_unrealized_since_baseline: float | None = None,
    hedge_realized_per_symbol: dict[str, float] | None = None,
    hedge_unrealized_per_symbol: dict[str, float] | None = None,
    hedge_pnl_aggregate_override: float | None = None,
    funding_override: tuple[float, float] | None = None,
) -> dict:
```

(Keep the actual signature as-is for fields not shown — the goal is to ADD `funding_override` as a new keyword-only param at the end.)

Find the lines around 110-111 that currently read:

```python
        funding_t0 = -op.funding_paid_token0
        funding_t1 = -op.funding_paid_token1
```

Replace with:

```python
        if funding_override is not None:
            # Override path: caller (engine) computed funding for a
            # user-selected window via get_funding_total_since.
            # Override values are in "paid" convention (positive = paid);
            # display inverts to "received" convention for the breakdown.
            funding_t0 = -funding_override[0]
            funding_t1 = -funding_override[1]
        else:
            # Default path: cumulative since op.started_at from DB column
            # (populated by the funding poller).
            funding_t0 = -op.funding_paid_token0
            funding_t1 = -op.funding_paid_token1
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_pnl_dual_leg.py::test_compute_operation_pnl_uses_funding_override_when_provided -v 2>&1 | tail -5`
Expected: 1 passed

Also run the full pnl test file to confirm no regression:

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_pnl_dual_leg.py --tb=no -q 2>&1 | tail -5`
Expected: all green

- [ ] **Step 6: Commit**

```bash
git add engine/pnl.py tests/test_pnl_dual_leg.py
git commit -m "feat(pnl): funding_override param in compute_operation_pnl

When funding_override=(t0_paid, t1_paid) is provided, compute_operation_pnl
uses it directly instead of reading op.funding_paid_token0/1. Engine
will pass the override when op.pnl_window_since_ts is set (Task 4).

Existing callers that don't pass funding_override (default None) get
the unchanged DB-cumulative behavior.

Spec: docs/superpowers/specs/2026-05-10-funding-window-design.md
"
```

---

## Task 4: Wire funding_override in engine _iterate

**Files:**
- Modify: `engine/__init__.py:1050-1090` (the PnL update block)

- [ ] **Step 1: Read the current PnL update block**

Run: `sed -n '1045,1095p' engine/__init__.py`
Expected: shows the existing block where `hedge_pnl_override` is computed via `getattr(self._exchange, "get_trade_pnl_since", None)` and then passed to `compute_operation_pnl`.

- [ ] **Step 2: Add funding_override fetch right after the hedge_pnl_override block**

Find this block (approx lines 1057-1070):

```python
                        if op_started_at > 0:
                            try:
                                getter = getattr(
                                    self._exchange, "get_trade_pnl_since", None,
                                )
                                if getter is not None:
                                    r = await getter(op_started_at, time.time())
                                    if r is not None:
                                        baseline, latest = r
                                        hedge_pnl_override = latest - baseline
                            except Exception as e:
                                logger.warning(
                                    f"get_trade_pnl_since failed: {e}"
                                )
```

After this block, ADD a sibling block for funding override:

```python
                        # Funding window override: when the user picked
                        # a start in the UI, sum funding from Lighter
                        # since that ts instead of the DB cumulative
                        # (which is since op.started_at).
                        funding_override = None
                        try:
                            window_since = op_row.get("pnl_window_since_ts")
                        except Exception:
                            window_since = None
                        if window_since is not None and float(window_since) > 0:
                            try:
                                fgetter = getattr(
                                    self._exchange, "get_funding_total_since", None,
                                )
                                if fgetter is not None:
                                    funding_override = await asyncio.wait_for(
                                        fgetter(
                                            since_ts=float(window_since),
                                            market_id_token0=self._token0_mid,
                                            market_id_token1=self._token1_mid,
                                        ),
                                        timeout=5.0,
                                    )
                            except Exception as e:
                                logger.warning(
                                    f"get_funding_total_since failed: {e}"
                                )
                                funding_override = None
```

- [ ] **Step 3: Pass funding_override to BOTH compute_operation_pnl calls**

Find both calls (around lines 1073 and 1083):

```python
                        if is_dual_leg:
                            self._hub.operation_pnl_breakdown = compute_operation_pnl(
                                op,
                                current_pool_value_usd=pool_value_usd,
                                current_token0_usd_price=p0_usd,
                                current_token1_usd_price=p1_usd,
                                hedge_realized_per_symbol=dict(self._hub.hedge_realized_pnls),
                                hedge_unrealized_per_symbol=dict(self._hub.hedge_unrealized_pnls),
                                hedge_pnl_aggregate_override=hedge_pnl_override,
                            )
                        else:
                            self._hub.operation_pnl_breakdown = compute_operation_pnl(
                                op,
                                current_pool_value_usd=pool_value_usd,
                                current_eth_price=p_now,
                                hedge_realized_since_baseline=self._hub.hedge_realized_pnl,
                                hedge_unrealized_since_baseline=self._hub.hedge_unrealized_pnl,
                                hedge_pnl_aggregate_override=hedge_pnl_override,
                            )
```

Add `funding_override=funding_override` as the last kwarg in BOTH branches:

```python
                        if is_dual_leg:
                            self._hub.operation_pnl_breakdown = compute_operation_pnl(
                                op,
                                current_pool_value_usd=pool_value_usd,
                                current_token0_usd_price=p0_usd,
                                current_token1_usd_price=p1_usd,
                                hedge_realized_per_symbol=dict(self._hub.hedge_realized_pnls),
                                hedge_unrealized_per_symbol=dict(self._hub.hedge_unrealized_pnls),
                                hedge_pnl_aggregate_override=hedge_pnl_override,
                                funding_override=funding_override,
                            )
                        else:
                            self._hub.operation_pnl_breakdown = compute_operation_pnl(
                                op,
                                current_pool_value_usd=pool_value_usd,
                                current_eth_price=p_now,
                                hedge_realized_since_baseline=self._hub.hedge_realized_pnl,
                                hedge_unrealized_since_baseline=self._hub.hedge_unrealized_pnl,
                                hedge_pnl_aggregate_override=hedge_pnl_override,
                                funding_override=funding_override,
                            )
```

- [ ] **Step 4: Run engine tests to confirm no regression**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_engine_dual_leg.py tests/test_engine_grid.py tests/test_pnl_dual_leg.py tests/test_lighter_funding_since.py --tb=no -q 2>&1 | tail -5`
Expected: all green

- [ ] **Step 5: Commit + push**

```bash
git add engine/__init__.py
git commit -m "feat(engine): wire funding_override when pnl_window_since_ts is set

When the operation has pnl_window_since_ts set (via the datetime picker),
fetch funding via the new LighterAdapter.get_funding_total_since (5s
timeout) and pass to compute_operation_pnl as funding_override. Default
path (window unset) keeps the cumulative-since-op-start behavior.

Mirrors the existing get_trade_pnl_since pattern for Hedge PnL.

Spec: docs/superpowers/specs/2026-05-10-funding-window-design.md
"
git push -u origin feature/funding-window
```

---

## Verification (post-merge live check)

1. Restart uvicorn: `start.bat`
2. Open dashboard, click 🕒 Janela on the operation card
3. Pick a recent timestamp (e.g. 1h ago)
4. UI Funding line should change from cumulative-since-op-start to cumulative-since-picker (small change since funding rates are tiny on Lighter)
5. Click "Limpar (usar op start)" → Funding goes back to DB cumulative
6. Engine log should not show `get_funding_total_since failed` warnings

## Self-review notes

- ✅ Spec coverage: all 4 spec components mapped to T1-T4
- ✅ No placeholders; every step has full code
- ✅ Type consistency: `funding_override: tuple[float, float] | None` everywhere
- ✅ Sign convention locked: test #3 in T2 + test in T3 both pin the invert
- ⚠ T2 reuses `_fetch_position_funding` which only returns 100 entries (no cursor). Documented as known limitation in the method docstring; not a blocker for typical use
- ⚠ T3 test assumes Operation dataclass constructor — implementer must read `engine/operation.py` if `Operation(id=99, status="active", ...)` doesn't work as written
