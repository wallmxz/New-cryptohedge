# Funding Window — Design

**Date:** 2026-05-10
**Status:** Approved
**Branch:** new branch off master (suggest `feature/funding-window`)

## Problem

The datetime picker at `web/templates/partials/operation.html` (commit `d1c7bed`) currently affects ONLY Hedge PnL. Funding still reads `op.funding_paid_token0/1` from the DB column — cumulative since `op.started_at`, ignoring the picker.

User wants: when the window is set (`op.pnl_window_since_ts != None`), Funding should also be computed from that timestamp forward, NOT from `op.started_at`.

## Goal

Reuse the existing `pnl_window_since_ts` field (no new DB column, no new UI). Add an adapter method that sums funding payments from Lighter's `position_funding` API since a given timestamp. Engine wires the override into `compute_operation_pnl` when the window is active.

## Non-goals

- New DB column or UI changes
- New picker for funding
- Backfilling old operations

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  ENGINE _iterate (existing flow, post-PR #2)                │
│                                                              │
│  if op.pnl_window_since_ts is not None:                     │
│      funding_override = await self._exchange                 │
│          .get_funding_total_since(                           │
│              account_index=settings.lighter_account_index,   │
│              since_ts=op.pnl_window_since_ts,                │
│          )                                                   │
│  else:                                                       │
│      funding_override = None                                 │
│                                                              │
│  breakdown = compute_operation_pnl(                          │
│      ..., funding_override=funding_override                  │
│  )                                                           │
└────────────┬────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────┐
│  exchanges/lighter.py — get_funding_total_since (new)        │
│                                                              │
│  Paginates AccountApi.position_funding(account, ...)         │
│  filters by timestamp >= since_ts                            │
│  Sums per-market into (token0_total, token1_total) by mid    │
│  Returns same sign convention as funding_paid_token0/1       │
│  (positive = "we paid", negative = "we received")            │
└─────────────────────────────────────────────────────────────┘
```

## Components

### 1. `exchanges/base.py` — extend abstract `ExchangeAdapter`

Add to base:
```python
async def get_funding_total_since(
    self, *, account_index: int, since_ts: float,
    market_id_token0: int | None = None,
    market_id_token1: int | None = None,
) -> tuple[float, float]:
    """Returns (token0_total, token1_total) of cumulative funding paid
    since the given unix timestamp. Sign convention: positive = "we
    paid", negative = "we received" (matches op.funding_paid_token0/1).

    Default implementation: returns (0.0, 0.0). Override in concrete adapters."""
    return (0.0, 0.0)
```

### 2. `exchanges/lighter.py` — implement override

```python
async def get_funding_total_since(
    self, *, account_index: int, since_ts: float,
    market_id_token0: int | None = None,
    market_id_token1: int | None = None,
) -> tuple[float, float]:
    """Paginates Lighter position_funding API since `since_ts`. Routes
    per-market into token0/token1 totals using the cached mids."""
    if self._signer is None:
        return (0.0, 0.0)

    # Reuse the existing _fetch_position_funding helper (paginated, auth-token aware)
    entries = await self._fetch_position_funding(account_index)

    t0 = t1 = 0.0
    for e in entries:
        ts = float(e.get("timestamp", 0))
        if ts < since_ts:
            continue
        change = float(e.get("change", 0))
        # Lighter convention: change > 0 = received, change < 0 = paid
        # We invert to match funding_paid_token0/1: positive = paid
        attributed = -change
        mid = int(e.get("market_id", -1))
        if market_id_token0 is not None and mid == market_id_token0:
            t0 += attributed
        elif market_id_token1 is not None and mid == market_id_token1:
            t1 += attributed
    return (t0, t1)
```

(Notes: `_fetch_position_funding` already exists and handles pagination + auth. We just filter by ts and route by market_id.)

### 3. `engine/pnl.py` — accept funding override

Extend `compute_operation_pnl` signature (currently around line 100) to accept `funding_override: tuple[float, float] | None = None`:

```python
def compute_operation_pnl(
    op,
    p0_now: float, p1_now: float,
    pool_value_now: float,
    *,
    hedge_pnl_aggregate_override: float | None = None,
    funding_override: tuple[float, float] | None = None,  # NEW
) -> dict:
    # ...existing code...

    if funding_override is not None:
        funding_t0 = -funding_override[0]
        funding_t1 = -funding_override[1]
    else:
        funding_t0 = -op.funding_paid_token0
        funding_t1 = -op.funding_paid_token1

    # ...rest unchanged...
```

(Sign matches existing `-op.funding_paid_token0` invert: positive = received.)

### 4. `engine/__init__.py` — wire override

Find where `compute_operation_pnl` is called (likely in `_iterate` or a sibling method). Before the call:

```python
funding_override = None
if op.pnl_window_since_ts is not None and self._exchange is not None:
    try:
        funding_override = await asyncio.wait_for(
            self._exchange.get_funding_total_since(
                account_index=self._settings.lighter_account_index,
                since_ts=op.pnl_window_since_ts,
                market_id_token0=self._token0_mid,
                market_id_token1=self._token1_mid,
            ),
            timeout=5.0,
        )
    except (asyncio.TimeoutError, Exception) as e:
        logger.warning(f"funding override fetch failed: {e}")
        funding_override = None  # fallback to DB cumulative

breakdown = compute_operation_pnl(
    op, ..., funding_override=funding_override,
)
```

## Tests (~5 new)

`tests/test_lighter_funding_since.py` (new):
1. `test_get_funding_total_since_filters_by_timestamp` — entries before since_ts excluded
2. `test_get_funding_total_since_routes_per_market_id` — t0 vs t1 attribution by mid
3. `test_get_funding_total_since_inverts_sign` — Lighter `change > 0` → token total < 0 (we received)
4. `test_get_funding_total_since_returns_zeros_when_signer_none` — graceful no-op

`tests/test_pnl_dual_leg.py` (extend):
5. `test_compute_operation_pnl_uses_funding_override_when_provided` — override (10.0, 5.0) → breakdown has funding_t0 = -10.0 ignoring DB column

## Risks

1. **Pagination cost** — Lighter `position_funding` paginates; on-demand fetch every iter could be slow if many entries. Mitigation: `get_funding_total_since` only called when `pnl_window_since_ts` is set (rare path); 5s timeout wraps it. Future cache TTL if needed.
2. **Sign convention mismatch** — easy to invert wrong; locked by test #3.
3. **Market ID resolution** — engine's `_token0_mid`/`_token1_mid` may be None at startup; method handles `mid is None` gracefully (returns 0 for that leg).

## Verification

1. Restart uvicorn after merge.
2. Set the picker to a recent timestamp (e.g. 1h ago).
3. UI Funding line should change from cumulative-since-op-start to cumulative-since-picker.
4. Clear the picker → Funding goes back to DB cumulative.
