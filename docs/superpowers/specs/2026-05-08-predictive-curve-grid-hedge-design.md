# Predictive Curve-Grid Hedge — Design Spec

**Date:** 2026-05-08 (rewrite of original two-grid draft per brainstorming)
**Status:** Approved through brainstorming (§1–§7)
**Branch:** `feature/cross-pair-dual-hedge`
**Depends on:** Position-truth redesign (commits 3e34ed9..3f9475c, spec 2026-05-07)

## Problem

The current engine fires hedge adjustments **reactively**: each iter computes drift between target (Beefy `my_amount × hedge_ratio`) and current (`get_effective_position`), and fires if `|drift| × ref_price ≥ $0.50`. This works but has limitations:

- **Reactive in absolute terms.** No concept of *where* the LP is on its V3 curve or *where it's going*. Each iter recomputes drift from scratch with no memory of prior level.
- **Order placement uses oracle mid + buffer (`ref_price × 0.999`/`× 1.001`)**, not the actual book bid/ask. On thin books or fast-moving markets, the order price may be off the actual cross-spread by more than the 0.1% buffer.
- **No coordination between legs.** Each leg fires independently when its drift hits floor — the two legs of a cross-pair can fire seconds apart even though both reflect the same underlying pool ratio movement.

## Goal

Replace the per-iter drift computation with a **pre-computed grid keyed on pool ratio `p`** (Uniswap slot0). When `p` crosses a level boundary, fire orders on *both* legs simultaneously at the **current bid/ask of the Lighter book** for the exact LP-exposure delta between the new level and the previously-tracked level. Granularity: **$0.50 of leg notional per level**.

The reactive engine stays as a fallback for situations the predictive path can't handle (warmup, RPC failures, empty book, grid stale during rebuild).

## Non-goals

- LP fees attribution / Beefy harvest event tracking (separate spec, deferred).
- Backtesting integration (predictive is validated live; backtest stays on reactive engine).
- Multi-vault parallel grid management (engine is per-vault).
- WebSocket subscription to Beefy strategy events (polling 60 s is adequate).
- Auto-disable of predictive after sustained failures (manual investigation if it stays in fallback >5 min).

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  ENGINE ITER (1 Hz)                                          │
│                                                              │
│  1. Read currentTick + sqrtPriceX96 from Uniswap pool        │
│  2. If grid is None or grid_stale (60s): _refresh_grid()     │
│  3. Map currentTick → level idx via bisect on p_levels       │
│  4. If first iter post-rebuild: snap idx, no fire (warmup)   │
│  5. If level changed: compute deltas per leg & fire          │
│  6. Update _last_level_idx                                   │
└────────────┬────────────────────────────────────────────────┘
             │ delta detected
             ▼
┌─────────────────────────────────────────────────────────────┐
│  PER-LEG EXECUTION                                           │
│                                                              │
│  For each leg (token0, token1):                              │
│    delta_amount = (amount[new] - amount[old]) × hedge_ratio  │
│    side = "sell" if delta > 0 else "buy"                     │
│    leg_notional_usd = abs(delta) × leg_USD_price             │
│    if leg_notional_usd < $0.50: skip THIS leg                │
│    else:                                                     │
│      price = book.best_bid (sell) or book.best_ask (buy)     │
│      place_long_term_order(symbol, side, size, price)        │
└────────────┬────────────────────────────────────────────────┘
             │ on PredictiveUnavailable (book empty, RPC down, …)
             ▼
┌─────────────────────────────────────────────────────────────┐
│  REACTIVE FALLBACK (existing _maybe_rebalance_leg)           │
│                                                              │
│  Same $0.50 floor. Uses oracle mid + buffer (legacy).        │
│  Engine NEVER runs both predictive AND reactive in same iter │
│  (hard guard via fired_predictive flag).                     │
└─────────────────────────────────────────────────────────────┘
```

### File structure

| File | Responsibility |
|---|---|
| `engine/predictive_grid.py` (NEW) | `LevelGrid` dataclass, `_build_grid()`, `find_level_idx()`, `compute_deltas()`. Pure functions over math + state. |
| `engine/__init__.py` | `_iterate()` rewritten: predictive primary path, reactive fallback. Adds `_grid`, `_last_level_idx`, `_last_grid_check_at` slots. |
| `state.py` | New field `predictive_status: str` for UI to show current mode. |
| `tests/test_predictive_grid.py` (NEW) | Grid construction + level mapping unit tests. |
| `tests/test_predictive_engine.py` (NEW) | Engine integration tests: trigger detection, bid/ask execution, fallback. |
| `tests/test_predictive_grid_refresh.py` (NEW) | Re-grid polling behavior. |

## Components

### `LevelGrid` dataclass + construction

```python
@dataclass
class LevelGrid:
    p_a: float                # raw price = ARB/WETH at lower bound
    p_b: float                # raw price at upper bound
    L: float                  # V3 liquidity
    p_levels: list[float]     # sorted ascending [p_a, p_1, p_2, ..., p_b]
    amount0_at: list[float]   # token0 (WETH) amount at each level
    amount1_at: list[float]   # token1 (ARB) amount at each level
    tick_lower: int           # source of truth from Beefy positionMain
    tick_upper: int


async def _build_grid(self, tick_lower: int, tick_upper: int) -> LevelGrid:
    """Build a fresh grid from current Beefy tick range and live prices.

    Discretizes [p_a, p_b] adaptively: each adjacent level pair must produce
    ≥$0.50 in at least one leg's notional at current USD prices. Granularity
    is therefore variable — denser where one leg moves slowly per Δp,
    sparser where it moves fast.

    Math (V3, valid for p ∈ [p_a, p_b]):
        amount0(p) = L × (1/√p − 1/√p_b)
        amount1(p) = L × (√p − √p_a)

    For p ≤ p_a: amount0 = L × (1/√p_a − 1/√p_b), amount1 = 0
    For p ≥ p_b: amount0 = 0, amount1 = L × (√p_b − √p_a)
    """
```

### Level mapping (`find_level_idx`)

```python
def find_level_idx(grid: LevelGrid, p_now: float) -> int:
    """Returns index k such that p_levels[k] ≤ p_now < p_levels[k+1].

    Below p_a → 0 (edge: full token0). Above p_b → len-1 (edge: full token1).

    Uses bisect for O(log N).
    """
    if p_now <= grid.p_levels[0]: return 0
    if p_now >= grid.p_levels[-1]: return len(grid.p_levels) - 1
    return bisect.bisect_right(grid.p_levels, p_now) - 1
```

### Delta computation

```python
def compute_deltas(grid: LevelGrid, old_idx: int, new_idx: int) -> tuple[float, float]:
    """Returns (delta_amount0, delta_amount1) for transition old_idx → new_idx.

    Positive delta = LP gained that token = need to short MORE.
    Multi-level jumps use direct diff between endpoints, NOT sum of intermediates
    (otherwise multi-level moves would mass-fire).
    """
    return (
        grid.amount0_at[new_idx] - grid.amount0_at[old_idx],
        grid.amount1_at[new_idx] - grid.amount1_at[old_idx],
    )
```

### Per-iter predictive logic (`_iterate_predictive`)

```python
async def _iterate_predictive(self) -> bool:
    """Returns True if predictive ran cleanly (regardless of whether it fired).
    Raises PredictiveUnavailable if predictive can't run this iter — caller
    falls back to reactive.
    """
    # Read pool state (Uniswap slot0). UniswapV3PoolReader.read_slot0()
    # returns (sqrt_price_x96, current_tick) tuple — see chains/uniswap.py:40.
    try:
        sqrt_price_x96, current_tick = await self._pool_reader.read_slot0()
        p_now = (sqrt_price_x96 / 2**96) ** 2  # raw ARB/WETH ratio
    except Exception as e:
        raise PredictiveUnavailable(f"slot0 read failed: {e}")

    # Map p → level idx
    new_idx = find_level_idx(self._grid, p_now)

    # Warmup: first iter post-rebuild
    if self._last_level_idx is None:
        self._last_level_idx = new_idx
        return True  # ran cleanly, just no fire

    # No level change
    if new_idx == self._last_level_idx:
        return True

    # Compute deltas with hedge_ratio applied
    delta_t0, delta_t1 = compute_deltas(
        self._grid, self._last_level_idx, new_idx,
    )
    delta_t0 *= self._hub.hedge_ratio
    delta_t1 *= self._hub.hedge_ratio

    # Fire each leg sequentially (Lighter nonce manager races on parallel)
    await self._fire_predictive_leg(self._settings.dydx_symbol_token0, delta_t0)
    await self._fire_predictive_leg(self._settings.dydx_symbol_token1, delta_t1)

    self._last_level_idx = new_idx
    return True
```

### Per-leg fire (`_fire_predictive_leg`)

```python
async def _fire_predictive_leg(self, symbol: str, delta: float) -> None:
    """Place taker order at current bid (sell) or ask (buy) for `delta` size.
    Skips silently if leg notional < $0.50. Raises PredictiveUnavailable
    if book is empty for this market.
    """
    if abs(delta) < 1e-12:
        return

    side = "sell" if delta > 0 else "buy"
    size = abs(delta)

    # Engine already resolves _token0_mid / _token1_mid at startup
    # (funding accumulator wiring, commit a571603). Reuse that.
    market_id = (
        self._token0_mid if symbol == self._settings.dydx_symbol_token0
        else self._token1_mid
    )
    if market_id is None:
        raise PredictiveUnavailable(f"market_id unresolved for {symbol}")
    book = self._exchange._ws_book_top.get(market_id)
    if not book or not book.get("best_bid") or not book.get("best_ask"):
        raise PredictiveUnavailable(f"book empty for {symbol} (mid={market_id})")

    price = book["best_bid"] if side == "sell" else book["best_ask"]
    leg_notional_usd = size * price

    if leg_notional_usd < self._settings.min_rebalance_notional_usd:
        logger.debug(
            f"Predictive: skip {symbol} leg, ${leg_notional_usd:.4f} < $0.50"
        )
        return

    cloid = self._next_cloid_for_leg(symbol)
    await self._exchange.place_long_term_order(
        symbol=symbol, side=side, size=size, price=price,
        cloid_int=cloid, ttl_seconds=60,
    )
    logger.info(
        f"Predictive fire [{symbol}]: {side} {size:.6f} @ {price:.6f} "
        f"(${leg_notional_usd:.2f}, level {self._last_level_idx} → ?)"
    )
```

### Re-grid polling (`_refresh_grid`, `_grid_stale`)

```python
_GRID_CHECK_INTERVAL_S = 60.0

def _grid_stale(self) -> bool:
    return (time.monotonic() - self._last_grid_check_at) > self._GRID_CHECK_INTERVAL_S

async def _refresh_grid(self) -> None:
    """Polls Beefy strategy.positionMain() and rebuilds grid if tick range
    changed. Atomic: keeps existing _grid intact on RPC failure.
    Resets _last_level_idx to None on rebuild → next iter snaps (no fire).
    """
    try:
        position_main = await self._beefy_reader._strategy.functions.positionMain().call()
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

    try:
        new_grid = await self._build_grid(new_lower, new_upper)
    except Exception as e:
        logger.exception(f"_build_grid failed: {e}")
        return

    old_range = (self._grid.tick_lower, self._grid.tick_upper) if self._grid else None
    self._grid = new_grid
    self._last_level_idx = None
    logger.info(
        f"Grid rebuilt: range {old_range} → ({new_lower}, {new_upper}), "
        f"{len(new_grid.p_levels)} levels"
    )
```

### Coexistence with reactive (`_iterate` rewrite)

```python
async def _iterate(self):
    # ... existing chain reads, target compute, etc ...

    fired_predictive = False
    fallback_reason = None

    try:
        if self._grid is None or self._grid_stale():
            await self._refresh_grid()

        if self._grid is not None:
            await self._iterate_predictive()  # raises PredictiveUnavailable on issue
            fired_predictive = True
            self._hub.predictive_status = "active"
        else:
            self._hub.predictive_status = "no_grid"
            fallback_reason = "grid not built"
    except PredictiveUnavailable as e:
        fallback_reason = str(e)
    except Exception as e:
        logger.exception(f"Predictive failed unexpectedly: {e}")
        fallback_reason = f"unexpected: {type(e).__name__}"

    if fallback_reason is not None:
        self._hub.predictive_status = f"fallback: {fallback_reason}"
        # Existing reactive path
        for sym in symbols:
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

`fired_predictive` flag + early return path (via `if fallback_reason is not None`) is the **hard guard** preventing both predictive and reactive from firing in the same iter. Test #13 covers it.

## Out-of-range handling

V3 amounts are deterministic on the boundary (already in §Components > construction). `find_level_idx` returns 0 (bottom) or `len-1` (top) when `p_now` is outside `[p_a, p_b]`. Edge level amounts:

- `amount0_at[0] = L × (1/√p_a − 1/√p_b)`, `amount1_at[0] = 0`
- `amount0_at[-1] = 0`, `amount1_at[-1] = L × (√p_b − √p_a)`

**Behavior in OOR:**

1. Engine maps to edge level once (single fire to position).
2. Subsequent iters with `p_now` still OOR: no level change → no fire.
3. When `p_now` re-enters range: level change detected → normal fire flow.
4. When Beefy re-rebalances to a new tick range covering current `p_now`: §Re-grid polling catches it → grid rebuild → snap → next iter normal.

During OOR + before Beefy re-rebalances, the LP literally doesn't move (single-asset, no V3 trades), so freezing the hedge is correct.

## Sign convention

- `delta > 0` → LP gained that token → SELL more on perp (open more short)
- `delta < 0` → LP lost that token → BUY back on perp (close some short)
- Order price: `book.best_bid` (sell) or `book.best_ask` (buy) — taker on Lighter (zero fee).

## Risks

1. **Beefy CLM v2 ABI shape variation**: `positionMain()` may return `((tickLower, tickUpper), (amount0, amount1), ...)` (current ABI) or differ on other vault types. Spec assumes `position_main[0]` indexing. Mitigation: log raw response if `int(...)` fails; user adjusts.

2. **Rebuild during volatile movement**: tick changes fast while rebuild runs. Mitigation: `_last_level_idx = None` after rebuild → snap (no spurious fire). 1-2 iter no-fire window acceptable.

3. **RPC throttling**: `positionMain` adds ~60 calls/hour to existing `slot0()` (1 Hz) + Beefy balances (1 Hz). Total ~3 calls/sec — well within public Arbitrum RPC limits.

4. **Hedge_ratio change mid-op**: deltas use `self._hub.hedge_ratio` at fire time, so a mid-op change applies to all future fires. Position drifts gradually over level crossings to the new ratio. Acceptable design.

5. **Position-truth interaction**: `_expected_short_size` stamping is unchanged — `place_long_term_order` always stamps on success. Hard guard prevents predictive + reactive double-stamping in the same iter.

6. **Build computation**: 200-level grid × V3 math < 10 ms. Not blocking. If gets slow (>50 ms) move to `asyncio.to_thread`.

7. **OOR + Beefy frozen**: bot waits indefinitely on edge level. UI shows status `OOR — single asset, hedge frozen`.

8. **Cumulative refresh failure**: 10 consecutive `_refresh_grid()` failures (10 min) → `self._grid = None` → permanent reactive fallback until issue resolves. Alert webhook fires at 5 failures.

## Testing

`tests/test_predictive_grid.py` (NEW):

1. `test_build_grid_levels_spaced_by_dollar_notional`
2. `test_build_grid_includes_p_a_and_p_b_as_endpoints`
3. `test_build_grid_amounts_match_v3_formula`
4. `test_find_level_idx_below_p_a_returns_zero`
5. `test_find_level_idx_above_p_b_returns_last`
6. `test_find_level_idx_in_range_uses_bisect`
7. `test_compute_deltas_handles_multi_level_jump`

`tests/test_predictive_engine.py` (NEW):

8. `test_iterate_predictive_first_iter_snaps_no_fire`
9. `test_iterate_predictive_level_change_fires_both_legs_at_book_prices`
10. `test_iterate_predictive_skips_leg_below_dollar_floor`
11. `test_iterate_predictive_empty_book_raises_predictive_unavailable`
12. `test_iterate_falls_back_to_reactive_when_predictive_unavailable`
13. `test_iterate_does_not_double_fire_predictive_and_reactive`

`tests/test_predictive_grid_refresh.py` (NEW):

14. `test_refresh_grid_rebuilds_when_tick_range_changes`
15. `test_refresh_grid_skips_when_unchanged`
16. `test_refresh_grid_keeps_old_grid_on_rpc_failure`

## Verification (post-deploy)

1. Restart uvicorn with new code.
2. Logs should show `Grid built: range (-81121, -76012), N levels` at startup.
3. Wait 5 min. Logs should show `Predictive fire [ETH-USD]:` and/or `[ARB-USD]:` as `p` moves.
4. Compare to Beefy display: bot's short position should track 98% of LP `amount0/amount1` within cents.
5. Manually open/close small tx on Lighter → bot recomputes via reactive fallback (predictive sees inconsistency → fallback runs).
6. Watch `state.predictive_status` in dashboard: should be `active` most of the time, `fallback: ...` rarely (with reason).
