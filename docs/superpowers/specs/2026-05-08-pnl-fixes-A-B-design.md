# PnL Fixes A+B — Design Spec

**Date:** 2026-05-08 (morning, post-funding-accumulator-spec)
**Status:** Approved (user authorized after live probe confirmed approach)
**Branch:** `feature/cross-pair-dual-hedge`
**Predecessor:** `2026-05-08-pnl-funding-accumulator-design.md` (T1-T9 already shipped)

## Problem

After the funding accumulator landed, two problems surfaced when the user looked at the live panel at 08:35:

1. **Funding poller fails with HTTP 400 every poll** — Lighter rejects `position_funding` calls without an auth token. The accumulator never receives entries; `funding_paid_token0/1` stay at 0.

   Live probe confirmed:
   ```
   without auth: status=400 body={"code":20001,"message":"invalid param : auth required for main accounts"}
   with auth:    status=200 body={"position_fundings":[...]}
   ```

2. **`hedge_pnl` resets on every uvicorn restart** — `StateHub.hedge_realized_pnls` and `hedge_unrealized_pnls` are in-memory dicts populated from fills processed since the current process started. After tonight's two restarts (and the funding-spec restart), op #28's panel shows `Hedge PnL = -$0.38` (only what accumulated post-restart), but Lighter's authoritative `account.pnl()` reports `trade_pnl = -$4.52` cumulative — meaning ~$4.16 of fill activity is missing from the panel.

## Goal

Two narrow code changes:

- **A**: Generate a Lighter auth token on every `_fetch_position_funding` call and pass it as the `auth` parameter. Funding entries flow correctly.
- **B**: Replace `hedge_pnl_t0/t1` computation in `compute_operation_pnl` with a query of `AccountApi.pnl(by="index", value=str(account_index), resolution="1h", start_timestamp=..., end_timestamp=..., auth=token)` since `op.started_at`, computed as the delta between the latest cumulative `trade_pnl` and the trade_pnl baseline observed at op start.

## Out of scope

- Per-leg `hedge_pnl_token0` / `hedge_pnl_token1` separation — Lighter's pnl endpoint returns account-level trade_pnl, not per-market. We collapse to a single aggregate. Per-leg attribution would require summing fills individually, which the SDK doesn't expose conveniently. Display the aggregate under "Hedge PnL"; the per-leg fields stay at 0 until a separate spec.
- LP fees attribution / Beefy harvest tracking. Still its own future spec.
- Pool-subtotal-vs-Beefy-display reconciliation (different baselines). Documented earlier.

## Architecture

### Component A: funding auth

```
LighterAdapter._fetch_position_funding(limit)
  ├── token, _ = self._signer.create_auth_token_with_expiry(deadline=-1)  # NEW
  ├── if err: log + return []
  └── resp = await self._account_api.position_funding(
          account_index=self._account_index, limit=limit,
          auth=token,                                                      # NEW
      )
```

`create_auth_token_with_expiry(deadline=-1)` uses the SDK constant `DEFAULT_10_MIN_AUTH_EXPIRY` — token is valid for 10 minutes; we regenerate every call (60-s poll cadence is well within that window). Token returned as `(token_str, error)` tuple — error is None on success.

### Component B: hedge_pnl from Lighter pnl endpoint

```
LighterAdapter._fetch_trade_pnl(start_ts, end_ts) -> tuple[float, float]
  Returns (trade_pnl_at_or_before_start_ts, trade_pnl_at_end_ts).
  Both are cumulative trade_pnl since account creation.
  Caller computes delta = trade_pnl_now - trade_pnl_baseline.

  ├── token = signer.create_auth_token_with_expiry(deadline=-1)
  ├── resp = await account_api.pnl(
  │       by="index", value=str(account_index),
  │       resolution="1h",
  │       start_timestamp=start_ts - 7200,  # 2 h cushion to capture baseline bucket
  │       end_timestamp=end_ts,
  │       count_back=300,
  │       auth=token,
  │   )
  └── pnl_history = resp.pnl  # list of {timestamp, trade_pnl, ...}
       baseline = last entry where timestamp <= start_ts (or 0.0 if none)
       latest   = pnl_history[-1].trade_pnl
       return baseline, latest

GridMakerEngine, in the iter that builds operation_pnl_breakdown:
  ├── if exchange supports get_trade_pnl_since(...):
  │       baseline, latest = await exchange.get_trade_pnl_since(op.started_at, time.time())
  │       hedge_pnl_aggregate = latest - baseline
  │   else:
  │       hedge_pnl_aggregate = legacy hub.hedge_realized_pnl + hub.hedge_unrealized_pnl
  │
  └── pass hedge_pnl_aggregate into compute_operation_pnl as a NEW kwarg
      `hedge_pnl_aggregate_override` that, when present, replaces the per-leg
      sum.
```

### Sign convention

Lighter's `trade_pnl` is signed with the standard convention: positive = account profitable, negative = losing. The display field `Hedge PnL` already shows the value as-is (no flip in `pnl.py`). Direct passthrough.

### Refresh cadence

Once per `_iterate` (same cadence as the breakdown computation, currently 1 Hz). Single HTTP call per iter is acceptable; the reconciler already polls more frequently. The token regeneration is in-process (no HTTP), so adding it to each call is cheap.

### Caching to avoid spamming Lighter

The pnl endpoint is rate-limited indirectly via Lighter's WAF. To avoid extra load, cache the result for 30 s in `LighterAdapter._trade_pnl_cache: tuple[float, tuple[float, float]] | None` (cache_ts, (baseline, latest)). On each `_fetch_trade_pnl`:
- If `cache_ts + 30 > now`, return cached value.
- Else fetch + cache.

30 s is half the funding-poll interval (60 s) so the panel refresh feels live without hitting Lighter every second.

## Components

### 1. `exchanges/lighter.py` — patch `_fetch_position_funding` and add `get_trade_pnl_since`

Patch `_fetch_position_funding`:
```python
async def _fetch_position_funding(self, *, limit: int = 100) -> list:
    if self._signer is None:
        return []
    try:
        token, err = self._signer.create_auth_token_with_expiry(
            deadline=SignerClient.DEFAULT_10_MIN_AUTH_EXPIRY,
        )
        if err is not None:
            logger.warning(f"_fetch_position_funding: auth token error: {err}")
            return []
    except Exception as e:
        logger.warning(f"_fetch_position_funding: auth token raised: {e}")
        return []
    try:
        resp = await self._account_api.position_funding(
            account_index=self._account_index, limit=limit, auth=token,
        )
    except Exception as e:
        logger.warning(f"_fetch_position_funding failed: {e}")
        return []
    return list(getattr(resp, "position_fundings", None) or [])
```

Add `get_trade_pnl_since`:
```python
async def get_trade_pnl_since(
    self, start_ts: float, end_ts: float,
) -> tuple[float, float] | None:
    """Returns (trade_pnl_baseline, trade_pnl_latest) cumulative trade_pnl
    from Lighter's account pnl endpoint. Caller subtracts baseline from
    latest to get pnl during the window. None on error."""
    # cache check
    now = time.monotonic()
    if self._trade_pnl_cache is not None:
        cache_at, cached = self._trade_pnl_cache
        if now - cache_at < 30.0:
            return cached
    if self._signer is None or self._account_api is None:
        return None
    try:
        token, err = self._signer.create_auth_token_with_expiry(
            deadline=SignerClient.DEFAULT_10_MIN_AUTH_EXPIRY,
        )
        if err is not None:
            logger.warning(f"get_trade_pnl_since: auth token error: {err}")
            return None
    except Exception as e:
        logger.warning(f"get_trade_pnl_since: auth token raised: {e}")
        return None
    # Cushion start by 2h so we can find a baseline bucket whose ts <= start_ts.
    cushion_start = max(0, int(start_ts) - 7200)
    try:
        resp = await self._account_api.pnl(
            by="index", value=str(self._account_index),
            resolution="1h",
            start_timestamp=cushion_start, end_timestamp=int(end_ts),
            count_back=300, auth=token,
        )
    except Exception as e:
        logger.warning(f"get_trade_pnl_since failed: {e}")
        return None
    history = list(getattr(resp, "pnl", None) or [])
    if not history:
        return None
    # Latest cumulative trade_pnl
    latest = float(getattr(history[-1], "trade_pnl", 0) or 0)
    # Baseline = trade_pnl at the bucket whose timestamp <= start_ts.
    # If no such bucket, baseline = 0 (account had no prior activity).
    baseline = 0.0
    for entry in history:
        ts = float(getattr(entry, "timestamp", 0) or 0)
        if ts <= start_ts:
            baseline = float(getattr(entry, "trade_pnl", 0) or 0)
        else:
            break
    out = (baseline, latest)
    self._trade_pnl_cache = (now, out)
    return out
```

Add slot in `__init__`:
```python
# 30-s cache for AccountApi.pnl. (cache_at_monotonic, (baseline, latest)).
self._trade_pnl_cache: tuple[float, tuple[float, float]] | None = None
```

### 2. `exchanges/base.py` — abstract method (default `None`)

```python
async def get_trade_pnl_since(
    self, start_ts: float, end_ts: float,
) -> tuple[float, float] | None:
    """Default: not supported. Adapters that integrate the venue's
    cumulative-pnl endpoint override this. Returns
    (trade_pnl_baseline, trade_pnl_latest), or None if unsupported/erroring.
    Engine subtracts baseline from latest to get pnl during the window;
    None means fall back to in-memory accumulators."""
    return None
```

### 3. `engine/__init__.py` — call get_trade_pnl_since in iter, pass override to compute_operation_pnl

In `_iterate`, before `compute_operation_pnl`:
```python
hedge_pnl_override = None
op_started_at = None
if self._hub.current_operation_id is not None:
    try:
        op_row = await self._db.get_operation(self._hub.current_operation_id)
        if op_row:
            op_started_at = float(op_row.get("started_at") or 0)
    except Exception:
        pass
if op_started_at:
    try:
        getter = getattr(self._exchange, "get_trade_pnl_since", None)
        if getter is not None:
            r = await getter(op_started_at, time.time())
            if r is not None:
                baseline, latest = r
                hedge_pnl_override = latest - baseline
    except Exception as e:
        logger.warning(f"get_trade_pnl_since failed: {e}")

# pass to compute_operation_pnl as new kwarg
breakdown = compute_operation_pnl(
    op,
    current_pool_value_usd=pool_value_usd,
    current_token0_usd_price=p0_usd,
    current_token1_usd_price=p1_usd,
    hedge_realized_per_symbol=dict(self._hub.hedge_realized_pnls),
    hedge_unrealized_per_symbol=dict(self._hub.hedge_unrealized_pnls),
    hedge_pnl_aggregate_override=hedge_pnl_override,
)
```

(Same wiring on the single-leg path with the legacy kwarg.)

### 4. `engine/pnl.py` — accept override

```python
def compute_operation_pnl(
    op, *,
    ...,
    hedge_pnl_aggregate_override: float | None = None,
) -> dict:
    ...
    # Hedge PnL section:
    if hedge_pnl_aggregate_override is not None:
        # Authoritative venue cumulative trade_pnl since op start. The
        # per-leg split isn't available from this endpoint, so we put
        # the full value on token0 and zero on token1 for display
        # consistency. Aggregate is what users see.
        hedge_pnl = hedge_pnl_aggregate_override
        hedge_pnl_t0 = hedge_pnl
        hedge_pnl_t1 = 0.0
    elif is_cross_pair:
        # ... existing logic ...
    else:
        # ... existing legacy path ...
```

## Tests

### Adapter (`tests/test_lighter_adapter.py`)

1. `test_fetch_position_funding_uses_auth_token` — mock signer.create_auth_token_with_expiry to return ("TOKEN", None); assert position_funding called with `auth="TOKEN"`.

2. `test_fetch_position_funding_returns_empty_on_token_error` — mock signer to return ("", "auth fail"); assert position_funding NOT called and `[]` returned.

3. `test_get_trade_pnl_since_returns_baseline_and_latest` — mock pnl response with entries at ts=1000 (trade_pnl=-1.0), 2000 (trade_pnl=-3.0); request start_ts=1500 → baseline=-1.0 (the entry at 1000 has ts<=1500), latest=-3.0.

4. `test_get_trade_pnl_since_caches_for_30s` — call twice within 30s; assert account_api.pnl called only once.

5. `test_get_trade_pnl_since_returns_none_on_error` — mock pnl to raise; assert None returned (no crash).

### Engine (`tests/test_engine_funding.py`)

6. `test_iterate_uses_trade_pnl_override_when_supported` — exchange mock with get_trade_pnl_since returning (−1.0, −3.0); engine iter; breakdown.hedge_pnl == −2.0.

7. `test_iterate_falls_back_to_in_memory_when_get_trade_pnl_since_returns_none` — exchange returns None; breakdown.hedge_pnl from in-memory accumulator.

### PnL (`tests/test_pnl_dual_leg.py`)

8. `test_compute_operation_pnl_uses_override_when_provided` — pass hedge_pnl_aggregate_override=5.0; breakdown.hedge_pnl == 5.0, hedge_pnl_token0 == 5.0, hedge_pnl_token1 == 0.0.

## Risks

1. **Per-leg attribution lost.** Lighter's pnl endpoint is account-level. The breakdown's `hedge_pnl_token0`/`hedge_pnl_token1` fields are zeroed out (we put aggregate on token0 for backward compat with the existing template). User loses per-leg insight. Mitigation: future spec can sum per-market fills via a different endpoint if needed.

2. **Token regeneration cost.** `create_auth_token_with_expiry` is in-process (HMAC-style signing on the api_private_key). Cheap. Negligible.

3. **30s cache freshness.** If price moves fast and user watches the panel, hedge_pnl can lag by up to 30 s. Acceptable trade-off vs hammering Lighter.

4. **First-bucket alignment imprecision.** trade_pnl baseline is the bucket whose timestamp ≤ op.started_at. If op started at 03:48 and the closest bucket is 03:00, the 48 minutes of pre-op activity in that bucket leak into our baseline (pre-op fills, if any). For op #28 there were no other ops running pre-op, so baseline ≈ trade_pnl at exactly 03:00 ≈ true zero for op #28's window. For ops where the user has been trading manually before the op starts, this leaks. Acceptable for v1.

## Verification

After uvicorn restart with the fix deployed:
1. Funding row in panel should populate within 60 s (first poll iteration).
2. Hedge PnL row should jump from "since restart" value to "since op start" value within 1 iter.
3. Cross-check Hedge PnL against Lighter UI's Account → PnL → Today.

If panel values still don't match within reason, redo via brainstorming → spec → plan → execute.
