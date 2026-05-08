# PnL Funding Accumulator — Design Spec

**Date:** 2026-05-08
**Status:** Approved (autonomous decision, user pre-authorized brainstorm/plan/execute pipeline)
**Branch:** `feature/cross-pair-dual-hedge`
**Scope:** Funding payments (Lighter) only. LP fees / Beefy perf fee deferred.

## Problem

The operation panel shows `+$0.00` across all PnL components except `Hedge PnL`:

```
LP fees recebidas    +$0.00
Beefy perf fee       +$0.00
IL natural           -$0.00
Hedge PnL            +$0.05
Funding              +$0.00
Perp fees            +$0.00
Bootstrap slippage   +$0.00
```

For an operation that's been ACTIVE for 1h48min on Lighter (zero-fee venue) with active funding cycles on both ETH and ARB perps.

## Diagnosis (per component)

| Component | Why $0? | Action |
|---|---|---|
| `lp_fees_earned` | No producer in code. CLAUDE.md flags as known gap. | Defer to dedicated spec. |
| `beefy_perf_fee` | Derived from `-0.10 * lp_fees_earned`. Will follow above. | Follows above. |
| `il_natural` | Math correct. Op #28 baseline pool=$50.20; current pool ≈$50.00; HODL at current prices ≈$50.20. \|IL\| < $0.005 → rounds to `-$0.00`. **Not a bug.** | Verify empirically; no fix. |
| `hedge_pnl` | Working ($0.05). | None. |
| `funding_paid_token0/1` | No producer in code. Lighter SDK exposes `AccountApi.position_funding(...)` but it's never called. | **Fix this spec.** |
| `perp_fees_paid_token0/1` | Lighter is zero-fee. **Correct $0.** | None. |
| `bootstrap_slippage` | Op #28 used hedge-existing path (manual Beefy deposit). No swap → no slippage. **Correct $0.** | None. |

DB confirmation (op #28):
```
baseline_amount0=0.0166 baseline_amount1=95.85
baseline_pool_value_usd=$50.20
baseline_token0_usd_price=$2280.77 baseline_token1_usd_price=$0.12895
lp_fees_earned=0.0  funding_paid_token0=0.0  funding_paid_token1=0.0
perp_fees_paid_token0=0.0  perp_fees_paid_token1=0.0
bootstrap_slippage=0.0  bootstrap_state=active
```

## Goal (this spec)

Populate `funding_paid_token0` and `funding_paid_token1` with real Lighter funding payments accumulated during the operation lifetime, so `Funding` row in the panel shows actual data.

Out of scope: LP fees attribution (Beefy harvest tracking). That requires its own spec.

## Architecture

```
Lighter HTTP API
    │  GET /api/v1/positionFunding?accountIndex=X&limit=100
    ▼
LighterAdapter._fetch_position_funding()         (new method)
    │  returns list[PositionFunding]
    ▼
LighterAdapter._funding_poller_loop()            (new background task)
    │  every 60s, dedupe by funding_id, filter by ts > op.started_at
    │  emits via callback per new entry
    ▼
GridMakerEngine._on_funding_payment(entry, op_id) (new handler)
    │  per-market accumulation:
    │    if entry.market_id == token0_mid: db_field = "funding_paid_token0"
    │    elif entry.market_id == token1_mid: db_field = "funding_paid_token1"
    │  delta = -float(entry.change)  # Lighter convention: change>0 = received
    ▼
db.add_to_operation_accumulator(op_id, db_field, delta)
    │
    ▼
Next iter: compute_operation_pnl reads op.funding_paid_token0/1, displays in breakdown
```

### Sign convention

Lighter `PositionFunding.change` (string-decimal):
- Positive: user received funding (long received from short, or vice versa per market funding rate)
- Negative: user paid funding

Existing pnl.py (line 92-93):
```python
funding_t0 = -op.funding_paid_token0
```

Convention: DB column `funding_paid_token0` stores "positive = we paid". To preserve this without altering pnl.py:
- On new funding entry, write `delta = -float(entry.change)` to DB.
  - User received +$0.10 → `delta = -0.10` → DB column more negative → `funding_t0 = -(-0.10) = +0.10` shows positive in breakdown.
  - User paid -$0.10 → `delta = +0.10` → DB column more positive → `funding_t0 = -(+0.10) = -0.10` shows negative.

### Backfill at startup

When the engine starts (or operation is resumed from DB), fetch all historical funding since `op.started_at` once. Subsequent polls fetch only new entries (dedupe by `funding_id`).

Backfill is bounded: paginate via `cursor`, stop when an entry's `timestamp < op.started_at`.

### Dedup

In-memory `set[int]` of seen `funding_id`s, keyed per-adapter-instance. Survives reconnects but resets on uvicorn restart — acceptable because backfill at startup re-syncs from `op.started_at`.

### Poll interval

60 seconds. Lighter funding cycle is hourly; polling more often than the cycle is wasteful and risks WAF.

### Background task lifecycle

Started in `LighterAdapter.connect()` alongside `_reconcile_task`. Cancelled in `disconnect()`. Catches and logs exceptions; doesn't crash on transient API failures.

### Engine wiring

`GridMakerEngine` registers a callback on the adapter at init time:
```python
self._exchange.subscribe_funding(self._on_funding_payment)
```

`subscribe_funding` is a new abstract method on `ExchangeAdapter` — empty default impl (other exchanges can opt in).

In the callback, the engine resolves market_id → token0/token1 leg via `settings.dydx_symbol_token0/token1` (already cached at startup) and writes to the appropriate DB column.

## Components

### 1. `exchanges/lighter.py` — funding poller

```python
async def _fetch_position_funding(
    self, *, market_id: int | None = None, limit: int = 100,
    cursor: str | None = None,
) -> tuple[list, str | None]:
    """Page-fetch funding history. Returns (entries, next_cursor)."""

async def _funding_poller_loop(self) -> None:
    """Background task. Polls every 60s. Dedupes via _seen_funding_ids."""

def subscribe_funding(self, callback: Callable[[PositionFunding], Awaitable[None]]) -> None:
    """Engine registers a per-entry callback."""
```

State on adapter:
- `self._seen_funding_ids: set[int] = set()`
- `self._funding_callback: Callable | None = None`
- `self._funding_task: asyncio.Task | None = None`

### 2. `exchanges/base.py` — abstract method

```python
class ExchangeAdapter(ABC):
    def subscribe_funding(
        self, callback: Callable[[PositionFunding], Awaitable[None]],
    ) -> None:
        """Default: no-op. Adapters that support funding override."""
```

### 3. `engine/__init__.py` — handler + wiring

In `__init__`:
```python
self._exchange.subscribe_funding(self._on_funding_payment)
```

New method:
```python
async def _on_funding_payment(self, entry) -> None:
    op_id = self._hub.current_operation_id
    if op_id is None:
        return  # not in active op — funding still happens but not attributed
    mid = entry.market_id
    token0_mid = self._token0_mid  # resolved at startup from market meta
    if mid == token0_mid:
        field = "funding_paid_token0"
    elif mid == self._token1_mid:
        field = "funding_paid_token1"
    else:
        return  # funding for a market we're not hedging
    delta = -float(entry.change)
    await self._db.add_to_operation_accumulator(op_id, field, delta)
```

`_token0_mid` and `_token1_mid` are resolved once at engine startup from
`exchange.get_market_meta(symbol).market_index`.

**Edge case — callback before metadata loads:** If a funding entry arrives
before `_token0_mid`/`_token1_mid` are populated (e.g., poller fires before
adapter metadata caching completes), the handler returns silently. The
funding entry's `funding_id` is NOT marked as seen, so the next poll will
retry it. This avoids losing payments to a startup race.

**API ordering assumption:** Spec assumes `position_funding` returns
entries in DESC order by timestamp (most recent first). This is the
typical convention for paginated history APIs. The first task validates
this empirically; if Lighter returns ASC, the backfill termination
condition flips (stop when ts > op.started_at instead).

## Testing

### Unit tests (LighterAdapter)

1. `test_fetch_position_funding_paginates` — mock `account_api.position_funding` returning two pages with cursor; assert all entries collected.

2. `test_funding_poller_dedupes_by_id` — emit 3 entries, then re-emit 2 of them; assert callback fires 3 times total.

3. `test_funding_poller_filters_by_timestamp` — entries with ts < op.started_at are excluded.

4. `test_funding_poller_invokes_callback_per_entry` — register a callback, run one iteration, assert callback called once per new entry with the entry as arg.

### Unit tests (engine)

5. `test_on_funding_payment_writes_token0_accumulator` — feed entry with `market_id == token0_mid`, assert `add_to_operation_accumulator` called with `"funding_paid_token0"`.

6. `test_on_funding_payment_writes_token1_accumulator` — same but token1.

7. `test_on_funding_payment_negates_received_change` — entry.change=+0.10 → delta=-0.10 written to DB.

8. `test_on_funding_payment_skips_when_no_active_op` — op_id=None → no DB write.

### Integration

9. `test_funding_breakdown_shows_received_funding` — wire end-to-end: feed mock entry, run breakdown computation, assert `breakdown["funding"] > 0` for received funding (or < 0 for paid).

## Risks

1. **`AccountApi.position_funding` requires auth header**: signature includes `authorization` and `auth` params. Need to confirm it works with the SDK's signed-request mechanism (similar to `account()` calls already used by `_fetch_short_size_via_http`).

2. **WAF rate limiting**: 60s poll interval is conservative. Reconciler is at 5s already; total HTTP load ~12 calls/min, well within typical limits.

3. **Sign convention assumption**: Documented assumption is `change > 0` = user received. If Lighter inverts this, breakdown sign will be backwards. Mitigation: include a unit test that pins the convention; if observation in production shows wrong sign, flip the negation.

4. **Funding for markets not hedged**: If Lighter sends funding for a market we're not currently hedging (shouldn't happen but possible), the handler skips. No accumulator update.

## Verification (post-implementation)

After uvicorn restart with the fix deployed:
1. Wait one funding cycle (~1h on Lighter — typically on the hour).
2. Observe `Funding` row in panel: should show signed value (positive = received).
3. Cross-check against `Lighter UI > Funding History` for the same period.
4. If sign or magnitude is wrong, redo via brainstorming → spec → plan → execute.
