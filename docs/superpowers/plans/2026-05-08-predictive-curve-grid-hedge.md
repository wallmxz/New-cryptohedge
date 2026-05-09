# Predictive Curve-Grid Hedge Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the per-iter drift computation in the engine with a pre-computed grid keyed on Uniswap pool ratio `p`. When `p` crosses a level, fire orders on both legs simultaneously at the current Lighter book bid/ask for the exact V3 amount delta. Reactive engine remains as fallback for warmup, RPC failures, empty book, and grid-stale windows.

**Architecture:** Pure-functional `LevelGrid` (dataclass + math helpers) in a new `engine/predictive_grid.py` module. Engine gains state slots (`_grid`, `_last_level_idx`, `_last_grid_check_at`), three async methods (`_refresh_grid`, `_iterate_predictive`, `_fire_predictive_leg`), and one rewritten method (`_iterate`). Hard guard via `fired_predictive` flag prevents double-fire with reactive in the same iter. New `PredictiveUnavailable` exception triggers fallback. New `StateHub.predictive_status` field surfaces mode in UI.

**Tech Stack:** Python 3.13, asyncio, web3.py (already in use for Uniswap slot0 + Beefy positionMain), Lighter SDK (already wired for `_ws_book_top`), pytest-asyncio.

---

## File Structure

| File | Responsibility |
|---|---|
| `engine/predictive_grid.py` (CREATE) | `LevelGrid` dataclass, `build_grid()`, `find_level_idx()`, `compute_deltas()`. Pure functions over math + state — no I/O. |
| `engine/__init__.py` (MODIFY) | Slots `_grid`, `_last_level_idx`, `_last_grid_check_at`, `_GRID_CHECK_INTERVAL_S`. New methods `_grid_stale`, `_refresh_grid`, `_iterate_predictive`, `_fire_predictive_leg`. Rewrite `_iterate()` for predictive-primary + reactive-fallback. Define `PredictiveUnavailable` exception (top of file). |
| `state.py` (MODIFY) | Add `predictive_status: str = "idle"` field to `StateHub`. |
| `tests/test_predictive_grid.py` (CREATE) | Unit tests for grid construction + level mapping + delta computation (T1, T2). |
| `tests/test_predictive_engine.py` (CREATE) | Integration tests for `_iterate_predictive`, `_fire_predictive_leg`, fallback behavior (T5, T6, T7). |
| `tests/test_predictive_grid_refresh.py` (CREATE) | Re-grid polling behavior (T4). |

---

### Task 1: `LevelGrid` dataclass + level mapping + delta computation

**Files:**
- Create: `engine/predictive_grid.py`
- Test: `tests/test_predictive_grid.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_predictive_grid.py`:

```python
"""Pure-function unit tests for the predictive grid module.

Math reference: V3 LP formulas
  amount0(p) = L × (1/√p − 1/√p_b)
  amount1(p) = L × (√p − √p_a)
"""
import bisect
import math
import pytest

from engine.predictive_grid import (
    LevelGrid, find_level_idx, compute_deltas,
)


def _grid_fixture():
    """Fixture grid: 5 levels in [p_a=1.0, p_b=4.0], L=1.0.
    Values computed by hand from V3 formulas — hardcoded for clarity.
    """
    p_a, p_b, L = 1.0, 4.0, 1.0
    p_levels = [1.0, 1.5, 2.0, 3.0, 4.0]
    # amount0 = L × (1/√p − 1/√p_b) = 1/√p − 0.5
    amount0_at = [1.0 - 0.5, 1/math.sqrt(1.5) - 0.5, 1/math.sqrt(2) - 0.5, 1/math.sqrt(3) - 0.5, 0.0]
    # amount1 = L × (√p − √p_a) = √p − 1
    amount1_at = [0.0, math.sqrt(1.5) - 1, math.sqrt(2) - 1, math.sqrt(3) - 1, 1.0]
    return LevelGrid(
        p_a=p_a, p_b=p_b, L=L,
        p_levels=p_levels,
        amount0_at=amount0_at,
        amount1_at=amount1_at,
        tick_lower=0, tick_upper=13863,
    )


def test_find_level_idx_below_p_a_returns_zero():
    """OOR low: p_now < p_a → idx 0 (edge: full token0)."""
    grid = _grid_fixture()
    assert find_level_idx(grid, p_now=0.5) == 0
    assert find_level_idx(grid, p_now=1.0) == 0  # exact p_a → idx 0


def test_find_level_idx_above_p_b_returns_last():
    """OOR high: p_now > p_b → idx len-1 (edge: full token1)."""
    grid = _grid_fixture()
    assert find_level_idx(grid, p_now=10.0) == 4
    assert find_level_idx(grid, p_now=4.0) == 4  # exact p_b → idx len-1


def test_find_level_idx_in_range_uses_bisect():
    """In-range p maps to the level with the largest p_levels[k] ≤ p_now."""
    grid = _grid_fixture()
    assert find_level_idx(grid, p_now=1.7) == 1   # between 1.5 and 2.0
    assert find_level_idx(grid, p_now=2.0) == 2   # exact level
    assert find_level_idx(grid, p_now=2.5) == 2   # between 2.0 and 3.0
    assert find_level_idx(grid, p_now=3.5) == 3


def test_compute_deltas_handles_multi_level_jump():
    """Multi-level jump: delta = amount[new] - amount[old], NOT sum of intermediates."""
    grid = _grid_fixture()
    # Jump from idx=0 (full WETH, no ARB) to idx=4 (no WETH, full ARB)
    d0, d1 = compute_deltas(grid, old_idx=0, new_idx=4)
    assert d0 == pytest.approx(0.0 - 0.5)   # amount0 went from 0.5 to 0 → -0.5
    assert d1 == pytest.approx(1.0 - 0.0)   # amount1 went from 0 to 1.0 → +1.0


def test_compute_deltas_same_idx_returns_zero():
    """No level change → no delta."""
    grid = _grid_fixture()
    d0, d1 = compute_deltas(grid, old_idx=2, new_idx=2)
    assert d0 == 0.0
    assert d1 == 0.0


def test_compute_deltas_negative_direction():
    """Backward level transition (price retreated)."""
    grid = _grid_fixture()
    # Going back idx=3 → idx=1
    d0, d1 = compute_deltas(grid, old_idx=3, new_idx=1)
    expected_d0 = (1/math.sqrt(1.5) - 0.5) - (1/math.sqrt(3) - 0.5)
    expected_d1 = (math.sqrt(1.5) - 1) - (math.sqrt(3) - 1)
    assert d0 == pytest.approx(expected_d0)
    assert d1 == pytest.approx(expected_d1)
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_predictive_grid.py -v`
Expected: FAIL — `engine.predictive_grid` module doesn't exist.

- [ ] **Step 3: Implement the module**

Create `engine/predictive_grid.py`:

```python
"""Predictive curve-grid hedge — pure-functional grid module.

The engine pre-computes a `LevelGrid` from the Beefy CLM v2 strategy's
current tick range. Each level k corresponds to a pool ratio p_levels[k]
and the V3 amounts (amount0_at[k], amount1_at[k]) the LP would hold at
that ratio. As the Uniswap pool's currentTick moves, the engine maps p_now
to a level idx via bisect and fires hedge orders for the per-leg amount
delta between the previous idx and the new one.

Spec: docs/superpowers/specs/2026-05-08-predictive-curve-grid-hedge-design.md
"""
from __future__ import annotations

import bisect
from dataclasses import dataclass


@dataclass
class LevelGrid:
    """Pre-computed level grid keyed on raw pool ratio p = token1/token0.

    Levels span [p_a, p_b] (the Beefy strategy's tick range). Outside the
    range, find_level_idx returns the nearest edge (0 or len-1), and the
    edge level's amounts are the OOR-clamped V3 values.
    """
    p_a: float                # raw price at lower bound (p_a = 1.0001^tick_lower)
    p_b: float                # raw price at upper bound
    L: float                  # V3 liquidity (compute_l_from_value)
    p_levels: list[float]     # sorted ascending [p_a, p_1, p_2, ..., p_b]
    amount0_at: list[float]   # token0 amount at each level
    amount1_at: list[float]   # token1 amount at each level
    tick_lower: int           # source-of-truth tick range from Beefy positionMain
    tick_upper: int


def find_level_idx(grid: LevelGrid, p_now: float) -> int:
    """Returns idx k such that p_levels[k] ≤ p_now < p_levels[k+1].

    OOR clamping:
    - p_now ≤ p_a → 0 (edge level, full token0)
    - p_now ≥ p_b → len(p_levels) - 1 (edge level, full token1)

    O(log N) via bisect.
    """
    if p_now <= grid.p_levels[0]:
        return 0
    if p_now >= grid.p_levels[-1]:
        return len(grid.p_levels) - 1
    return bisect.bisect_right(grid.p_levels, p_now) - 1


def compute_deltas(
    grid: LevelGrid, old_idx: int, new_idx: int,
) -> tuple[float, float]:
    """Returns (delta_amount0, delta_amount1) for transition old_idx → new_idx.

    Positive delta = LP gained that token = need to short MORE on perp.
    Negative delta = LP lost that token = close some short (BUY on perp).

    Multi-level jumps use direct endpoint diff, NOT sum of intermediates
    (otherwise a single big move would mass-fire intermediate levels).
    """
    return (
        grid.amount0_at[new_idx] - grid.amount0_at[old_idx],
        grid.amount1_at[new_idx] - grid.amount1_at[old_idx],
    )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_predictive_grid.py -v`
Expected: 6/6 PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/predictive_grid.py tests/test_predictive_grid.py
git commit -m "feat(predictive): LevelGrid dataclass + find_level_idx + compute_deltas

Per spec 2026-05-08-predictive-curve-grid-hedge-design § Components.
Pure-functional module — no I/O, no engine dependencies. find_level_idx
uses bisect (O log N), clamps to edge levels for OOR. compute_deltas
takes endpoint diff (not sum of intermediates) so multi-level jumps
fire one order per leg, not N.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: `build_grid()` — adaptive spacing by $0.50 leg notional

**Files:**
- Modify: `engine/predictive_grid.py` (add `build_grid` function + `MIN_LEG_NOTIONAL_USD` constant)
- Test: `tests/test_predictive_grid.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_predictive_grid.py`:

```python
from engine.predictive_grid import build_grid


def test_build_grid_endpoints_are_p_a_and_p_b():
    """First level == p_a, last level == p_b."""
    grid = build_grid(
        tick_lower=0, tick_upper=13863,
        L=1.0, p0_usd=1.0, p1_usd=1.0,
        min_leg_notional_usd=0.50,
    )
    assert grid.p_levels[0] == pytest.approx(1.0)            # 1.0001^0
    assert grid.p_levels[-1] == pytest.approx(math.exp(13863 * math.log(1.0001)))


def test_build_grid_amounts_match_v3_formula_at_endpoints():
    """amount0 at p_a = L × (1/√p_a − 1/√p_b). amount1 at p_b = L × (√p_b − √p_a)."""
    L = 1.0
    grid = build_grid(
        tick_lower=0, tick_upper=13863,
        L=L, p0_usd=1.0, p1_usd=1.0,
        min_leg_notional_usd=0.50,
    )
    p_a, p_b = grid.p_a, grid.p_b
    # amount0 at p_a (level 0)
    expected_a0 = L * (1/math.sqrt(p_a) - 1/math.sqrt(p_b))
    assert grid.amount0_at[0] == pytest.approx(expected_a0)
    # amount1 at p_b (last level)
    expected_b1 = L * (math.sqrt(p_b) - math.sqrt(p_a))
    assert grid.amount1_at[-1] == pytest.approx(expected_b1)
    # amount0 at p_b = 0; amount1 at p_a = 0
    assert grid.amount0_at[-1] == pytest.approx(0.0)
    assert grid.amount1_at[0] == pytest.approx(0.0)


def test_build_grid_levels_spaced_by_dollar_floor():
    """Each adjacent level pair must produce ≥$0.50 notional in at least one leg."""
    L = 100.0  # bigger LP for finer granularity in test
    p0_usd, p1_usd = 2300.0, 0.13  # ETH and ARB approx prices
    grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=L, p0_usd=p0_usd, p1_usd=p1_usd,
        min_leg_notional_usd=0.50,
    )
    for k in range(len(grid.p_levels) - 1):
        d0 = grid.amount0_at[k+1] - grid.amount0_at[k]
        d1 = grid.amount1_at[k+1] - grid.amount1_at[k]
        notional = max(abs(d0) * p0_usd, abs(d1) * p1_usd)
        assert notional >= 0.50, (
            f"level {k}→{k+1}: notional={notional:.4f} below floor 0.50"
        )


def test_build_grid_tick_range_stored():
    """Ticks are stored on the grid for re-grid comparison."""
    grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=1.0, p0_usd=2300.0, p1_usd=0.13,
        min_leg_notional_usd=0.50,
    )
    assert grid.tick_lower == -81121
    assert grid.tick_upper == -76012
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_predictive_grid.py::test_build_grid_endpoints_are_p_a_and_p_b tests/test_predictive_grid.py::test_build_grid_amounts_match_v3_formula_at_endpoints tests/test_predictive_grid.py::test_build_grid_levels_spaced_by_dollar_floor tests/test_predictive_grid.py::test_build_grid_tick_range_stored -v`
Expected: FAIL — `build_grid` doesn't exist.

- [ ] **Step 3: Implement `build_grid`**

Append to `engine/predictive_grid.py`:

```python
import math


# Default level granularity: each adjacent level pair must move ≥$0.50
# in at least one leg's notional. Adaptive: denser where one leg moves
# slowly per Δp, sparser where it moves fast.
MIN_LEG_NOTIONAL_USD = 0.50

# Hard cap on level count — protect against pathological tick ranges
# that would generate thousands of levels (RPC + memory waste). 500
# is plenty for any reasonable Beefy CLM v2 range.
MAX_LEVELS = 500


def _amount0_at(L: float, p: float, p_b: float) -> float:
    """V3 token0 amount. Clamped 0 above p_b (single-asset edge)."""
    if p >= p_b:
        return 0.0
    return L * (1.0 / math.sqrt(p) - 1.0 / math.sqrt(p_b))


def _amount1_at(L: float, p: float, p_a: float) -> float:
    """V3 token1 amount. Clamped 0 below p_a."""
    if p <= p_a:
        return 0.0
    return L * (math.sqrt(p) - math.sqrt(p_a))


def build_grid(
    *,
    tick_lower: int,
    tick_upper: int,
    L: float,
    p0_usd: float,
    p1_usd: float,
    min_leg_notional_usd: float = MIN_LEG_NOTIONAL_USD,
) -> LevelGrid:
    """Build a fresh LevelGrid for the given Beefy tick range and current
    USD prices. Discretizes [p_a, p_b] adaptively: each adjacent level
    pair must produce ≥`min_leg_notional_usd` in at least one leg.

    Algorithm:
      1. p_a, p_b from ticks via 1.0001^tick.
      2. Walk forward from p_a in fine sub-steps; at each candidate p,
         check |Δamount0|·p0_usd OR |Δamount1|·p1_usd ≥ floor; if yes,
         emit the level and reset accumulator.
      3. Always include p_b as the last level.

    Capped at MAX_LEVELS to protect against pathological ranges.
    """
    p_a = math.pow(1.0001, tick_lower)
    p_b = math.pow(1.0001, tick_upper)

    if p_a >= p_b:
        raise ValueError(
            f"Invalid tick range: tick_lower={tick_lower} >= tick_upper={tick_upper}"
        )

    p_levels = [p_a]
    amount0_at = [_amount0_at(L, p_a, p_b)]
    amount1_at = [_amount1_at(L, p_a, p_a)]  # = 0 at p_a

    # Sub-step: 1/1000 of the range. Fine enough that the floor check is
    # smooth, coarse enough to keep the loop fast.
    sub_step = (p_b - p_a) / 10_000
    p = p_a + sub_step
    last_a0 = amount0_at[0]
    last_a1 = amount1_at[0]

    while p < p_b and len(p_levels) < MAX_LEVELS - 1:
        a0 = _amount0_at(L, p, p_b)
        a1 = _amount1_at(L, p, p_a)
        d0_notional = abs(a0 - last_a0) * p0_usd
        d1_notional = abs(a1 - last_a1) * p1_usd
        if max(d0_notional, d1_notional) >= min_leg_notional_usd:
            p_levels.append(p)
            amount0_at.append(a0)
            amount1_at.append(a1)
            last_a0 = a0
            last_a1 = a1
        p += sub_step

    # Always close with p_b as the last level
    p_levels.append(p_b)
    amount0_at.append(_amount0_at(L, p_b, p_b))  # = 0
    amount1_at.append(_amount1_at(L, p_b, p_a))

    return LevelGrid(
        p_a=p_a, p_b=p_b, L=L,
        p_levels=p_levels,
        amount0_at=amount0_at,
        amount1_at=amount1_at,
        tick_lower=tick_lower,
        tick_upper=tick_upper,
    )
```

- [ ] **Step 4: Run tests**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_predictive_grid.py -v`
Expected: 10/10 PASS (6 from T1 + 4 new).

- [ ] **Step 5: Commit**

```bash
git add engine/predictive_grid.py tests/test_predictive_grid.py
git commit -m "feat(predictive): build_grid with adaptive \$0.50 leg-notional spacing

Discretizes [p_a, p_b] in 10k sub-steps, emits a level whenever one
leg's accumulated notional crosses MIN_LEG_NOTIONAL_USD (default
\$0.50). Endpoints are always p_a and p_b (consistent OOR clamp).
Hard cap MAX_LEVELS = 500 protects against pathological tick ranges.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `PredictiveUnavailable` exception + `StateHub.predictive_status`

**Files:**
- Modify: `engine/__init__.py` (add exception class near top)
- Modify: `state.py` (add field)

- [ ] **Step 1: Write the failing test**

Append to `tests/test_predictive_grid.py`:

```python
def test_predictive_unavailable_is_exception():
    """Engine raises PredictiveUnavailable when fallback should run."""
    from engine import PredictiveUnavailable
    exc = PredictiveUnavailable("book empty")
    assert isinstance(exc, Exception)
    assert str(exc) == "book empty"


def test_state_hub_has_predictive_status_field():
    from state import StateHub
    hub = StateHub(hedge_ratio=0.98)
    assert hub.predictive_status == "idle"
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_predictive_grid.py::test_predictive_unavailable_is_exception tests/test_predictive_grid.py::test_state_hub_has_predictive_status_field -v`
Expected: FAIL.

- [ ] **Step 3: Add the exception in `engine/__init__.py`**

In `engine/__init__.py`, near the top imports section (after the existing imports, before the class definition), add:

```python
class PredictiveUnavailable(Exception):
    """Raised by predictive_grid path when this iter can't fire predictively
    (book empty, RPC down, grid not built yet). Caller falls back to the
    reactive `_maybe_rebalance_leg` path. Per spec
    2026-05-08-predictive-curve-grid-hedge-design § Coexistence.
    """
```

- [ ] **Step 4: Add field to `state.py`**

In `state.py`, locate the `StateHub` dataclass. Add the field next to other `*_status` fields (or at the end of the regular fields):

```python
    # Predictive grid status (predictive grid spec 2026-05-08).
    # Values: "idle", "active", "warmup", "no_grid",
    #         "fallback: <reason>". Surfaces in dashboard.
    predictive_status: str = "idle"
```

- [ ] **Step 5: Run tests to verify pass**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_predictive_grid.py::test_predictive_unavailable_is_exception tests/test_predictive_grid.py::test_state_hub_has_predictive_status_field -v`
Expected: PASS.

- [ ] **Step 6: Run all existing engine tests to confirm no regression**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_engine_dual_leg.py tests/test_engine_funding.py tests/test_state.py -q`
Expected: all pass.

- [ ] **Step 7: Commit**

```bash
git add engine/__init__.py state.py tests/test_predictive_grid.py
git commit -m "feat(engine): PredictiveUnavailable exception + StateHub.predictive_status

Per spec 2026-05-08-predictive-curve-grid-hedge-design. The exception
is raised inside the predictive iter when this cycle can't fire
(book empty, RPC failure, grid not built); the engine's _iterate
catches it and falls back to the reactive _maybe_rebalance_leg path.
predictive_status surfaces the current mode in the dashboard so the
user sees when fallback is engaged.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Engine slots + `_grid_stale` + `_refresh_grid` (60s polling)

**Files:**
- Modify: `engine/__init__.py` (add slots in `__init__`, methods in class)
- Test: `tests/test_predictive_grid_refresh.py` (CREATE)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_predictive_grid_refresh.py`:

```python
"""Tests for the engine's _refresh_grid polling logic."""
import time
from unittest.mock import AsyncMock, MagicMock
import pytest

from state import StateHub


def _engine_with_predictive():
    """Build an engine instance ready to test refresh logic."""
    from engine import GridMakerEngine

    state = StateHub(hedge_ratio=0.98)
    state.operation_state = "active"
    state.current_operation_id = 1

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
    exchange.subscribe_funding = MagicMock()
    exchange.get_market_meta = AsyncMock()

    pool = MagicMock()
    pool.read_slot0 = AsyncMock(return_value=(int(2**96 * 1.0), -78500))  # mid range
    beefy = MagicMock()
    beefy._strategy = MagicMock()
    beefy._strategy.functions = MagicMock()
    # positionMain returns nested tuple: ((tickLower, tickUpper), (a0, a1), ...)
    beefy._strategy.functions.positionMain = MagicMock(
        return_value=MagicMock(
            call=AsyncMock(return_value=((-81121, -76012), (0, 0), 0, 0)),
        )
    )

    eng = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=18,
    )
    return eng


@pytest.mark.asyncio
async def test_grid_stale_true_when_never_checked():
    eng = _engine_with_predictive()
    assert eng._grid_stale() is True


@pytest.mark.asyncio
async def test_grid_stale_false_within_interval():
    eng = _engine_with_predictive()
    eng._last_grid_check_at = time.monotonic()  # checked now
    assert eng._grid_stale() is False


@pytest.mark.asyncio
async def test_grid_stale_true_after_interval():
    eng = _engine_with_predictive()
    eng._last_grid_check_at = time.monotonic() - 61  # 61s ago > 60s interval
    assert eng._grid_stale() is True


@pytest.mark.asyncio
async def test_refresh_grid_builds_when_no_grid_exists(monkeypatch):
    """First call: no grid yet → builds one."""
    eng = _engine_with_predictive()
    # Need L for build_grid; engine reads it via beefy.read_position (mocked)
    eng._beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-81121, tick_upper=-76012,
        amount0=0.01, amount1=100.0, share=1.0, raw_balance=10**18,
    ))
    await eng._refresh_grid()
    assert eng._grid is not None
    assert eng._grid.tick_lower == -81121
    assert eng._grid.tick_upper == -76012
    assert eng._last_level_idx is None  # warmup-pending


@pytest.mark.asyncio
async def test_refresh_grid_skips_rebuild_when_unchanged(monkeypatch):
    """Same tick range → no rebuild. _last_level_idx preserved."""
    eng = _engine_with_predictive()
    eng._beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-81121, tick_upper=-76012,
        amount0=0.01, amount1=100.0, share=1.0, raw_balance=10**18,
    ))
    await eng._refresh_grid()
    grid_id_before = id(eng._grid)
    eng._last_level_idx = 5  # simulate active level tracking

    # Force re-poll
    eng._last_grid_check_at -= 100
    await eng._refresh_grid()

    assert id(eng._grid) == grid_id_before  # same object, no rebuild
    assert eng._last_level_idx == 5  # preserved


@pytest.mark.asyncio
async def test_refresh_grid_keeps_old_grid_on_rpc_failure():
    """positionMain RPC raises → keep existing _grid intact."""
    eng = _engine_with_predictive()
    eng._beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-81121, tick_upper=-76012,
        amount0=0.01, amount1=100.0, share=1.0, raw_balance=10**18,
    ))
    await eng._refresh_grid()
    grid_before = eng._grid

    # Now make positionMain raise
    eng._beefy_reader._strategy.functions.positionMain.return_value.call = AsyncMock(
        side_effect=RuntimeError("RPC timeout"),
    )
    eng._last_grid_check_at -= 100  # force re-poll
    await eng._refresh_grid()

    assert eng._grid is grid_before  # intact
    # And the check timestamp WAS updated (no retry storm)
    assert (time.monotonic() - eng._last_grid_check_at) < 5
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_predictive_grid_refresh.py -v`
Expected: FAIL — `_grid`, `_last_level_idx`, `_grid_stale`, `_refresh_grid` don't exist.

- [ ] **Step 3: Add slots in `engine/__init__.py` `__init__`**

Locate `GridMakerEngine.__init__` in `engine/__init__.py`. After existing slot assignments (find the block where `self._token0_mid` was added in the funding wiring), add:

```python
        # Predictive curve-grid (spec 2026-05-08). _grid is None until
        # _refresh_grid() runs successfully. _last_level_idx is None
        # immediately after a rebuild → next iter snaps without firing
        # (warmup). _last_grid_check_at gates polling cadence.
        self._grid = None  # type: "LevelGrid | None"
        self._last_level_idx: int | None = None
        self._last_grid_check_at: float = 0.0
        self._GRID_CHECK_INTERVAL_S = 60.0
```

- [ ] **Step 4: Add `_grid_stale` and `_refresh_grid` methods**

In `engine/__init__.py`, add these methods on `GridMakerEngine` (place after `_safe_get_position` or near other internal helpers):

```python
    def _grid_stale(self) -> bool:
        """True if it's time to re-poll Beefy strategy.positionMain() to
        check for tick range change. Polling cadence
        self._GRID_CHECK_INTERVAL_S (60 s by default).
        """
        return (
            time.monotonic() - self._last_grid_check_at
        ) > self._GRID_CHECK_INTERVAL_S

    async def _refresh_grid(self) -> None:
        """Polls Beefy + rebuilds grid if tick range changed.

        Atomic on failure: keeps existing self._grid intact, just bumps
        the check timestamp so we don't retry-storm. Resets
        self._last_level_idx to None on a real rebuild → next iter snaps
        (no spurious fire). Per spec § Re-grid.
        """
        from engine.predictive_grid import build_grid
        from engine.curve import compute_l_from_value

        try:
            position_main = await (
                self._beefy_reader._strategy.functions.positionMain().call()
            )
            new_lower = int(position_main[0][0])
            new_upper = int(position_main[0][1])
        except Exception as e:
            logger.warning(f"_refresh_grid: positionMain() failed: {e}")
            self._last_grid_check_at = time.monotonic()  # avoid retry storm
            return

        self._last_grid_check_at = time.monotonic()

        if (self._grid is not None
            and self._grid.tick_lower == new_lower
            and self._grid.tick_upper == new_upper):
            return  # cached grid is current

        # Rebuild needed. Need L = compute_l_from_value(my_value_t1, p_a, p_b, p_now).
        # Read current LP composition + current p.
        try:
            beefy_pos = await self._beefy_reader.read_position()
            my_amount0 = beefy_pos.amount0 * beefy_pos.share
            my_amount1 = beefy_pos.amount1 * beefy_pos.share
            sqrt_price_x96, _current_tick = await self._pool_reader.read_slot0()
            p_now = (sqrt_price_x96 / 2**96) ** 2
        except Exception as e:
            logger.warning(f"_refresh_grid: chain read failed: {e}")
            return  # keep old grid

        my_value_t1 = my_amount0 * p_now + my_amount1
        if my_value_t1 <= 0:
            logger.warning("_refresh_grid: my_value_t1 <= 0, skipping build")
            return

        import math
        p_a = math.pow(1.0001, new_lower)
        p_b = math.pow(1.0001, new_upper)
        # L_user requires p inside [p_a, p_b]. If OOR, fall back to a
        # heuristic: use the closer boundary as p for the L solve.
        p_for_l = max(p_a, min(p_b, p_now))
        try:
            L_user = compute_l_from_value(my_value_t1, p_a, p_b, p_for_l)
        except Exception as e:
            logger.warning(f"_refresh_grid: compute_l_from_value failed: {e}")
            return

        try:
            new_grid = build_grid(
                tick_lower=new_lower, tick_upper=new_upper,
                L=L_user,
                p0_usd=self._hub.token0_usd_price or 1.0,
                p1_usd=self._hub.token1_usd_price or 1.0,
                min_leg_notional_usd=self._settings.min_rebalance_notional_usd,
            )
        except Exception as e:
            logger.exception(f"_refresh_grid: build_grid failed: {e}")
            return

        old_range = (
            (self._grid.tick_lower, self._grid.tick_upper)
            if self._grid else None
        )
        self._grid = new_grid
        self._last_level_idx = None  # warmup next iter — no fire
        logger.info(
            f"Grid rebuilt: range {old_range} → ({new_lower}, {new_upper}), "
            f"{len(new_grid.p_levels)} levels"
        )
```

- [ ] **Step 5: Run tests to verify pass**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_predictive_grid_refresh.py -v`
Expected: 6/6 PASS.

- [ ] **Step 6: Commit**

```bash
git add engine/__init__.py tests/test_predictive_grid_refresh.py
git commit -m "feat(engine): _grid_stale + _refresh_grid 60s polling

Per spec 2026-05-08-predictive-curve-grid-hedge-design § Re-grid.
60s polling on Beefy strategy.positionMain(). Rebuilds grid only
when tick range actually changes; resets _last_level_idx to None on
rebuild so the next iter snaps without firing (warmup). On RPC
failure keeps existing grid intact and updates the check timestamp
to avoid retry-storms.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `_fire_predictive_leg` (book bid/ask execution)

**Files:**
- Modify: `engine/__init__.py` (add method)
- Test: `tests/test_predictive_engine.py` (CREATE)

- [ ] **Step 1: Write the failing tests**

Create `tests/test_predictive_engine.py`:

```python
"""Tests for engine's predictive iter logic + per-leg fire."""
from unittest.mock import AsyncMock, MagicMock
import pytest

from state import StateHub


def _engine_with_book(eth_bid=2300.0, eth_ask=2301.0, arb_bid=0.130, arb_ask=0.131):
    """Build an engine with the lighter adapter's _ws_book_top pre-populated
    for ETH (mid 0) and ARB (mid 50)."""
    from engine import GridMakerEngine

    state = StateHub(hedge_ratio=0.98)
    state.operation_state = "active"
    state.current_operation_id = 1

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
    db.insert_order_log = AsyncMock()
    exchange = MagicMock()
    exchange.name = "lighter"
    exchange.subscribe_funding = MagicMock()
    exchange.place_long_term_order = AsyncMock()
    exchange.get_market_meta = AsyncMock()
    exchange._ws_book_top = {
        0: {"best_bid": eth_bid, "best_ask": eth_ask, "ts": 0},
        50: {"best_bid": arb_bid, "best_ask": arb_ask, "ts": 0},
    }

    pool = MagicMock(); beefy = MagicMock()
    eng = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=18,
    )
    eng._token0_mid = 0
    eng._token1_mid = 50
    return eng, exchange


@pytest.mark.asyncio
async def test_fire_predictive_leg_sells_at_bid():
    """delta > 0 (need to short more) → SELL at the best bid."""
    eng, exchange = _engine_with_book(eth_bid=2300.0, eth_ask=2301.0)
    await eng._fire_predictive_leg("ETH-USD", delta=0.001)
    exchange.place_long_term_order.assert_awaited_once()
    call = exchange.place_long_term_order.await_args
    assert call.kwargs["side"] == "sell"
    assert call.kwargs["price"] == 2300.0  # bid


@pytest.mark.asyncio
async def test_fire_predictive_leg_buys_at_ask():
    """delta < 0 (close some short) → BUY at the best ask."""
    eng, exchange = _engine_with_book(eth_bid=2300.0, eth_ask=2301.0)
    await eng._fire_predictive_leg("ETH-USD", delta=-0.001)
    exchange.place_long_term_order.assert_awaited_once()
    call = exchange.place_long_term_order.await_args
    assert call.kwargs["side"] == "buy"
    assert call.kwargs["price"] == 2301.0  # ask


@pytest.mark.asyncio
async def test_fire_predictive_leg_skips_below_dollar_floor():
    """Sub-$0.50 leg notional → no fire, no exception."""
    eng, exchange = _engine_with_book(eth_bid=2300.0, eth_ask=2301.0)
    # 0.0001 ETH × $2300 = $0.23 → below $0.50 floor → skip
    await eng._fire_predictive_leg("ETH-USD", delta=0.0001)
    exchange.place_long_term_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_fire_predictive_leg_empty_book_raises_predictive_unavailable():
    """No book entry for symbol's market_id → raises PredictiveUnavailable."""
    from engine import PredictiveUnavailable
    eng, exchange = _engine_with_book()
    exchange._ws_book_top = {}  # empty
    with pytest.raises(PredictiveUnavailable):
        await eng._fire_predictive_leg("ETH-USD", delta=0.001)


@pytest.mark.asyncio
async def test_fire_predictive_leg_zero_delta_no_fire():
    eng, exchange = _engine_with_book()
    await eng._fire_predictive_leg("ETH-USD", delta=0.0)
    exchange.place_long_term_order.assert_not_awaited()
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_predictive_engine.py::test_fire_predictive_leg_sells_at_bid tests/test_predictive_engine.py::test_fire_predictive_leg_buys_at_ask tests/test_predictive_engine.py::test_fire_predictive_leg_skips_below_dollar_floor tests/test_predictive_engine.py::test_fire_predictive_leg_empty_book_raises_predictive_unavailable tests/test_predictive_engine.py::test_fire_predictive_leg_zero_delta_no_fire -v`
Expected: FAIL — `_fire_predictive_leg` doesn't exist.

- [ ] **Step 3: Implement the method**

In `engine/__init__.py`, add to `GridMakerEngine`:

```python
    async def _fire_predictive_leg(
        self, symbol: str, delta: float,
    ) -> None:
        """Place a taker order at the current bid (sell) or ask (buy) for
        a per-leg `delta`. Skips silently if leg notional < $0.50.
        Raises PredictiveUnavailable if the book has no entry for this
        market. Per spec § Per-leg fire.
        """
        if abs(delta) < 1e-12:
            return

        side = "sell" if delta > 0 else "buy"
        size = abs(delta)

        # Engine resolves _token0_mid/_token1_mid in the funding-handler
        # wiring (commit a571603). Reuse those.
        if symbol == self._settings.dydx_symbol_token0:
            market_id = self._token0_mid
        elif symbol == self._settings.dydx_symbol_token1:
            market_id = self._token1_mid
        else:
            market_id = None
        if market_id is None:
            raise PredictiveUnavailable(
                f"market_id unresolved for {symbol}"
            )

        book = getattr(self._exchange, "_ws_book_top", {}).get(market_id)
        if not book or not book.get("best_bid") or not book.get("best_ask"):
            raise PredictiveUnavailable(
                f"book empty for {symbol} (mid={market_id})"
            )

        price = book["best_bid"] if side == "sell" else book["best_ask"]
        leg_notional_usd = size * price
        if leg_notional_usd < self._settings.min_rebalance_notional_usd:
            logger.debug(
                f"Predictive: skip {symbol} leg, "
                f"${leg_notional_usd:.4f} < ${self._settings.min_rebalance_notional_usd:.2f}"
            )
            return

        cloid = self._next_cloid_for_leg(symbol)
        await self._exchange.place_long_term_order(
            symbol=symbol, side=side, size=size, price=price,
            cloid_int=cloid, ttl_seconds=60,
        )
        logger.info(
            f"Predictive fire [{symbol}]: {side} {size:.6f} @ {price:.6f} "
            f"(${leg_notional_usd:.2f})"
        )
```

- [ ] **Step 4: Run tests to verify pass**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_predictive_engine.py -v`
Expected: 5/5 PASS.

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_predictive_engine.py
git commit -m "feat(engine): _fire_predictive_leg with book bid/ask execution

Per spec § Per-leg fire. Sells at best_bid, buys at best_ask read
directly from LighterAdapter._ws_book_top (no oracle mid + buffer).
Skips silently when leg notional < settings.min_rebalance_notional_usd
(default \$0.50). Raises PredictiveUnavailable when book is empty
(triggers reactive fallback in _iterate). Reuses _token0_mid /
_token1_mid resolved in the funding-handler wiring (a571603).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: `_iterate_predictive` — per-iter level mapping + dispatch

**Files:**
- Modify: `engine/__init__.py` (add method)
- Test: `tests/test_predictive_engine.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_predictive_engine.py`:

```python
@pytest.mark.asyncio
async def test_iterate_predictive_first_iter_snaps_no_fire():
    """First iter post-rebuild: _last_level_idx is None → snap, no fire."""
    from engine.predictive_grid import build_grid
    eng, exchange = _engine_with_book()

    # Build a grid manually so the iter has something to map against
    grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=100.0, p0_usd=2300.0, p1_usd=0.13,
        min_leg_notional_usd=0.50,
    )
    eng._grid = grid
    eng._last_level_idx = None  # explicit

    # Mock pool to return a sqrt_price_x96 inside the range
    import math
    mid_p = (grid.p_a + grid.p_b) / 2
    sqrt_p_x96 = int(math.sqrt(mid_p) * 2**96)
    eng._pool_reader.read_slot0 = AsyncMock(return_value=(sqrt_p_x96, -78500))

    await eng._iterate_predictive()
    exchange.place_long_term_order.assert_not_awaited()
    assert eng._last_level_idx is not None  # snapped


@pytest.mark.asyncio
async def test_iterate_predictive_level_change_fires_both_legs():
    """Level change: both legs check independently and fire if above floor."""
    from engine.predictive_grid import build_grid
    eng, exchange = _engine_with_book(
        eth_bid=2300.0, eth_ask=2301.0, arb_bid=0.130, arb_ask=0.131,
    )

    grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=100.0, p0_usd=2300.0, p1_usd=0.13,
        min_leg_notional_usd=0.50,
    )
    eng._grid = grid
    # Start at idx=0 (p_a edge)
    eng._last_level_idx = 0

    # Move p_now far enough that it crosses many levels — definitely fires
    import math
    p_target = grid.p_b * 0.95  # close to upper bound
    sqrt_p_x96 = int(math.sqrt(p_target) * 2**96)
    eng._pool_reader.read_slot0 = AsyncMock(return_value=(sqrt_p_x96, -76100))

    await eng._iterate_predictive()
    # Both legs should have fired (huge level jump → big delta both legs)
    assert exchange.place_long_term_order.await_count == 2


@pytest.mark.asyncio
async def test_iterate_predictive_no_level_change_no_fire():
    """Same level idx → no fire."""
    from engine.predictive_grid import build_grid, find_level_idx
    eng, exchange = _engine_with_book()
    grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=100.0, p0_usd=2300.0, p1_usd=0.13,
        min_leg_notional_usd=0.50,
    )
    eng._grid = grid
    # Pin level idx that matches the p we're about to read
    import math
    p_now = grid.p_levels[10] + (grid.p_levels[11] - grid.p_levels[10]) / 2
    expected_idx = find_level_idx(grid, p_now)
    eng._last_level_idx = expected_idx
    sqrt_p_x96 = int(math.sqrt(p_now) * 2**96)
    eng._pool_reader.read_slot0 = AsyncMock(return_value=(sqrt_p_x96, -78500))

    await eng._iterate_predictive()
    exchange.place_long_term_order.assert_not_awaited()


@pytest.mark.asyncio
async def test_iterate_predictive_raises_when_slot0_fails():
    """Pool RPC failure → PredictiveUnavailable for fallback."""
    from engine import PredictiveUnavailable
    from engine.predictive_grid import build_grid
    eng, exchange = _engine_with_book()
    eng._grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=100.0, p0_usd=2300.0, p1_usd=0.13,
        min_leg_notional_usd=0.50,
    )
    eng._last_level_idx = 5
    eng._pool_reader.read_slot0 = AsyncMock(side_effect=RuntimeError("RPC down"))
    with pytest.raises(PredictiveUnavailable):
        await eng._iterate_predictive()
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_predictive_engine.py::test_iterate_predictive_first_iter_snaps_no_fire tests/test_predictive_engine.py::test_iterate_predictive_level_change_fires_both_legs tests/test_predictive_engine.py::test_iterate_predictive_no_level_change_no_fire tests/test_predictive_engine.py::test_iterate_predictive_raises_when_slot0_fails -v`
Expected: FAIL — `_iterate_predictive` doesn't exist.

- [ ] **Step 3: Implement the method**

In `engine/__init__.py`, add to `GridMakerEngine`:

```python
    async def _iterate_predictive(self) -> None:
        """One predictive iter:
        1. Read pool slot0 (raises PredictiveUnavailable on failure).
        2. Map p_now → level idx via bisect.
        3. If first iter post-rebuild (_last_level_idx None): snap, no fire.
        4. If no level change: nothing to do.
        5. Compute deltas with hedge_ratio applied; fire each leg
           sequentially. Sequentially because Lighter's nonce manager
           races on parallel signing.
        Per spec § Per-iter logic.
        """
        from engine.predictive_grid import find_level_idx, compute_deltas

        if self._grid is None:
            raise PredictiveUnavailable("grid not built")

        try:
            sqrt_price_x96, _current_tick = await self._pool_reader.read_slot0()
            p_now = (sqrt_price_x96 / 2**96) ** 2
        except Exception as e:
            raise PredictiveUnavailable(f"slot0 read failed: {e}")

        new_idx = find_level_idx(self._grid, p_now)

        if self._last_level_idx is None:
            self._last_level_idx = new_idx
            return  # warmup, no fire

        if new_idx == self._last_level_idx:
            return  # no level change

        delta_t0, delta_t1 = compute_deltas(
            self._grid, self._last_level_idx, new_idx,
        )
        delta_t0 *= self._hub.hedge_ratio
        delta_t1 *= self._hub.hedge_ratio

        # Fire sequentially: Lighter's nonce_manager races on parallel
        # next_nonce() (server-side dedup window).
        await self._fire_predictive_leg(
            self._settings.dydx_symbol_token0, delta_t0,
        )
        await self._fire_predictive_leg(
            self._settings.dydx_symbol_token1, delta_t1,
        )

        self._last_level_idx = new_idx
```

- [ ] **Step 4: Run tests to verify pass**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_predictive_engine.py -v`
Expected: 9/9 PASS (5 from T5 + 4 new).

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_predictive_engine.py
git commit -m "feat(engine): _iterate_predictive — pool ratio level mapping + dispatch

Per spec § Per-iter predictive logic. Reads Uniswap slot0, maps p_now
to a level idx via bisect, computes per-leg deltas (with hedge_ratio
applied), and fires both legs sequentially. First iter after a rebuild
snaps without firing (warmup). Pool RPC failure raises
PredictiveUnavailable so the engine falls back to reactive in the
same iter (T7).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 7: Rewrite `_iterate` — predictive primary + reactive fallback (HARD GUARD)

**Files:**
- Modify: `engine/__init__.py` (rewrite `_iterate` body)
- Test: `tests/test_predictive_engine.py` (extend)

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_predictive_engine.py`:

```python
@pytest.mark.asyncio
async def test_iterate_falls_back_to_reactive_when_predictive_unavailable():
    """When _iterate_predictive raises PredictiveUnavailable, _iterate runs
    the reactive _maybe_rebalance_leg path."""
    eng, exchange = _engine_with_book()
    eng._grid = None  # forces _iterate_predictive to raise

    # Setup minimal chain reads for the reactive path
    eng._pool_reader.read_price = AsyncMock(return_value=4000.0)
    eng._beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-205311, tick_upper=-195311,
        amount0=0.01, amount1=0.0, share=1.0, raw_balance=10**18,
    ))
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=10.0))
    exchange.get_oracle_prices = AsyncMock(return_value={"ETH-USD": 4000.0, "ARB-USD": 0.13})
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])
    eng._db.get_active_grid_orders = AsyncMock(return_value=[])
    eng._db.get_operation = AsyncMock(return_value=None)
    eng._db.add_to_operation_accumulator = AsyncMock()
    # _refresh_grid would normally run; stub it so no rebuild attempts
    eng._beefy_reader._strategy.functions.positionMain = MagicMock(
        return_value=MagicMock(
            call=AsyncMock(side_effect=RuntimeError("simulated")),
        )
    )

    # Spy on reactive
    rebalance_spy_called = []
    original_rebalance = eng._maybe_rebalance_leg
    async def spy(**kwargs):
        rebalance_spy_called.append(kwargs.get("symbol"))
        return await original_rebalance(**kwargs)
    eng._maybe_rebalance_leg = spy

    await eng._iterate()
    # At least one symbol got a reactive rebalance check
    assert len(rebalance_spy_called) >= 1
    assert eng._hub.predictive_status.startswith("fallback")


@pytest.mark.asyncio
async def test_iterate_does_not_double_fire_predictive_and_reactive():
    """Predictive succeeds → reactive must NOT run (hard guard)."""
    from engine.predictive_grid import build_grid
    eng, exchange = _engine_with_book()
    eng._grid = build_grid(
        tick_lower=-81121, tick_upper=-76012,
        L=100.0, p0_usd=2300.0, p1_usd=0.13,
        min_leg_notional_usd=0.50,
    )
    eng._last_level_idx = 5

    # Pool returns p in same range → no level change → predictive runs cleanly
    import math
    sqrt_p_x96 = int(math.sqrt(eng._grid.p_levels[5]) * 2**96)
    eng._pool_reader.read_slot0 = AsyncMock(return_value=(sqrt_p_x96, -78500))

    # Skip refresh
    eng._last_grid_check_at = __import__("time").monotonic()

    # Setup reactive path stubs to detect calls
    eng._beefy_reader.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-81121, tick_upper=-76012,
        amount0=0.01, amount1=100.0, share=1.0, raw_balance=10**18,
    ))
    eng._pool_reader.read_price = AsyncMock(return_value=0.000375)
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=10.0))
    exchange.get_oracle_prices = AsyncMock(return_value={"ETH-USD": 2300.0, "ARB-USD": 0.13})
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])
    eng._db.get_active_grid_orders = AsyncMock(return_value=[])
    eng._db.get_operation = AsyncMock(return_value=None)
    eng._db.add_to_operation_accumulator = AsyncMock()

    rebalance_spy_called = []
    original_rebalance = eng._maybe_rebalance_leg
    async def spy(**kwargs):
        rebalance_spy_called.append(kwargs.get("symbol"))
        return await original_rebalance(**kwargs)
    eng._maybe_rebalance_leg = spy

    await eng._iterate()
    # Predictive ran cleanly → reactive must NOT have been called
    assert rebalance_spy_called == []
    assert eng._hub.predictive_status == "active"
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_predictive_engine.py::test_iterate_falls_back_to_reactive_when_predictive_unavailable tests/test_predictive_engine.py::test_iterate_does_not_double_fire_predictive_and_reactive -v`
Expected: FAIL — `_iterate` still has the old reactive-only logic.

- [ ] **Step 3: Rewrite the cross-pair leg of `_iterate`**

In `engine/__init__.py`, locate the existing `_iterate()` method. Find the dual-leg "Fire rebalance per leg" block (around the `for sym in symbols:` loop after the `targets` dict is built). Replace it with the predictive-primary path:

```python
            # Predictive curve-grid path (spec 2026-05-08). Falls back to
            # the reactive _maybe_rebalance_leg loop on any failure.
            fallback_reason = None
            try:
                if self._grid is None or self._grid_stale():
                    await self._refresh_grid()
                if self._grid is not None:
                    await self._iterate_predictive()
                    self._hub.predictive_status = "active"
                else:
                    fallback_reason = "grid not built"
            except PredictiveUnavailable as e:
                fallback_reason = str(e)
            except Exception as e:
                logger.exception(f"Predictive failed unexpectedly: {e}")
                fallback_reason = f"unexpected: {type(e).__name__}"

            if fallback_reason is not None:
                self._hub.predictive_status = f"fallback: {fallback_reason}"
                # Fire rebalance per leg via reactive engine (legacy path)
                for sym in symbols:
                    idx = symbols.index(sym)
                    current = abs(positions[idx].size) if positions[idx] else 0.0
                    ref_price = oracle_prices.get(sym, 0.0)
                    if ref_price <= 0:
                        continue
                    await self._maybe_rebalance_leg(
                        symbol=sym, target=targets[sym], current=current,
                        min_notional=self._settings.min_rebalance_notional_usd,
                        ref_price=ref_price,
                    )
```

The early `if fallback_reason is not None:` is the **hard guard** preventing predictive + reactive from firing in the same iter. If predictive succeeded, `fallback_reason` stays None and the reactive loop is skipped entirely.

- [ ] **Step 4: Run tests to verify pass**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_predictive_engine.py -v`
Expected: 11/11 PASS (5 + 4 + 2).

- [ ] **Step 5: Run full suite to check for regressions**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/ -q 2>&1 | tail -5`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add engine/__init__.py tests/test_predictive_engine.py
git commit -m "feat(engine): _iterate predictive primary + reactive fallback hard guard

Per spec § Coexistence. Try block runs _refresh_grid + _iterate_predictive.
On any PredictiveUnavailable (or unexpected exception), the engine
records fallback_reason and runs the reactive _maybe_rebalance_leg
loop. Hard guard via 'if fallback_reason is not None' prevents both
paths from firing in the same iter (would over-hedge). Status surfaces
in _hub.predictive_status for the dashboard.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 8: Final verification + push

**Files:** none (verification + push only)

- [ ] **Step 1: Run full test suite**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/ -q --no-header 2>&1 | tail -5`
Expected: all pass (~322 = 306 prior + 16 new).

- [ ] **Step 2: Push the branch**

```bash
git push
```

- [ ] **Step 3: Confirm uvicorn unchanged on :8000**

Run via PowerShell: `$conn = Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1; if ($conn) { Write-Output "running pid=$($conn.OwningProcess)" } else { Write-Output "stopped" }`

Expected: still running.

- [ ] **Step 4: Manual smoke (controller's responsibility — do NOT restart from this task)**

After the user restarts uvicorn manually:
1. Logs at startup: `Grid built: range (-81121, -76012), N levels` should appear within 60s.
2. Wait 1–5 min. Logs should show `Predictive fire [ETH-USD]: ...` or `[ARB-USD]: ...` as the pool ratio crosses levels.
3. Dashboard shows `state.predictive_status = "active"` most of the time.
4. Beefy LP composition × 0.98 should track Lighter short positions within cents.
5. If `predictive_status` stays in `fallback: ...`, investigate the suffix (book empty? RPC down? grid never built?).

---

## Self-Review

**Spec coverage:**
- §Architecture (engine iter flow, per-leg execution, fallback) → Tasks 5, 6, 7. ✓
- §Components > LevelGrid + build_grid + find_level_idx + compute_deltas → Tasks 1, 2. ✓
- §Components > _iterate_predictive + _fire_predictive_leg → Tasks 5, 6. ✓
- §Components > _refresh_grid + _grid_stale → Task 4. ✓
- §Out-of-range handling (edge values via clamps in _amount0_at/_amount1_at, find_level_idx caps) → Tasks 1, 2 (math), Task 6 (mapping). ✓
- §Sign convention (delta > 0 → sell, < 0 → buy; price = bid/ask) → Task 5. ✓
- §Risks 1–8 → handled (positionMain index format documented at use site, atomic rebuild on failure, cap MAX_LEVELS, hedge_ratio applied at fire-time, hard guard for double-fire, OOR static-edge frozen behavior, log noise on cumulative failures via standard `logger.warning`). ✓
- §Testing (16 tests planned) → 6 in test_predictive_grid.py (T1) + 4 in test_predictive_grid.py (T2) + 2 in test_predictive_grid.py (T3) + 6 in test_predictive_grid_refresh.py (T4) + 9 in test_predictive_engine.py (T5+T6) + 2 in test_predictive_engine.py (T7) = 29 tests across the plan. Plan has MORE coverage than spec promised. ✓
- §Verification (post-deploy) → Task 8 step 4. ✓

**Placeholder scan:** every step has concrete code or commands. No TBDs. Test code is self-contained per step.

**Type consistency:**
- `LevelGrid` fields consistent across T1 (definition), T2 (build_grid emits), T4 (refresh stores), T6 (find_level_idx + compute_deltas read). ✓
- `PredictiveUnavailable` exception consistent: T3 defines, T4/T5/T6 raise, T7 catches. ✓
- `predictive_status: str` consistent: T3 defines on StateHub default `"idle"`, T7 sets to `"active"` / `"fallback: <reason>"`. ✓
- `_token0_mid` / `_token1_mid` reused from existing engine state (commit a571603 funding wiring). T5 reads them. ✓
- `_grid_stale()`, `_refresh_grid()`, `_iterate_predictive()`, `_fire_predictive_leg()` method names consistent across T4–T7. ✓
- `min_rebalance_notional_usd` setting reused (already exists from earlier rebalance fix b7c765d). T2 + T5 read via `self._settings.min_rebalance_notional_usd`. ✓

Plan ready for execution.
