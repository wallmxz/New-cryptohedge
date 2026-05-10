# Adapter-owned Position Truth Redesign — Design Spec

**Date:** 2026-05-07
**Status:** Draft (pending user review)
**Branch:** `feature/cross-pair-dual-hedge`
**Trigger:** 3 over-hedge incidents in a single session (ops #25, #26, #27),
all caused by the bot firing duplicate hedge orders because it could not
reliably tell whether a previous order had filled.

## Problem

The hedge bot fires taker orders to maintain `short_size = lp_amount × hedge_ratio`.
Each iter (1 Hz) it computes `drift = target - current_short_size` and, if
the drift exceeds `min_notional`, fires another order. The mechanism breaks
when `current_short_size` reads as 0 right after a fill — the bot believes
the previous order failed and fires another, stacking positions.

Three sources of "current size" all proved unreliable:

1. **HTTP `_verify_fill` via `/account_inactive_orders`** — eventually-consistent;
   typically 2 s lag, observed up to 10 s under load.
2. **WS `account_all` cache** — push-based; expected sub-second but observed
   up to 30 s+ during today's incidents.
3. **HTTP `/account` direct query** — slightly faster than `inactive_orders`
   but still eventually-consistent.

Patches tried in this session — buffer on IOC LIMIT, 30 s cooldown,
600 ms inter-order gap — all addressed adjacent symptoms but **not the
core race**: between "order accepted by exchange" and "exchange state
reflects fill in any reader the bot consults", the bot acts on stale data.

## Goal

Make over-hedge structurally impossible by giving the bot a single
authoritative position-size accessor that **never reports zero right
after a successful fire**, while preserving correct behavior when an
order genuinely doesn't fill (book moved, IOC auto-cancel).

## Design — adapter-owned position truth

The `LighterAdapter` becomes the sole owner of "what's my short size
on this symbol". It exposes one accessor — `get_effective_position(symbol)` —
that fuses three internal layers:

1. **`_observed_short_size[mid]`** — unsigned magnitude, written from WS
   `account_all` snapshots. Truth-eventually, lags actual exchange state.
2. **`_expected_short_size[mid]`** — unsigned magnitude, delta-stamped
   on every `place_long_term_order` where `create_order` returned
   `err=None`. Optimistically credits the order as filled, **independent
   of `_verify_fill` or the returned `Order.size`** — that's what makes
   the design over-hedge-proof.
3. **HTTP authoritative reconciliation** — runs in the background;
   resolves persistent divergence between observed and expected by
   querying `AccountApi.account` and pinning both to that result.

The accessor returns a `Position` with `size = max(observed, expected)` —
"what we have, or what we think we just acquired". Engine uses this
accessor for drift; lifecycle uses `place_long_term_order` (which
auto-stamps expected) — neither needs to know about lag, race, or
HTTP/WS layers.

## Architecture

```
   create_order(err=None)                       Lighter WS account_all
            │                                              │
            ▼                                              ▼
   ┌─────────────────────┐                     ┌─────────────────────┐
   │ _expected_short_size│                     │ _observed_short_size│
   │   (delta-stamped)   │                     │  (snapshot pushes)  │
   └─────────────────────┘                     └─────────────────────┘
            │                                              │
            └────────────► get_effective_position() ◄──────┘
                                     │
                                     ▼  size = max(observed, expected)
                    Engine `_safe_get_position` (drift math)
                    Lifecycle `_open_short` (auto via place_long_term_order)

   Background _reconciler_task (every 5 s, snapshot-then-act):
     for mid in expected:
       observed = _observed_short_size[mid]
       if abs(expected - observed) <= step_size:
         expected[mid] = observed       # WS caught up
         continue
       if (now - last_fire[mid]) > 30s:
         truth = http_query_short(mid)  # authoritative
         if last_fire unchanged during await:
           observed[mid] = truth
           expected[mid] = truth
```

## State

New / renamed fields on `LighterAdapter`:

| Field | Type | Source | Lifetime |
|---|---|---|---|
| `_observed_short_size` | `dict[int, float]` (UNSIGNED magnitude) | `_on_account_update` (WS) | per-symbol, replaced wholesale per snapshot |
| `_expected_short_size` | `dict[int, float]` (UNSIGNED magnitude) | `place_long_term_order` (success path, `err is None`) | reset by reconciler when WS catches up or HTTP confirms |
| `_last_fire_at` | `dict[int, float]` (monotonic) | `place_long_term_order` success path | rewritten on every successful fire — reconciler timeout is measured from the LATEST fire on a symbol |
| `_reconcile_task` | `asyncio.Task \| None` | started in `connect`, cancelled in `disconnect` | runs forever, sleeps 5 s between scans |

Keys are `market_index` (int), matching the existing `_ws_book_top`
convention. The field `_ws_account_positions` from the WS migration is
**renamed** to `_observed_short_size` (single source of truth — no
parallel state). `entry_price` / `unrealized_pnl` previously stored
inside `_ws_account_positions` move into a sibling
`_observed_position_meta: dict[int, dict]` so `get_position` (kept for
diagnostics) can still return a fully-populated `Position`.

**Sign convention.** The bot only opens hedge SHORTS. Positions are
tracked as unsigned magnitude. A long position in the same market (e.g.
the user manually opened one) shows up as 0 in `_observed_short_size` —
the engine doesn't try to hedge against longs; that's an operator-level
concern surfaced via the diagnostic `get_position`. This eliminates the
`max(signed, signed)` ambiguity (where −0.6 < −0.5 in math but more-short
in business terms).

## API

```python
class LighterAdapter:
    async def get_effective_position(self, symbol: str) -> Position | None:
        """Returns the position the engine should hedge against. Fuses
        WS-observed state with locally-tracked expected state from
        recent fires. Never under-reports immediately after a fill —
        avoids the over-hedge stack that bare WS reads produced in
        the 2026-05-07 incidents.

        Returns None if both layers report zero.
        """

    async def get_position(self, symbol: str) -> Position | None:
        """RAW WS-observed position. Kept for code paths that
        explicitly want the unfused source (e.g. reconciliation,
        diagnostics). The hedge engine MUST NOT use this directly —
        use `get_effective_position` instead.
        """
```

`place_long_term_order` keeps its existing signature; the expected stamp
is a side effect of the success path, transparent to callers.

## Reconciliation logic

### Stamping rule (in `place_long_term_order` success path)

Stamp happens when `create_order` returns `err is None` — server
accepted the message. **Independent of `_verify_fill` or the returned
`Order.size`**. This is the entire point of the design: trust
server-accept for fill, let the reconciler resolve any divergence
later. Keeping the stamp tied to `verify_fill > 0` would re-introduce
the over-hedge bug (verify_fill is the unreliable layer).

```python
# Inside _place_long_term_order_unlocked, on the success path:
if err is None:
    if side == "sell":
        # short increases by `size`
        self._expected_short_size[mid] = (
            self._expected_short_size.get(mid, 0.0) + size
        )
    else:  # "buy" — covering a short
        cur = self._expected_short_size.get(mid, 0.0)
        self._expected_short_size[mid] = max(0.0, cur - size)
    self._last_fire_at[mid] = time.monotonic()
```

### Reconciler loop

`_reconciler_loop()` runs as a background asyncio task started in
`connect()`. Every 5 s it iterates `_expected_short_size` keys:

```python
async def _reconciler_loop(self):
    while not self._ws_closing:
        try:
            await self._reconcile_once()
        except Exception as e:
            logger.warning(f"Reconciler iteration failed: {e}")
        await asyncio.sleep(5.0)

async def _reconcile_once(self):
    # Snapshot the keys + per-symbol (expected, last_fire) at scan
    # time. We only act on entries whose state hasn't changed by the
    # time the await returns — protects against the race where a
    # new fire happens while we're awaiting HTTP.
    snapshot = {
        mid: (
            self._expected_short_size.get(mid, 0.0),
            self._last_fire_at.get(mid, 0.0),
        )
        for mid in list(self._expected_short_size.keys())
    }
    for mid, (expected_at_scan, last_fire_at_scan) in snapshot.items():
        observed = self._observed_short_size.get(mid, 0.0)
        # Catch-up case: WS already shows the expected size (within
        # one step). Pin expected to observed and move on.
        if abs(expected_at_scan - observed) <= self._step_size_for(mid):
            # Only commit if no new fire happened mid-scan.
            if self._last_fire_at.get(mid, 0.0) == last_fire_at_scan:
                self._expected_short_size[mid] = observed
            continue
        # Timeout case: divergence persists past RECONCILE_TIMEOUT_S
        # since the last fire. Query HTTP authoritative.
        elapsed = time.monotonic() - last_fire_at_scan
        if elapsed <= RECONCILE_TIMEOUT_S:
            continue
        truth = await self._fetch_short_size_via_http(mid)
        if truth is None:
            continue  # HTTP failed; next scan will retry
        # Re-check that nothing fired during the await, otherwise
        # we'd overwrite a fresher stamp with stale truth.
        if self._last_fire_at.get(mid, 0.0) != last_fire_at_scan:
            logger.debug(
                f"Reconcile[{mid}] aborted: new fire stamped during "
                f"HTTP query — next scan will re-evaluate."
            )
            continue
        self._observed_short_size[mid] = truth
        self._expected_short_size[mid] = truth
        logger.info(
            f"Reconciled short_size[{mid}] via HTTP: "
            f"observed_was={observed}, expected_was={expected_at_scan}, "
            f"truth={truth}"
        )
```

### Constants

- `RECONCILE_TIMEOUT_S = 10.0` — WS account_all pushes typically land in
  <1 s; 10 s is "definitely should have arrived". Tightened from the
  original 30 s after observing real-world WS lag never exceeds 5 s.
  Faster recovery from genuinely-failed IOC fills at the cost of one
  extra HTTP call per failed fire. Over-hedge protection in
  `get_effective_position` is independent of this value.
- `step_size_for(mid)` reads the symbol's step_size from `_markets`
  (already cached at connect time) — tolerance equals one tick on the
  size axis.
- Reconciler scan period: 5 s. Independent of timeout. Only checks
  whether a reconcile is due — the actual HTTP query fires at most
  once per `RECONCILE_TIMEOUT_S` per symbol while divergence persists.

`_fetch_short_size_via_http(mid)` calls
`AccountApi.account(by="index", value=str(account_index))` and extracts
the position for the given `market_id`. Returns the unsigned magnitude
of the short side (or 0 if flat / long). Returns `None` on HTTP error
so the caller can skip and retry next scan without overwriting state.

## Buy/sell convention (preserved)

The adapter's existing `_place_long_term_order_unlocked` reads top-of-book
on the side **opposite** to the order's direction:
- `side="sell"` → reads `best_bid` (sells INTO the bid)
- `side="buy"` → reads `best_ask` (buys AT the ask)

No buffer applied — the IOC LIMIT goes at the exact cached level.
Tick-level book moves during request flight will auto-cancel the IOC;
the engine's drift logic re-fires on the next iter (now safely guarded
by expected/observed reconciliation, not by ad-hoc cooldowns).

## Engine integration

Two call-site changes in `engine/__init__.py`:

```python
# In _safe_get_position (used by _iterate):
- return await self._exchange.get_position(sym)
+ return await self._exchange.get_effective_position(sym)

# (No changes elsewhere — drift math, _maybe_rebalance_leg, all unchanged.)
```

The engine's per-leg cooldown (`_last_rebalance_at_per_leg`,
`REBALANCE_COOLDOWN_S`) is **removed** — the adapter's expected_position
guard subsumes it. Same for the idle-throttle (`IDLE_EXCHANGE_POLL_INTERVAL_S`):
already removed in a prior step because WS reads are free.

## Lifecycle integration

**No changes**. `lifecycle._open_short` already calls
`exchange.place_long_term_order`, which now stamps `_expected_short_size`
on success. Bootstrap fires inherit the guard automatically.

The synthetic 0.05% slippage previously booked into `perp_fees_paid_*`
stays removed (Lighter is zero-fee).

## Test strategy

Unit tests against `LighterAdapter` (existing stub-based pattern in
`tests/test_lighter_adapter.py`):

1. **`test_expected_stamp_after_successful_fire`**:
   place_long_term_order succeeds → `get_effective_position` returns
   the new size even when WS cache is empty.
2. **`test_effective_uses_max_when_ws_lags`**:
   set observed=0.0148, expected=0.0148 → effective=0.0148.
   set observed=0, expected=0.0148 → effective=0.0148.
   set observed=0.0148, expected=0 → effective=0.0148.
3. **`test_reconciler_clears_expected_when_observed_catches_up`**:
   simulate WS update setting observed=expected → next reconciler tick
   pins expected to observed (no divergence, no HTTP query).
4. **`test_reconciler_http_query_on_timeout`**:
   set expected > observed, advance time past RECONCILE_TIMEOUT_S,
   stub HTTP `account` to return a specific position → reconciler
   pins both observed and expected to the HTTP truth.
5. **`test_reconciler_http_zero_means_real_failure`**:
   simulate IOC auto-cancel scenario: expected=0.0148, observed=0,
   timeout elapses, HTTP returns 0 → reconciler resets expected to 0,
   so engine sees drift again on next iter.
6. **`test_get_position_unchanged`** (regression):
   `get_position` keeps returning raw observed (no fusion) — used
   by reconciler itself and diagnostic tooling.

Engine-side integration regression test:

7. **`test_engine_does_not_double_fire_during_ws_lag`**:
   uses a **real** `LighterAdapter` instance (with the existing
   sys.modules-stubbed lighter SDK), wired to a stub signer where
   `create_order` returns `err=None` (success) but the WS pump never
   pushes an account update. Run engine `_iterate` twice with engine
   target = some non-zero value. Assert `signer.create_order.await_count
   == 1` — the second iter must read `get_effective_position` =
   target (because expected was stamped) → drift = 0 → no fire.

   This is the literal regression test for the 2026-05-07 over-hedge
   incidents. A `MagicMock`-only test would not exercise the
   adapter's stamping path and so would not catch a regression where
   stamping breaks (e.g. accidentally tying it back to `verify_fill`).

All seven tests added under `tests/test_lighter_adapter.py` and
`tests/test_engine_dual_leg.py` respectively.

## Out of scope

- Replacing IOC LIMIT with `ORDER_TYPE_MARKET` — orthogonal; current pattern
  is correct, no slippage, just need fills to land. May revisit if the
  no-buffer policy proves to leave too many ordered un-filled.
- WS `trades` stream subscription — Lighter ships trades inside `account_all`
  snapshots; subscribing to a separate trades channel would be cleaner but
  adds dependency on undocumented behavior.
- Multi-leg dependent ordering (e.g., open ETH only after ARB confirmed
  filled) — not needed; legs are independent for drift calculation.

## Migration

Single PR on `feature/cross-pair-dual-hedge`:

1. Add `_observed_short_size` (rename of `_ws_account_positions`),
   `_observed_position_meta`, `_expected_short_size`, `_last_fire_at`,
   `_reconcile_task` to `LighterAdapter.__init__`.
2. Update `_on_account_update` to write the renamed fields. Single
   source of truth for observed state — no parallel `_ws_account_positions`.
3. Implement `get_effective_position` (returns Position with
   `size = max(observed, expected)`, side=`"short"` whenever non-zero).
4. Update `place_long_term_order` success path: stamp `_expected_short_size`
   delta-style (sell increments, buy decrements clamped at 0) on
   `err is None`, regardless of `verify_fill` outcome.
5. Implement `_reconciler_loop` + `_reconcile_once` + `_fetch_short_size_via_http`.
   Wire `_reconcile_task` start in `connect()`, cancel in `disconnect()`.
6. Engine: replace the `get_position` call in `_safe_get_position`
   (and any other call site used for drift math) with
   `get_effective_position`. Search:
   `grep -n "self._exchange.get_position" engine/__init__.py`.
7. Engine: remove `_last_rebalance_at_per_leg`, `REBALANCE_COOLDOWN_S`,
   and the cooldown branch at the top of `_maybe_rebalance_leg`. Remove
   `__init__.py` field initialization for both.
8. Tests: add the 7 cases listed above. Update any existing test that
   accesses `_ws_account_positions` directly to use the new field name.

No DB schema changes. No `.env` changes. No UI changes.
