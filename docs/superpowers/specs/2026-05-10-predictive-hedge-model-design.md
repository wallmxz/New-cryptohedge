# Predictive Hedge Model — Design

**Date:** 2026-05-10
**Status:** Approved (brainstorm)
**Branch:** `feature/predictive-grid-v2`
**Supersedes:** `docs/superpowers/specs/2026-05-08-predictive-curve-grid-hedge-design.md` (the v1 grid-based design that hit the `positionAlt` bug)

## Problem

The reactive hedge engine works (commit `ed8923d` disabled predictive v1 and reverted to reactive `_maybe_rebalance_leg` with `$0.50` floor). But reactive has two limitations the user wants to fix:

1. **No anticipation.** Bot only fires AFTER reading Beefy's `balances()` and computing drift. There's no internal model of "given current pool ratio, what should the hedge be?". Pure stimulus-response.
2. **No semantic check.** If Beefy's `balances()` ever returns garbage (RPC corruption, contract upgrade), reactive silently follows the wrong target. There's no independent calculation to cross-check against.

## Goal

Replace reactive with a **predictive hedge model** that:
- Computes target hedge LOCALLY from V3 formula using on-chain liquidity (`L_main`, `L_alt`) — not derived from Beefy aggregates
- Verifies its prediction against Beefy `balances()` every iter (Beefy stays the authoritative ground truth for fires)
- Refreshes `L` cache on a 300s TTL OR on verify divergence > 1%
- Maintains all existing safety: `position-truth` stamping, `$0.50` floor, no buffer on bid/ask, sequential per-leg fires
- Cannot over-hedge by structural design (target always uses authoritative actual, predicted is informational)

The v1 grid-based design (`engine/predictive_grid.py`) is removed entirely — the V3 formula is cheap enough to evaluate per-iter; pre-computing a discrete grid was an unnecessary optimization that introduced the buggy `compute_l_from_value` derivation.

## Non-goals (explicit out-of-scope)
- LP fees attribution (separate spec, future)
- Pre-placing limit orders at expected level prices (Lighter is taker-only via this engine)
- UI grid visualization (no grid to visualize)
- Multi-vault support (per-engine, per-vault as today)
- Backtest integration (simulator stays on reactive for now)

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  ENGINE ITER (1 Hz)                                              │
│                                                                   │
│  Parallel reads (asyncio.gather, 5s timeout each):               │
│    pool.slot0()           → p_now, current_tick                  │
│    beefy.read_position()  → tick_lower/upper, amount0/1, share   │
│    [get_effective_position(s) for s in legs]  → current shorts   │
│                                                                   │
│  L cache check:                                                   │
│    stale (TTL 300s)? OR new_tick_range? → trigger refresh        │
│                                                                   │
│  Predict target via V3 formula:                                   │
│    predicted_amount0 = L_main·(1/√p − 1/√p_b)                    │
│                      + L_alt·(1/√p − 1/√p_b_alt) [if in range]   │
│    predicted_amount1 = L_main·(√p − √p_a)                        │
│                      + L_alt·(√p − √p_a_alt)    [if in range]    │
│                                                                   │
│  Verify (Beefy is authoritative):                                 │
│    actual_amount0 = beefy.amount0                                │
│    actual_amount1 = beefy.amount1                                │
│    if |predicted − actual|/actual > 1%:                          │
│        log warning, schedule L refresh next iter                 │
│                                                                   │
│  Target uses ACTUAL (predicted is informational):                 │
│    target_t0 = actual_amount0 · share · hedge_ratio              │
│    target_t1 = actual_amount1 · share · hedge_ratio              │
│                                                                   │
│  Drift fire per leg (sequential, position-truth-protected):      │
│    drift = target − current_short                                 │
│    if |drift| · ref_price ≥ $0.50: fire(side, abs(drift))        │
└──────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌──────────────────────────────────────────────────────────────────┐
│  L CACHE REFRESH (async, non-blocking)                           │
│                                                                   │
│  Read positionMain L from Uniswap V3 pool directly:              │
│    key = keccak256(beefy_strategy, tickLower, tickUpper)         │
│    (liquidity, ...) = pool.positions(key)                        │
│  Same for positionAlt (using strategy.positionAlt() ranges)      │
│                                                                   │
│  Update cache atomically:                                         │
│    self._L_cache = HedgeModelCache(                              │
│        L_main, p_a_main, p_b_main,                               │
│        L_alt,  p_a_alt,  p_b_alt,                                │
│        refreshed_at=monotonic())                                 │
│                                                                   │
│  Failure: keep cached values, log warning, retry next iter       │
└──────────────────────────────────────────────────────────────────┘
```

## Components

### `chains/v3_position.py` (new, ~50 LoC)

```python
@dataclass
class V3Position:
    liquidity: int       # raw L from V3 pool storage
    tick_lower: int
    tick_upper: int

class V3PositionReader:
    """Reads positionMain + positionAlt liquidity directly from
    Uniswap V3 pool (not derived from Beefy balances)."""

    def __init__(self, w3, pool_address, beefy_strategy_address):
        self._pool = w3.eth.contract(address=pool_address, abi=UNISWAP_V3_POOL_ABI)
        self._strategy = w3.eth.contract(address=beefy_strategy_address, abi=STRATEGY_ABI)

    async def read_position_main(self) -> V3Position:
        """Reads (tickLower, tickUpper) from Beefy strategy.positionMain(),
        then queries Uniswap V3 pool.positions(key) directly for L."""
        tick_lower, tick_upper = await self._strategy.functions.positionMain().call()
        return await self._read_v3_position_at(tick_lower, tick_upper)

    async def read_position_alt(self) -> V3Position | None:
        """Reads positionAlt range. Returns None if alt is empty/inactive."""
        try:
            tick_lower, tick_upper = await self._strategy.functions.positionAlt().call()
            if tick_lower == tick_upper:  # alt inactive sentinel
                return None
            return await self._read_v3_position_at(tick_lower, tick_upper)
        except Exception:
            return None  # alt not active

    async def _read_v3_position_at(self, tick_lower: int, tick_upper: int) -> V3Position:
        position_key = self._compute_position_key(tick_lower, tick_upper)
        liquidity, *_ = await self._pool.functions.positions(position_key).call()
        return V3Position(liquidity=liquidity, tick_lower=tick_lower, tick_upper=tick_upper)

    def _compute_position_key(self, tick_lower: int, tick_upper: int) -> bytes:
        return Web3.solidity_keccak(
            ["address", "int24", "int24"],
            [self._strategy.address, tick_lower, tick_upper],
        )
```

### `engine/hedge_model.py` (new, ~80 LoC)

```python
@dataclass
class HedgeModelCache:
    L_main: int
    p_a_main: float
    p_b_main: float
    L_alt: int | None
    p_a_alt: float | None
    p_b_alt: float | None
    refreshed_at: float  # monotonic seconds

REFRESH_TTL_S = 300.0
DIVERGENCE_THRESHOLD = 0.01  # 1%

class HedgeModel:
    """Predictive hedge model. Maintains cached L from V3 positions,
    computes target via formula, verifies against Beefy actual."""

    def __init__(self, v3_reader: V3PositionReader):
        self._reader = v3_reader
        self._cache: HedgeModelCache | None = None
        self._refresh_pending: bool = False

    def cache_stale(self) -> bool:
        if self._cache is None:
            return True
        return (time.monotonic() - self._cache.refreshed_at) > REFRESH_TTL_S

    async def refresh_cache(self) -> None:
        """Re-reads L_main + L_alt from V3 pool. Updates cache atomically.
        Failure preserves prior cache."""
        try:
            main, alt = await asyncio.gather(
                self._reader.read_position_main(),
                self._reader.read_position_alt(),
            )
            self._cache = HedgeModelCache(
                L_main=main.liquidity,
                p_a_main=math.pow(1.0001, main.tick_lower),
                p_b_main=math.pow(1.0001, main.tick_upper),
                L_alt=alt.liquidity if alt else None,
                p_a_alt=math.pow(1.0001, alt.tick_lower) if alt else None,
                p_b_alt=math.pow(1.0001, alt.tick_upper) if alt else None,
                refreshed_at=time.monotonic(),
            )
            self._refresh_pending = False
        except Exception as e:
            logger.warning(f"HedgeModel.refresh_cache failed, keeping prior: {e}")

    def predict(self, p_now: float, decimals0: int, decimals1: int) -> tuple[float, float] | None:
        """Returns (predicted_amount0_total, predicted_amount1_total) for the
        STRATEGY in DISPLAY UNITS (decimals applied), matching Beefy
        `balances()` semantics for direct verify comparison.

        Multiplication by user share happens in caller. Returns None if
        cache empty (caller falls back to Beefy actual)."""
        if self._cache is None:
            return None
        c = self._cache
        # positionMain contribution (raw)
        a0_main = _v3_amount0(c.L_main, p_now, c.p_a_main, c.p_b_main)
        a1_main = _v3_amount1(c.L_main, p_now, c.p_a_main, c.p_b_main)
        # positionAlt contribution (raw, if active)
        a0_alt = a1_alt = 0.0
        if c.L_alt is not None:
            a0_alt = _v3_amount0(c.L_alt, p_now, c.p_a_alt, c.p_b_alt)
            a1_alt = _v3_amount1(c.L_alt, p_now, c.p_a_alt, c.p_b_alt)
        # Scale raw → display units (Beefy.balances() returns same scaling)
        return (
            (a0_main + a0_alt) / (10 ** decimals0),
            (a1_main + a1_alt) / (10 ** decimals1),
        )

    def verify(self, predicted: tuple[float, float], actual: tuple[float, float]) -> float:
        """Returns max relative divergence across legs. If > DIVERGENCE_THRESHOLD,
        caller schedules cache refresh."""
        d0 = abs(predicted[0] - actual[0]) / max(actual[0], 1e-18)
        d1 = abs(predicted[1] - actual[1]) / max(actual[1], 1e-18)
        max_div = max(d0, d1)
        if max_div > DIVERGENCE_THRESHOLD:
            self._refresh_pending = True
        return max_div

    def should_refresh(self) -> bool:
        return self.cache_stale() or self._refresh_pending


def _v3_amount0(L: int, p: float, p_a: float, p_b: float) -> float:
    """V3 token0 amount in display units (decimals applied by caller via raw L scaling)."""
    if p >= p_b:
        return 0.0
    p_use = max(p, p_a)
    return float(L) * (1.0 / math.sqrt(p_use) - 1.0 / math.sqrt(p_b))


def _v3_amount1(L: int, p: float, p_a: float, p_b: float) -> float:
    if p <= p_a:
        return 0.0
    p_use = min(p, p_b)
    return float(L) * (math.sqrt(p_use) - math.sqrt(p_a))
```

**Unit convention.** `_v3_amount0/1` work in raw token units (matching Uniswap on-chain math). `HedgeModel.predict()` divides by `10^decimals` so it returns DISPLAY UNITS — same scaling as `BeefyClmReader.read_position()`. This makes verify a direct float comparison without unit conversion mistakes. The engine passes decimals from the existing reader.

### `engine/__init__.py:_iterate` — refactor

The existing `_iterate` (lines 873–1136) gets:

1. **Removed:** `_iterate_predictive`, `_fire_predictive_leg`, `_grid_stale`, `_refresh_grid`, `PredictiveUnavailable` block, `_grid` field, `_last_level_idx` field, fallback_reason scaffolding.

2. **Added:** `HedgeModel` instance attached to engine; cache refresh check at iter start; predicted-vs-actual verify; status published to `StateHub.hedge_model_status`.

3. **Kept intact:** `_maybe_rebalance_leg` (it's the canonical fire path — now called with `target = actual × share × hedge_ratio` always); position-truth flow; metric publishing.

Pseudocode of new `_iterate` body (omitting unchanged scaffolding):

```python
# Parallel reads (existing, with timeout=5s wrapping each)
slot0_task = asyncio.create_task(self._pool_reader.read_slot0())
beefy_task = asyncio.create_task(self._beefy_reader.read_position())
positions_task = asyncio.create_task(asyncio.gather(*[
    self._safe_get_position(s) for s in symbols
]))
sqrt_x96, current_tick = await asyncio.wait_for(slot0_task, timeout=5.0)
beefy_pos = await asyncio.wait_for(beefy_task, timeout=5.0)
positions = await positions_task
p_now = (sqrt_x96 / 2**96) ** 2

# L cache refresh (non-blocking — schedule, don't await fire on failure)
if self._hedge_model.should_refresh():
    asyncio.create_task(self._hedge_model.refresh_cache())

# Predict (informational — cache may be cold)
predicted = self._hedge_model.predict(
    p_now,
    decimals0=self._beefy_reader._decimals0,
    decimals1=self._beefy_reader._decimals1,
)
actual = (beefy_pos.amount0, beefy_pos.amount1)  # display units

# Verify (logs + schedules refresh on divergence; never blocks fire)
if predicted is not None:
    div = self._hedge_model.verify(predicted, actual)
    self._hub.hedge_model_status = (
        "active" if div <= 0.01 else f"verify_diverging:{div:.1%}"
    )
else:
    self._hub.hedge_model_status = "warming_up"

# Target ALWAYS uses authoritative actual
my_amount0 = actual[0] * beefy_pos.share
my_amount1 = actual[1] * beefy_pos.share
targets = {
    symbols[0]: my_amount0 * self._hub.hedge_ratio,
}
if is_dual_leg:
    targets[symbols[1]] = my_amount1 * self._hub.hedge_ratio

# Drift fire (existing _maybe_rebalance_leg, no behavior change)
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

### `state.py:StateHub` — `hedge_model_status` field

Replace `predictive_status` (currently set to `"fallback: predictive disabled (positionAlt unmodeled)"` constant) with `hedge_model_status`:

- `"warming_up"` — cache cold (first iter or after restart)
- `"active"` — cache fresh, divergence ≤ 1%
- `"verify_diverging:X%"` — divergence > 1%, refresh scheduled
- `"L_cache_stale_rpc_failed"` — refresh attempted, RPC failed, using stale cache

UI shows this in the operation card (small text). No new endpoint needed — already in SSE state stream.

## Anti-engasgo (no-freeze) measures

1. **All chain RPC calls wrapped in `asyncio.wait_for(..., timeout=5.0)`.** Timeout → log + skip iter, never await indefinitely.
2. **L cache refresh is `asyncio.create_task` — fire-and-forget.** The fire path uses whatever cache value exists at that moment. Refresh that fails or hangs doesn't block the loop.
3. **Verify divergence is informational** — sets `_refresh_pending`, never blocks fire.
4. **Iter body wrapped in `try/except Exception`** with `logger.exception` — any unexpected error logs full traceback and continues to next iter.
5. **Fire per leg is sequential** (Lighter nonce manager requires) — but each `place_long_term_order` already has internal timeout via the adapter.
6. **Position-truth (`_expected_short_size`) stamping is preserved** — the structural protection against over-hedge from 2026-05-07 redesign stays intact.
7. **New Prometheus alert:** `engine_iter_duration_seconds_p99 > 2.0` for 5 min triggers warning. (Metric already exists; alert config added.)

## Files added / modified / deleted

**Added:**
- `chains/v3_position.py` (~50 LoC)
- `engine/hedge_model.py` (~80 LoC)
- `tests/test_v3_position.py` (~5 tests)
- `tests/test_hedge_model.py` (~7 tests)

**Modified:**
- `engine/__init__.py` — `_iterate` body refactored (~80 LoC removed, ~40 LoC added). `__init__` adds `_hedge_model` attribute. `_safe_get_position` unchanged.
- `state.py` — rename `predictive_status` → `hedge_model_status` (field rename, default value change).
- `web/templates/partials/operation.html` — display `hedge_model_status` (1 line change).

**Deleted:**
- `engine/predictive_grid.py` (-161 LoC)
- `tests/test_predictive_grid.py`, `tests/test_predictive_engine.py`, `tests/test_predictive_grid_refresh.py` (~600 LoC of tests for v1 design — entire model gone)

**Net diff:** roughly −650 LoC removed, +200 LoC added = net **~−450 LoC**. Spec eliminates more than it adds.

## Test strategy (~12 new tests)

`tests/test_v3_position.py`:
1. `test_compute_position_key_matches_uniswap_format` — keccak(owner, int24, int24) byte-exact match to known V3 position key
2. `test_read_position_main_returns_liquidity_from_pool_storage` — mock w3, verify pool.positions called with right key, returns L
3. `test_read_position_alt_returns_none_when_inactive` — alt with tick_lower==tick_upper → None
4. `test_read_position_alt_returns_none_on_rpc_failure` — exception in alt read → None (not raised)
5. `test_read_position_main_propagates_strategy_failure` — strategy.positionMain() failure → exception bubbles (cache layer handles)

`tests/test_hedge_model.py`:
6. `test_predict_returns_none_when_cache_empty` — cold model returns None (caller uses actual)
7. `test_predict_main_only_when_alt_inactive` — cache with L_alt=None → predicted = main only
8. `test_predict_includes_alt_when_active` — both active → sum of contributions
9. `test_verify_returns_max_relative_divergence` — predicted vs actual, max(d0, d1) returned
10. `test_verify_schedules_refresh_when_divergence_exceeds_threshold` — div > 1% → `should_refresh()` = True
11. `test_refresh_cache_keeps_prior_on_rpc_failure` — refresh raises → cache unchanged, `_refresh_pending` resets
12. `test_cache_stale_after_ttl` — monotonic clock advance > 300s → stale True

Plus integration test in `tests/test_engine_dual_leg.py`:
13. `test_iterate_uses_actual_target_for_fire_even_when_predicted_diverges` — model predicted = wrong on purpose, actual = right; fire uses actual; warning logged. Regression for the "always trust authoritative source" invariant.

## Migration

1. Branch `feature/predictive-grid-v2` (current).
2. Implement T1–T8 (see plan, generated by `writing-plans` skill).
3. Each task TDD: failing test → implement → green → commit.
4. Live verify: restart uvicorn, watch `hedge_model_status` go `warming_up` → `active` → drift fires happen as expected.
5. Compare predicted vs actual via DB query: `SELECT * FROM operations WHERE id = 28` and engine logs.
6. Run full pytest suite; expect ~340+ tests verde.
7. Open PR (target: master); user reviews, merges if happy.
8. Post-merge: update CLAUDE.md status, WORKING_ON.md, memory entries.

## Risks

1. **`pool.positions(key)` ABI mismatch** — different Uniswap V3 pool versions may return different tuple shapes. Mitigation: use canonical V3 ABI committed to repo (`abi/uniswap_v3_pool.json`); test against op #28's pool address as smoke before deploy.
2. **Beefy strategy ABI for `positionAlt`** — may not exist on all CLM v2 strategies. Mitigation: `read_position_alt` returns None on any exception (already in design); engine handles `L_alt is None` cleanly.
3. **Decimal scaling of raw L** — V3 positions store liquidity raw; V3 amount formulas return raw amounts. `HedgeModel.predict()` performs the divide internally (see Components / Unit convention) so verify compares apples-to-apples with Beefy. Tests must cover an asymmetric-decimals pair (e.g. WETH 18 / USDC 6) to catch any off-by-decimals regression.
4. **Cold start on uvicorn restart** — first iter has no cache. Engine falls back to actual (Beefy direct) for that iter; refresh fires async; second iter has cache. Documented in `_iterate` flow above. No user-visible degradation.
5. **`positionAlt` flips active/inactive between iters** — Beefy can deploy/retire alt range mid-operation. Verify divergence catches this within one iter; refresh resets cache. Tested by integration test 13.
6. **Persistent verify divergence** — if Beefy returns garbage continuously, model never converges. Mitigation: `hedge_model_status` shows the state; user sees `"verify_diverging:X%"` and can stop the bot manually. No automatic disable (would mask real bugs).

## Verification (post-deploy live check)

User runs `start.bat`. Within 10 seconds in `uvicorn.log`:
- `HedgeModel.refresh_cache: L_main=<int>, L_alt=<int|None>` (cache populated)
- `hedge_model_status: warming_up → active`
- Drift fires within first minutes if hedge was off-target

Within first hour:
- `hedge_model_status` stays `"active"` (divergence < 1% steady state)
- Beefy harvest event (if any) shows brief `"verify_diverging:Y%"` followed by automatic refresh + return to `"active"`

Failure mode:
- If `hedge_model_status` stays `"warming_up"` > 30s → V3 RPC failing, check `ARBITRUM_RPC_URL`
- If status oscillates `"active"` ↔ `"verify_diverging:X%"` for >5 min → real divergence, investigate (Beefy upgrade? alt range churning?)
