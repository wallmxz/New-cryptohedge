# Predictive Curve-Grid Hedge — Design Spec

**Date:** 2026-05-08
**Status:** Draft — **needs single-grid rewrite before planning** (see below)
**Branch:** `feature/cross-pair-dual-hedge`
**Depends on:** Position-truth redesign (commits 3e34ed9..3f9475c, spec 2026-05-07)

## Handoff note (2026-05-08, end of context window)

This spec currently describes **two grids** per leg (`_grid_eth`, `_grid_arb`). User asked "pq 2 grids?" and I (assistant) acknowledged that's over-engineered. Agreed direction is a **single grid** keyed on pool ratio `p`, with two parallel amount arrays — one per leg:

```
LevelGrid:
    p_a, p_b, L
    p_levels: [p1, p2, p3, ...]
    amount0_at: [...]    # WETH at each level
    amount1_at: [...]    # ARB at each level
```

Each level crossing checks both legs independently and fires per-leg if `|delta| × leg_price ≥ $0.50`. Spec body still says "per leg" grid in places — those need to be rewritten before this can be turned into a plan. The *math* (V3 curve formulas, $0.50 floor, pool-ratio trigger source) is correct; it's just the data-structure description that needs to collapse from two grids into one.

**Next steps when resuming:**
1. Rewrite this spec to single-grid (replace "Grid construction (per leg)" + "Trigger logic" sections, keep math + WAF/race-safety sections as-is)
2. User reviews rewritten spec
3. Invoke `superpowers:writing-plans` for implementation plan
4. Execute via `superpowers:subagent-driven-development`
5. **Then** address the deferred PnL breakdown card bug (operation card shows $0.00 across LP fees, IL natural, Hedge PnL, Funding, Perp Fees, Bootstrap Slippage — needs its own brainstorm/spec)


## Problem

The current engine fires hedge adjustments only when `|drift| × ref_price ≥ min_notional`. The min_notional comes from Lighter's declared `min_quote_amount = $10`, which means the LP's exposure must change by $10 (~20% of a $50 position) before any rebalance fires. In practice the user observed multiple periods where the LP composition shifted visibly on Beefy yet zero rebalance orders appeared on Lighter — large unhedged windows during which IL accumulates.

The deeper issue: the engine is **reactive in absolute terms** ("how much have we drifted from target right now?"). It has no concept of *where* the LP is on its V3 curve or *where it is going* as the pool ratio moves. The `compute_curve_preview` already computes the full V3 amount-vs-price curve for the dashboard, but that information isn't used for execution.

## Goal

Minimize residual IL by **tracking the V3 curve** at fine granularity. Pre-compute a grid of pool-ratio levels along the curve. As the on-chain pool ratio moves, fire a market order for the exact LP-exposure delta between the level we just crossed and the level we were on before. Granularity = **$0.50 of leg notional per level**, ~20× finer than today's $10 reactive threshold.

## Why pool ratio (`p`) as the trigger source

The Beefy CLM holds a Uniswap V3 position. The V3 math is parameterized **purely by pool ratio** `p = amount1 / amount0` (for our pair, ARB per WETH). At any `p`:

```
x(p) = L · (1/√p − 1/√p_b)        # WETH amount in LP
y(p) = L · (√p − √p_a)             # ARB amount in LP
```

where `[p_a, p_b]` is the active range and `L` is the strategy's liquidity. Every other observable (token USD prices, Lighter's perp prices, oracle midpoints) is *derived* from this — and any drift from `p` to a derived signal introduces approximation. Reading `p` directly from the Uniswap pool slot0 at 1 Hz is the most accurate signal we have for predicting LP composition changes. Lighter perps trade in USD, but the **trigger** is `p`; the orders themselves are sized in token base units (already independent of USD price).

## Design — predictive grid

The engine holds a precomputed `LevelGrid` per leg. Each grid is a sorted list of `(p_level, amount_at_level)` tuples. As `p` advances, the engine maps `p` to a grid index and fires a market order for the cumulative amount delta between the previous index and the new one.

### Grid construction

For each leg (token0 / token1), discretize `[p_a, p_b]` into levels spaced by **$0.50 of leg notional** at the *current* USD price of that leg:

```
step_eth_size  = $0.50 / P_ETH_USD     # ≈ 0.000219 ETH at $2280/ETH
step_arb_size  = $0.50 / P_ARB_USD     # ≈ 3.9 ARB at $0.128/ARB
```

Walk the curve from `p_a` to `p_b`. Append a new level whenever the leg's amount has changed by `step_<leg>_size` since the last level. Final list:

```
grid_eth = [
  (p=17026, amount0=0.0312),  # 100% WETH at p_a
  (p=17061, amount0=0.0310),  # 0.000219 ETH delta
  (p=17097, amount0=0.0308),
  ...
  (p=19782, amount0=0.0000),  # 0% WETH at p_b
]
grid_arb = [
  (p=17026, amount1=0.0),     # 0% ARB at p_a
  (p=17041, amount1=3.91),    # 3.9 ARB delta
  ...
  (p=19782, amount1=574.2),   # 100% ARB at p_b
]
```

Both grids span `[p_a, p_b]`. ETH grid is **decreasing** in amount as `p` grows (curve sells WETH for ARB). ARB grid is **increasing**.

Grids are recomputed when **any of** `p_a`, `p_b`, or `L` changes (Beefy auto-rebalance, deposit, withdrawal). Otherwise they live in memory across iterations. Recompute is cheap (O(N) curve evaluations, N ≈ 100–200) and runs at most a few times per session.

### Execution logic — replaces `_maybe_rebalance_leg`

State per leg (kept in `StateHub` or engine local):

- `last_level_idx`: integer index into the grid corresponding to the last `p` we processed
- `target_short_at_last_level`: the cumulative target short size at that level

Each iter:

```
p_now = pool_reader.read_price()

for each leg:
    grid = self._grid[leg]
    if grid is None or grid.invalid_for(p_a, p_b, L):
        grid = build_grid(leg, p_a, p_b, L, P_leg_usd)
        self._grid[leg] = grid

    new_idx = grid.bisect_right(p_now)  # which interval p_now falls in
    if new_idx == last_level_idx[leg]:
        continue                                # no level crossed

    target_amount_now = grid[new_idx].amount * hedge_ratio
    target_amount_was = grid[last_level_idx[leg]].amount * hedge_ratio
    delta_amount      = target_amount_now - target_amount_was

    if abs(delta_amount) * P_leg_usd < MIN_NOTIONAL_USD:
        # crossed a sub-level (e.g. delta < step_size). Skip this iter,
        # the delta accumulates against last_level_idx.
        continue

    side = "sell" if delta_amount > 0 else "buy"
    await self._exchange.place_long_term_order(
        symbol=leg_symbol, side=side, size=abs(delta_amount),
        price=0,  # ignored — adapter reads bid/ask from WS cache
        cloid_int=self._next_cloid_for_leg(leg_symbol),
        ttl_seconds=60,
    )
    last_level_idx[leg] = new_idx
```

Notes:

- The **delta is cumulative** across skipped levels. If `p` jumps from index 5 to index 12 in one tick (network gap, fast move), one order fires for the full `target[12] − target[5]` swing. We never lose levels.
- The `MIN_NOTIONAL_USD = $0.50` floor matches the user's stated Lighter floor. Fires below it are deferred until the cumulative delta exceeds it.
- `ref_price` for the floor check uses the WS-cached USD oracle (already published on `hub.token0_usd_price` / `token1_usd_price`).

### Reuse of position-truth redesign

This design **replaces only the trigger logic in `_maybe_rebalance_leg`**. Everything below it stays:

- `place_long_term_order` stamps `_expected_short_size` on `err is None` (Task 3 of the prior redesign)
- `get_effective_position` is *not* read here — the new logic is pure predictive ("here's the next target the curve says we should be at"). The previous redesign's role was to prevent over-hedge from racing with WS lag; with the predictive logic, *we don't ask the adapter what our position is at all*. We only fire deltas based on level transitions, and `_expected_short_size` accumulates them. The reconciler still resolves divergence at 30 s timeout.
- Per-leg cooldown: removed (subsumed by the level-crossing gate — we never fire twice on the same level).

### Sign convention

- `amount0` is WETH. As `p` increases, WETH leaves the pool (sold for ARB). `target_short_eth(p)` decreases. Crossing UP → engine **buys ETH** to cover part of the short. Crossing DOWN → engine **sells more ETH**.
- `amount1` is ARB. Symmetric.
- The `delta_amount > 0 → side = "sell"` rule above produces the correct side because `target_amount_now − target_amount_was` carries the sign of the change in amount, not in `p`.

### Edge cases

- **Out of range** (`p < p_a` or `p > p_b`): the LP holds 100% of one token. The grid endpoints clamp to `(p_a, full_token0)` and `(p_b, 0)`. Once `p` exits the range and stays out, no further crossings → no fires. When `p` re-enters, the level-crossing logic resumes correctly.
- **Beefy auto-rebalance** (range shifts to a new `[p_a', p_b']`): detected by comparing the live `tick_lower / tick_upper` from `BeefyClmReader` against the cached grid's range. On mismatch, rebuild both grids and reset `last_level_idx` to the level matching the current `p`. Any in-flight delta from the old grid is left to the reconciler (HTTP truth at 30 s).
- **Stale grid after price moves** (USD prices drift, so `step_<leg>_size` shifts): grids stay valid because they're keyed by `p`, not by USD. We do recompute when the LP's `L` changes (deposit/withdraw) since amounts at every `p` change. ETH/ARB USD price changes only affect the **next grid build's** `step_size`, not the current grid's level positions.
- **First iter after engine start with an existing operation**: `last_level_idx` is unknown. Initialize it to `bisect(grid, p_now)` without firing — we adopt the current LP exposure as the baseline and only fire on subsequent crossings.
- **Skip-level on very fast moves**: cumulative delta is summed into one market order; no level loss.
- **Grid with zero levels** (`L = 0` or range collapsed): no-op, log a warning.

## Architecture

```
┌──────────────────────┐
│  pool_reader.read_   │   (existing, on-chain Uniswap slot0)
│  price() → p_now     │
└──────────┬───────────┘
           │
           ▼
┌─────────────────────────┐
│  GridManager (NEW)      │
│  · build_grid(p_a,p_b,L,│
│    leg, P_leg_usd)      │
│  · bisect(p_now)        │
│  · invalid_for(...)     │
└──────────┬──────────────┘
           │
           ▼
┌─────────────────────────────┐
│  _iterate (MODIFIED)        │
│  for leg in legs:           │
│    crossed = grid.bisect... │
│    if crossed:              │
│      delta = target_now -   │
│              target_was     │
│      place_long_term_order  │
└──────────┬──────────────────┘
           │
           ▼
┌──────────────────────────┐
│  LighterAdapter          │  (existing, from 2026-05-07 redesign)
│  · stamps expected       │
│  · reconciler resolves   │
│    divergence on 30 s    │
└──────────────────────────┘
```

## State

New on `GridMakerEngine`:

| Field | Type | Purpose |
|---|---|---|
| `_grid_eth` | `LevelGrid \| None` | precomputed ETH amount levels keyed by p |
| `_grid_arb` | `LevelGrid \| None` | precomputed ARB amount levels keyed by p |
| `_last_level_idx` | `dict[str, int]` | per-leg index into its grid |

`LevelGrid` is a small dataclass:

```python
@dataclass
class LevelGrid:
    leg_symbol: str          # "ETH-USD" or "ARB-USD"
    p_a: float               # range floor (cached for invalidation)
    p_b: float               # range ceiling
    L: float                 # liquidity (cached for invalidation)
    p_levels: list[float]    # sorted, length N
    amounts: list[float]     # length N, amount of token at each p_level
    hedge_ratio: float       # cached, rebuild if changed

    def bisect_right(self, p: float) -> int: ...
    def invalid_for(self, p_a, p_b, L, hedge_ratio) -> bool: ...
```

Removed:

- `min_notional` from `_maybe_rebalance_leg`'s argument list (now a constant `MIN_NOTIONAL_USD = 0.50` at module level)
- The `drift = target - current` computation entirely

## API

The `_iterate` loop replaces its current per-leg block:

```python
# Before (reactive):
for sym in symbols:
    meta = await self._exchange.get_market_meta(sym)
    idx = symbols.index(sym)
    current = abs(positions[idx].size) if positions[idx] else 0.0
    ref_price = oracle_prices.get(sym, 0.0)
    if ref_price <= 0:
        continue
    await self._maybe_rebalance_leg(
        symbol=sym, target=targets[sym], current=current,
        min_notional=meta.min_notional, ref_price=ref_price,
    )

# After (predictive):
for sym in symbols:
    leg_amount = my_amount0 if sym == settings.dydx_symbol_token0 else my_amount1
    ref_price = oracle_prices.get(sym, 0.0)
    if ref_price <= 0:
        continue
    await self._maybe_advance_grid(
        symbol=sym, leg_amount_now=leg_amount,
        p_now=p_now, p_a=p_a, p_b=p_b, L=L_user,
        ref_price=ref_price,
    )
```

`_maybe_advance_grid` is the new method that contains the level-crossing logic. `_maybe_rebalance_leg` is removed.

## Constants

- `MIN_NOTIONAL_USD = 0.50` — module-level constant in `engine/__init__.py`. Floor for any single market order. Subject to user override at runtime via settings if desired (out of scope for v1).

## Test strategy

Unit tests against the `GridMakerEngine` and a new `engine/grid.py` module:

1. **`test_build_grid_eth_descending`**: with `p_a=17000, p_b=20000, L=1000, P_eth_usd=2280`, verify generated grid has `amounts[0] = x(p_a, p_b, L)` and `amounts[-1] ≈ 0`, length matches expected step count, monotone decreasing.

2. **`test_build_grid_arb_ascending`**: same fixture, verify `amounts[0] ≈ 0` and `amounts[-1] = y(p_a, p_b, L)`, monotone increasing.

3. **`test_bisect_returns_correct_index`**: feed a small grid `[17000, 17500, 18000, 18500, 19000]`, call `bisect_right(p_now)` for `p_now ∈ {16800, 17000, 17499, 17500, 18001, 19500}`, verify each.

4. **`test_grid_invalidates_on_range_change`**: build with `p_a=17000`, then `invalid_for(p_a=17100, ...)` returns True. Same `p_a` returns False.

5. **`test_advance_grid_no_fire_within_level`**: place engine with `last_level_idx=5`, `p_now` still in interval 5 → assert no `place_long_term_order` call.

6. **`test_advance_grid_fires_on_level_cross`**: `last_level_idx=5`, `p_now` advances to interval 7 → fires market order for `amounts[7] − amounts[5]`. Side and size verified.

7. **`test_advance_grid_skip_levels`**: `last_level_idx=5`, `p_now` jumps to interval 12 (network gap) → fires single order for `amounts[12] − amounts[5]`, NOT 7 separate orders.

8. **`test_advance_grid_below_min_notional_defers`**: small `p` move where `|delta| × ref_price < $0.50` → no fire, `last_level_idx` UNCHANGED so the cumulative delta keeps accruing.

9. **`test_grid_recomputed_on_beefy_rebalance`**: simulate `p_a/p_b/L` change, verify next iter rebuilds grid, resets `last_level_idx` to current p.

10. **`test_advance_grid_first_iter_baseline`** (regression): on engine startup with an existing operation, `last_level_idx` is None → first iter sets it to `bisect(p_now)` and fires NO order (we adopt current LP as baseline).

11. **`test_engine_does_not_double_fire_during_ws_lag`** (regression from prior redesign): kept as-is — predictive logic still relies on `_expected_short_size` stamping for over-hedge protection.

12. **`test_reconciler_resets_grid_index_on_truth_correction`**: simulate fires that didn't actually fill (truth lower than expected). Reconciler timeout fires, HTTP truth lower than `_expected_short_size`. Verify `_last_level_idx` realigns to the grid index whose target matches truth. Without this, grid index stays ahead and subsequent fires miss the actual gap.

## Known risks

**Reconciler-vs-grid-index desync.** When the reconciler resets `_expected_short_size` to HTTP truth (after a 30 s divergence timeout), it doesn't touch `_last_level_idx`. If a series of fires returned `err is None` but didn't actually fill (e.g. silent IOC auto-cancel that the cloid lookup never saw), bot's `_last_level_idx` advances while truth stays put. Reconciler corrects expected; grid index stays ahead. Next iter computes delta from the (advanced) grid index, missing the actual gap.

**Mitigation.** When reconciler resets `_expected_short_size[mid]` to `truth`, also realign `_last_level_idx[leg]` to `bisect(grid, target_for_amount(truth))`. One-line addition to `_reconcile_once` after the truth pin. Costs nothing in the steady case, prevents the desync after a real failure.

**Scope.** The mitigation is part of this design (i.e. v1 includes it). Test 9 (`test_grid_recomputed_on_beefy_rebalance`) exercises grid invalidation; an additional test should cover the reconciler-realign path.

## Out of scope

- **Pre-placing LIMIT orders on Lighter** (vs market on cross): considered and rejected. The grid is keyed by pool ratio `p`, not by Lighter's USD price. Pre-placing would require projecting `p`-levels onto USD-price levels per leg, which assumes the *other* leg's USD price is constant — an approximation that breaks when both prices move. Pure trigger-on-cross is cleaner and the latency penalty (~1 s polling) is small compared to typical pool-ratio drift.
- **Adaptive granularity** (denser near current `p`, sparser at the tails): considered. Skipped for v1 — uniform $0.50 spacing is simpler and the IL difference is marginal at 100+ levels.
- **Simulator/backtest integration**: out of scope for the live-bot change. Backtest will need to be updated separately to use the same grid logic (otherwise its `_iterate` divergence would skew results).
- **Settings UI for `MIN_NOTIONAL_USD`**: out of scope. Hardcoded constant for v1.
- **Market-order pricing strategy** (which side of book, slippage cap): unchanged — `place_long_term_order` already does IOC LIMIT at exact bid/ask via the WS cache (per the prior redesign).

## Migration

Single PR on `feature/cross-pair-dual-hedge`:

1. Add `engine/grid.py` with `LevelGrid` dataclass and `build_grid` function. Pure function, fully unit-tested.
2. Add `_grid_eth`, `_grid_arb`, `_last_level_idx` fields to `GridMakerEngine.__init__`.
3. Add `_maybe_advance_grid` method to engine.
4. Modify `_iterate` to call `_maybe_advance_grid` per leg instead of `_maybe_rebalance_leg`.
5. Remove `_maybe_rebalance_leg` and `_aggressive_correct` (already removed in prior cleanup) and any stale references.
6. Add `MIN_NOTIONAL_USD = 0.50` constant.
7. Tests: 11 cases above. Existing `test_engine_does_not_double_fire_during_ws_lag` already covers the safety guarantee from the prior redesign and stays valid.

No DB schema changes. No `.env` changes. UI continues to display the same curve preview (it already uses `compute_target_grid` which is unchanged — that grid is for visualization, not execution; the new `LevelGrid` in `engine/grid.py` is independent).

## Migration impact

After this lands:
- Order rate during steady markets: ~0–5 fires per minute (depends on `p` volatility within the range)
- Order rate during fast moves: bounded by `(N_levels × hedge_ratio)` per range traversal
- Each fire ≈ $0.50 of leg notional (Lighter is zero-fee, so cost is just the spread, which `place_long_term_order` minimizes by hitting bid/ask exactly)
- Residual IL between fires: ≤ $0.50 of leg notional + 1 s of price drift
- Old reactive logic: gone. The `min_notional = $10` threshold that suppressed rebalances is gone.
