# PnL Fixes A+B Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the two PnL panel issues observed at 08:35 — (A) funding poller HTTP 400, (B) Hedge PnL resetting on uvicorn restart.

**Architecture:** A: pass `auth=signer.create_auth_token_with_expiry(...)[0]` to `position_funding`. B: query `AccountApi.pnl(by="index", value=..., resolution="1h", auth=token, start_timestamp=op.started_at-7200, end_timestamp=now, count_back=300)` once per engine iter (cached 30 s), compute `latest_trade_pnl - baseline_at_or_before_op_start`, pass as `hedge_pnl_aggregate_override` to `compute_operation_pnl`.

**Tech Stack:** lighter-sdk 1.0.9 (`SignerClient.create_auth_token_with_expiry`, `AccountApi.pnl`), aiosqlite, pytest-asyncio.

---

## File Structure

| File | Responsibility |
|---|---|
| `exchanges/lighter.py` | Patch `_fetch_position_funding` (add auth). Add `_trade_pnl_cache` slot. Add `get_trade_pnl_since` method. |
| `exchanges/base.py` | Add `get_trade_pnl_since` default implementation returning `None`. |
| `engine/pnl.py` | Accept `hedge_pnl_aggregate_override` kwarg; if present, replace per-leg sum. |
| `engine/__init__.py` | In `_iterate`, fetch trade_pnl since op.started_at; pass override to `compute_operation_pnl`. |
| `tests/test_lighter_adapter.py` | Tests for funding auth + `get_trade_pnl_since`. |
| `tests/test_pnl_dual_leg.py` | Test for `hedge_pnl_aggregate_override`. |
| `tests/test_engine_funding.py` | Test for engine wiring (override path + fallback path). |

---

### Task 1: Funding poller — pass auth token

**Files:**
- Modify: `exchanges/lighter.py` — `_fetch_position_funding` (current location near `get_oracle_prices`)
- Test: `tests/test_lighter_adapter.py` — append two new tests

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lighter_adapter.py`:

```python
@pytest.mark.asyncio
async def test_fetch_position_funding_passes_auth_token():
    """Per spec 2026-05-08-pnl-fixes-A-B: Lighter rejects position_funding
    without an auth token. _fetch_position_funding must generate one via
    signer.create_auth_token_with_expiry and pass it as `auth=`."""
    _install_lighter_stub()
    a = _make_adapter()
    a._account_api = MagicMock()
    a._account_api.position_funding = AsyncMock(return_value=MagicMock(
        position_fundings=[], next_cursor=None,
    ))
    a._signer = MagicMock()
    a._signer.create_auth_token_with_expiry = MagicMock(return_value=("TOKEN", None))
    await a._fetch_position_funding(limit=100)
    call_kwargs = a._account_api.position_funding.await_args.kwargs
    assert call_kwargs.get("auth") == "TOKEN"


@pytest.mark.asyncio
async def test_fetch_position_funding_returns_empty_on_token_error():
    """If signer returns an error, return empty list and DO NOT call the API."""
    _install_lighter_stub()
    a = _make_adapter()
    a._account_api = MagicMock()
    a._account_api.position_funding = AsyncMock(return_value=MagicMock(
        position_fundings=[], next_cursor=None,
    ))
    a._signer = MagicMock()
    a._signer.create_auth_token_with_expiry = MagicMock(
        return_value=("", "auth fail")
    )
    out = await a._fetch_position_funding(limit=100)
    assert out == []
    a._account_api.position_funding.assert_not_called()
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_lighter_adapter.py::test_fetch_position_funding_passes_auth_token tests/test_lighter_adapter.py::test_fetch_position_funding_returns_empty_on_token_error -v`
Expected: FAIL — current implementation passes no auth.

- [ ] **Step 3: Patch `_fetch_position_funding`**

In `exchanges/lighter.py`, replace the current `_fetch_position_funding` body with:

```python
    async def _fetch_position_funding(
        self, *, limit: int = 100,
    ) -> list:
        """Fetch the most-recent funding payments for this account from
        Lighter's position_funding endpoint. Returns the SDK's typed
        PositionFunding objects (or empty list on HTTP/parse failure
        so the poller doesn't crash).

        Lighter requires an auth token for account-scoped endpoints
        (code 20001 "auth required for main accounts" without one).
        We regenerate per call — token TTL is 10 min and the poller
        runs every 60 s, so it's always fresh.

        We do not paginate via cursor here — the poll cadence is 60 s
        and the page size (100) covers far more than one cycle on any
        reasonable funding-history rate. If we ever lag enough that
        100 entries don't cover the gap, the next poll catches up.
        """
        if self._signer is None:
            return []
        try:
            from lighter import SignerClient as _SignerClient
            token, err = self._signer.create_auth_token_with_expiry(
                deadline=_SignerClient.DEFAULT_10_MIN_AUTH_EXPIRY,
            )
            if err is not None:
                logger.warning(
                    f"_fetch_position_funding: auth token error: {err}"
                )
                return []
        except Exception as e:
            logger.warning(
                f"_fetch_position_funding: auth token raised: {e}"
            )
            return []
        try:
            resp = await self._account_api.position_funding(
                account_index=self._account_index, limit=limit,
                auth=token,
            )
        except Exception as e:
            logger.warning(f"_fetch_position_funding failed: {e}")
            return []
        return list(getattr(resp, "position_fundings", None) or [])
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_lighter_adapter.py::test_fetch_position_funding_passes_auth_token tests/test_lighter_adapter.py::test_fetch_position_funding_returns_empty_on_token_error -v`
Expected: PASS.

Also re-run the older funding tests to verify they still pass with the new signer dependency:
Run: `python -m pytest tests/test_lighter_adapter.py -k funding -v`

The earlier `test_fetch_position_funding_returns_entries` and `test_fetch_position_funding_returns_empty_on_error` set `a._account_api` but NOT `a._signer`. With the new code path, `a._signer is None` → returns `[]` early. Update those two tests to set `a._signer = MagicMock()` with the success token return:

```python
    a._signer = MagicMock()
    a._signer.create_auth_token_with_expiry = MagicMock(return_value=("TOKEN", None))
```

Same edit for `test_funding_poller_iteration_fires_callback_per_entry` and `test_funding_poller_iteration_noop_when_no_callback` if they exercise this path.

Re-run the full funding test set; all should pass.

- [ ] **Step 5: Commit**

```bash
git add exchanges/lighter.py tests/test_lighter_adapter.py
git commit -m "fix(lighter): _fetch_position_funding passes auth token

Lighter rejects account-scoped endpoints (code 20001) without auth.
Generate via signer.create_auth_token_with_expiry on each call (token
TTL is 10 min, poll cadence 60 s; always fresh). On signer error,
return empty + skip the HTTP call. Updates earlier funding tests to
set a stub signer.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: ExchangeAdapter base — `get_trade_pnl_since` default

**Files:**
- Modify: `exchanges/base.py`
- Test: `tests/test_lighter_adapter.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_lighter_adapter.py`:

```python
@pytest.mark.asyncio
async def test_get_trade_pnl_since_default_returns_none_on_base():
    """Default impl on ExchangeAdapter returns None — adapters that
    don't expose a cumulative-pnl endpoint inherit the disabled state."""
    _install_lighter_stub()
    a = _make_adapter()
    # Don't set up _signer/_account_api — fall through to default
    # in any non-Lighter context. Test the BASE class behavior by
    # asserting the default exists and returns None for a fresh subclass.
    from exchanges.base import ExchangeAdapter
    assert hasattr(ExchangeAdapter, "get_trade_pnl_since")
    # invoke unbound default by binding via a dummy instance:
    class _Dummy(ExchangeAdapter):
        name = "dummy"
        async def connect(self): pass
        async def disconnect(self): pass
        async def subscribe_orderbook(self, s, c): pass
        async def subscribe_fills(self, s, c): pass
        async def get_position(self, s): return None
        async def get_oracle_prices(self, syms): return {}
        async def get_fills(self, s, since=None): return []
        def get_tick_size(self, s): return 0.01
        def get_min_notional(self, s): return 0.01
    d = _Dummy()
    assert await d.get_trade_pnl_since(0.0, 1.0) is None
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_lighter_adapter.py::test_get_trade_pnl_since_default_returns_none_on_base -v`
Expected: FAIL — `AttributeError`.

- [ ] **Step 3: Add the default**

In `exchanges/base.py`, after `get_min_notional` abstract method (or wherever subscribe_funding was added), add:

```python
    async def get_trade_pnl_since(
        self, start_ts: float, end_ts: float,
    ) -> tuple[float, float] | None:
        """Returns (trade_pnl_baseline, trade_pnl_latest) cumulative
        trade_pnl from the venue's account-pnl endpoint. The caller
        subtracts baseline from latest to get pnl during the window.
        Default: not supported (returns None) — adapters that integrate
        the venue's cumulative-pnl endpoint override this."""
        return None
```

- [ ] **Step 4: Run test**

Run: `python -m pytest tests/test_lighter_adapter.py::test_get_trade_pnl_since_default_returns_none_on_base -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add exchanges/base.py tests/test_lighter_adapter.py
git commit -m "feat(base): get_trade_pnl_since default returning None

Per spec 2026-05-08-pnl-fixes-A-B. Concrete adapters override; default
None means engine falls back to in-memory accumulators (unchanged
behavior on adapters without venue-side cumulative pnl).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: LighterAdapter — `get_trade_pnl_since` with 30s cache

**Files:**
- Modify: `exchanges/lighter.py` — add `_trade_pnl_cache` slot in `__init__`, add method near `_fetch_position_funding`
- Test: `tests/test_lighter_adapter.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_lighter_adapter.py`:

```python
@pytest.mark.asyncio
async def test_get_trade_pnl_since_returns_baseline_and_latest():
    """Mock pnl response with 3 hourly buckets: ts=1000,2000,3000 with
    trade_pnl=-1.0,-2.0,-3.0 (cumulative). Request start_ts=1500
    -> baseline = -1.0 (entry at 1000 has ts<=1500), latest = -3.0."""
    _install_lighter_stub()
    a = _make_adapter()
    a._signer = MagicMock()
    a._signer.create_auth_token_with_expiry = MagicMock(
        return_value=("TOKEN", None)
    )
    a._account_api = MagicMock()
    a._account_api.pnl = AsyncMock(return_value=MagicMock(
        pnl=[
            MagicMock(timestamp=1000, trade_pnl=-1.0),
            MagicMock(timestamp=2000, trade_pnl=-2.0),
            MagicMock(timestamp=3000, trade_pnl=-3.0),
        ],
    ))
    out = await a.get_trade_pnl_since(start_ts=1500.0, end_ts=3500.0)
    assert out == (-1.0, -3.0)


@pytest.mark.asyncio
async def test_get_trade_pnl_since_caches_for_30s():
    """Two calls within 30 s share the cached result; pnl HTTP called once."""
    _install_lighter_stub()
    a = _make_adapter()
    a._signer = MagicMock()
    a._signer.create_auth_token_with_expiry = MagicMock(return_value=("TOKEN", None))
    a._account_api = MagicMock()
    a._account_api.pnl = AsyncMock(return_value=MagicMock(
        pnl=[MagicMock(timestamp=1000, trade_pnl=-5.0)],
    ))
    r1 = await a.get_trade_pnl_since(start_ts=500.0, end_ts=2000.0)
    r2 = await a.get_trade_pnl_since(start_ts=500.0, end_ts=2000.0)
    assert r1 == r2 == (-5.0, -5.0)
    assert a._account_api.pnl.await_count == 1


@pytest.mark.asyncio
async def test_get_trade_pnl_since_returns_none_on_error():
    """HTTP failure -> None; doesn't crash."""
    _install_lighter_stub()
    a = _make_adapter()
    a._signer = MagicMock()
    a._signer.create_auth_token_with_expiry = MagicMock(return_value=("TOKEN", None))
    a._account_api = MagicMock()
    a._account_api.pnl = AsyncMock(side_effect=RuntimeError("boom"))
    out = await a.get_trade_pnl_since(start_ts=100.0, end_ts=200.0)
    assert out is None


@pytest.mark.asyncio
async def test_get_trade_pnl_since_returns_zero_baseline_when_no_history_before_start():
    """If all returned buckets are AFTER start_ts, baseline = 0.0
    (account had no prior cumulative pnl to subtract)."""
    _install_lighter_stub()
    a = _make_adapter()
    a._signer = MagicMock()
    a._signer.create_auth_token_with_expiry = MagicMock(return_value=("TOKEN", None))
    a._account_api = MagicMock()
    a._account_api.pnl = AsyncMock(return_value=MagicMock(
        pnl=[
            MagicMock(timestamp=2000, trade_pnl=-2.0),
            MagicMock(timestamp=3000, trade_pnl=-3.0),
        ],
    ))
    out = await a.get_trade_pnl_since(start_ts=1500.0, end_ts=3500.0)
    assert out == (0.0, -3.0)
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_lighter_adapter.py -k trade_pnl_since -v`
Expected: FAIL — `AttributeError: 'get_trade_pnl_since'`.

- [ ] **Step 3: Add cache slot + method**

In `exchanges/lighter.py` `__init__`, after the `_funding_task` slot:

```python
        # 30-s in-process cache for AccountApi.pnl. The endpoint is
        # account-level (one snapshot per call) and the panel reads
        # hedge_pnl every iter (1 Hz). Cache shaves Lighter-side load
        # without compromising display freshness.
        # Layout: (cache_at_monotonic, (baseline, latest)) | None.
        self._trade_pnl_cache: tuple[float, tuple[float, float]] | None = None
```

In `exchanges/lighter.py`, after `_fetch_position_funding`, add:

```python
    async def get_trade_pnl_since(
        self, start_ts: float, end_ts: float,
    ) -> tuple[float, float] | None:
        """Cumulative trade_pnl from Lighter's AccountApi.pnl, returned
        as (baseline_at_or_before_start_ts, latest). Caller computes
        delta = latest - baseline to get pnl during the window.
        Returns None on error so the engine falls back to its in-memory
        accumulator. 30-s in-process cache.
        """
        # Cache check — re-use a recent answer to spare Lighter.
        now_mono = time.monotonic()
        if self._trade_pnl_cache is not None:
            cache_at, cached = self._trade_pnl_cache
            if now_mono - cache_at < 30.0:
                return cached
        if self._signer is None or self._account_api is None:
            return None
        try:
            from lighter import SignerClient as _SignerClient
            token, err = self._signer.create_auth_token_with_expiry(
                deadline=_SignerClient.DEFAULT_10_MIN_AUTH_EXPIRY,
            )
            if err is not None:
                logger.warning(f"get_trade_pnl_since: auth token error: {err}")
                return None
        except Exception as e:
            logger.warning(f"get_trade_pnl_since: auth token raised: {e}")
            return None
        # 2 h cushion ensures we get an entry whose ts <= start_ts as
        # the cumulative-pnl baseline (Lighter aligns to hour boundaries).
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
        latest = float(getattr(history[-1], "trade_pnl", 0) or 0)
        # Baseline = latest entry whose timestamp <= start_ts. If none
        # in the window, default to 0 (account had no prior pnl).
        baseline = 0.0
        for entry in history:
            ts = float(getattr(entry, "timestamp", 0) or 0)
            if ts <= start_ts:
                baseline = float(getattr(entry, "trade_pnl", 0) or 0)
            else:
                break
        out = (baseline, latest)
        self._trade_pnl_cache = (now_mono, out)
        return out
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_lighter_adapter.py -k trade_pnl_since -v`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add exchanges/lighter.py tests/test_lighter_adapter.py
git commit -m "feat(lighter): get_trade_pnl_since for venue-side hedge_pnl

Replaces the in-memory _hedge_realized_pnls accumulator (which loses
state on every uvicorn restart) with a query of AccountApi.pnl.
Returns (baseline_at_op_start, latest) so the engine can compute the
exact delta during the operation window. 30 s cache to avoid hammering
Lighter (panel refreshes at 1 Hz). 2 h cushion on start_timestamp so
the response always contains a baseline bucket whose ts <= start_ts.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: `compute_operation_pnl` accepts `hedge_pnl_aggregate_override`

**Files:**
- Modify: `engine/pnl.py`
- Test: `tests/test_pnl_dual_leg.py`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_pnl_dual_leg.py`:

```python
def test_compute_operation_pnl_uses_override_when_provided():
    """When hedge_pnl_aggregate_override is provided (e.g., from a
    venue-side cumulative pnl query), it replaces the per-leg sum and
    becomes the authoritative hedge_pnl. Per-leg fields collapse: the
    full value goes on token0 for display consistency, token1 = 0."""
    from engine.pnl import compute_operation_pnl
    from engine.operation import Operation, OperationState
    op = Operation(
        id=1, started_at=1700000000.0, state=OperationState.ACTIVE,
        baseline_eth_price=2000.0, baseline_pool_value_usd=50.0,
        baseline_amount0=0.01, baseline_amount1=100.0,
        baseline_collateral=100.0,
        baseline_token0_usd_price=2000.0, baseline_token1_usd_price=0.10,
    )
    out = compute_operation_pnl(
        op,
        current_pool_value_usd=50.0,
        current_token0_usd_price=2000.0,
        current_token1_usd_price=0.10,
        hedge_realized_per_symbol={"ETH-USD": 1.0, "ARB-USD": 2.0},
        hedge_unrealized_per_symbol={"ETH-USD": 0.5, "ARB-USD": 0.5},
        hedge_pnl_aggregate_override=-7.5,
    )
    assert out["hedge_pnl"] == -7.5
    assert out["hedge_pnl_token0"] == -7.5
    assert out["hedge_pnl_token1"] == 0.0


def test_compute_operation_pnl_keeps_per_leg_when_no_override():
    """When override is None (default), the existing per-leg sum
    behavior is preserved — backwards compatible."""
    from engine.pnl import compute_operation_pnl
    from engine.operation import Operation, OperationState
    op = Operation(
        id=1, started_at=1700000000.0, state=OperationState.ACTIVE,
        baseline_eth_price=2000.0, baseline_pool_value_usd=50.0,
        baseline_amount0=0.01, baseline_amount1=100.0,
        baseline_collateral=100.0,
        baseline_token0_usd_price=2000.0, baseline_token1_usd_price=0.10,
    )
    out = compute_operation_pnl(
        op,
        current_pool_value_usd=50.0,
        current_token0_usd_price=2000.0,
        current_token1_usd_price=0.10,
        hedge_realized_per_symbol={"ARB-USD": 2.0, "ETH-USD": 1.0},
        hedge_unrealized_per_symbol={"ARB-USD": 0.5, "ETH-USD": 0.5},
    )
    # sorted keys: ARB-USD < ETH-USD lexicographically -> token0_key="ARB-USD"
    assert out["hedge_pnl_token0"] == 2.5  # ARB realized + unrealized
    assert out["hedge_pnl_token1"] == 1.5  # ETH realized + unrealized
    assert out["hedge_pnl"] == 4.0
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_pnl_dual_leg.py::test_compute_operation_pnl_uses_override_when_provided -v`
Expected: FAIL — `compute_operation_pnl` doesn't accept `hedge_pnl_aggregate_override`.

- [ ] **Step 3: Add the kwarg**

In `engine/pnl.py`, modify `compute_operation_pnl`:

Replace the `def compute_operation_pnl(...)` signature so the new kwarg appears at the end of the keyword-only block:

```python
def compute_operation_pnl(
    op: Operation,
    *,
    current_pool_value_usd: float,
    # cross-pair signature (preferred):
    current_token0_usd_price: float | None = None,
    current_token1_usd_price: float | None = None,
    hedge_realized_per_symbol: dict[str, float] | None = None,
    hedge_unrealized_per_symbol: dict[str, float] | None = None,
    # legacy single-leg signature (kept for backwards compat):
    current_eth_price: float | None = None,
    hedge_realized_since_baseline: float | None = None,
    hedge_unrealized_since_baseline: float | None = None,
    # Authoritative hedge_pnl override (used when the venue exposes
    # cumulative trade_pnl since op start — survives uvicorn restarts).
    hedge_pnl_aggregate_override: float | None = None,
) -> dict:
```

In the same function, locate the existing `# Hedge PnL — per-leg dicts in cross-pair, single aggregate in legacy.` block and replace it with:

```python
    # Hedge PnL.
    if hedge_pnl_aggregate_override is not None:
        # Authoritative venue-side cumulative trade_pnl since op start
        # (e.g., LighterAdapter.get_trade_pnl_since). Per-leg attribution
        # isn't available at this level, so the full value lives on
        # token0 and token1 stays 0 — the aggregate is what users see.
        hedge_pnl_t0 = hedge_pnl_aggregate_override
        hedge_pnl_t1 = 0.0
    elif is_cross_pair:
        rps = hedge_realized_per_symbol or {}
        ups = hedge_unrealized_per_symbol or {}
        # Symbol order is whatever's in the dicts; we pick keys deterministically
        # by sorted order so token0 vs token1 attribution is stable across calls.
        keys = sorted(set(rps) | set(ups))
        token0_key = keys[0] if keys else None
        token1_key = keys[1] if len(keys) > 1 else None

        hedge_pnl_t0 = (
            (rps.get(token0_key, 0.0) + ups.get(token0_key, 0.0))
            if token0_key else 0.0
        )
        hedge_pnl_t1 = (
            (rps.get(token1_key, 0.0) + ups.get(token1_key, 0.0))
            if token1_key else 0.0
        )
    else:
        hedge_pnl_t0 = (
            (hedge_realized_since_baseline or 0.0)
            + (hedge_unrealized_since_baseline or 0.0)
        )
        hedge_pnl_t1 = 0.0

    hedge_pnl = hedge_pnl_t0 + hedge_pnl_t1
```

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_pnl_dual_leg.py -v`
Expected: all PASS (existing + 2 new).

- [ ] **Step 5: Commit**

```bash
git add engine/pnl.py tests/test_pnl_dual_leg.py
git commit -m "feat(pnl): hedge_pnl_aggregate_override kwarg

Lets the engine pass an authoritative hedge_pnl from the venue's
cumulative trade_pnl endpoint (LighterAdapter.get_trade_pnl_since),
overriding the per-leg sum that resets on every uvicorn restart. When
provided, token0_key gets the full value and token1_key gets 0 — the
aggregate is what the panel displays under 'Hedge PnL'. Per-leg
attribution is reserved for a future spec.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: Engine wiring — query trade_pnl, pass override

**Files:**
- Modify: `engine/__init__.py` — `_iterate` (both dual-leg and single-leg branches), import `time` if not already
- Test: `tests/test_engine_funding.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_engine_funding.py`:

```python
@pytest.mark.asyncio
async def test_iterate_uses_trade_pnl_override_when_supported():
    """When the exchange exposes get_trade_pnl_since, _iterate fetches
    it and passes hedge_pnl_aggregate_override into compute_operation_pnl."""
    from engine import GridMakerEngine
    state = StateHub(hedge_ratio=1.0)
    state.operation_state = "active"
    state.current_operation_id = 42

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
    db.add_to_operation_accumulator = AsyncMock()
    db.insert_order_log = AsyncMock()
    db.get_active_grid_orders = AsyncMock(return_value=[])
    db.get_operation = AsyncMock(return_value={
        "id": 42, "started_at": 1700000000.0, "status": "active",
        "baseline_eth_price": 2000.0, "baseline_pool_value_usd": 50.0,
        "baseline_amount0": 0.01, "baseline_amount1": 100.0,
        "baseline_collateral": 100.0,
        "baseline_token0_usd_price": 2000.0,
        "baseline_token1_usd_price": 0.10,
    })

    exchange = MagicMock()
    exchange.name = "lighter"
    exchange.subscribe_funding = MagicMock()
    exchange.place_long_term_order = AsyncMock()
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=10.0))
    exchange.get_oracle_prices = AsyncMock(return_value={
        "ETH-USD": 2000.0, "ARB-USD": 0.10,
    })
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])
    # Authoritative cumulative pnl: baseline=-1.0 at op start, latest=-3.5 now.
    # Override = -2.5.
    exchange.get_trade_pnl_since = AsyncMock(return_value=(-1.0, -3.5))

    pool = MagicMock()
    pool.read_price = AsyncMock(return_value=20000.0)  # arbitrary in-range value
    beefy = MagicMock()
    beefy.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-100000, tick_upper=100000,  # full-range to avoid out-of-range branch
        amount0=0.01, amount1=100.0, share=1.0, raw_balance=10**18,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=18,
    )

    await engine._iterate()

    breakdown = state.operation_pnl_breakdown
    assert breakdown.get("hedge_pnl") == -2.5, (
        f"expected hedge_pnl=-2.5 (latest -3.5 minus baseline -1.0), "
        f"got {breakdown.get('hedge_pnl')}"
    )
    assert breakdown.get("hedge_pnl_token0") == -2.5
    assert breakdown.get("hedge_pnl_token1") == 0.0


@pytest.mark.asyncio
async def test_iterate_falls_back_when_get_trade_pnl_since_returns_none():
    """When get_trade_pnl_since returns None (transient error or
    unsupported adapter), _iterate falls back to the in-memory hedge
    accumulator without crashing."""
    from engine import GridMakerEngine
    state = StateHub(hedge_ratio=1.0)
    state.operation_state = "active"
    state.current_operation_id = 42
    state.hedge_realized_pnls = {"ETH-USD": 1.0}
    state.hedge_unrealized_pnls = {"ETH-USD": 0.5}

    settings = MagicMock()
    settings.dydx_symbol_token0 = "ETH-USD"
    settings.dydx_symbol_token1 = ""
    settings.threshold_aggressive = 0.01
    settings.max_open_orders = 200
    settings.pool_token0_symbol = "ETH"
    settings.pool_token1_symbol = "USDC"
    settings.alert_webhook_url = ""
    settings.dydx_symbol = "ETH-USD"
    settings.min_rebalance_notional_usd = 0.50

    db = MagicMock()
    db.add_to_operation_accumulator = AsyncMock()
    db.insert_order_log = AsyncMock()
    db.get_active_grid_orders = AsyncMock(return_value=[])
    db.get_operation = AsyncMock(return_value={
        "id": 42, "started_at": 1700000000.0, "status": "active",
        "baseline_eth_price": 2000.0, "baseline_pool_value_usd": 50.0,
        "baseline_amount0": 0.01, "baseline_amount1": 0.0,
        "baseline_collateral": 100.0,
    })

    exchange = MagicMock()
    exchange.name = "lighter"
    exchange.subscribe_funding = MagicMock()
    exchange.place_long_term_order = AsyncMock()
    exchange.get_collateral = AsyncMock(return_value=130.0)
    exchange.get_position = AsyncMock(return_value=None)
    exchange.get_market_meta = AsyncMock(return_value=MagicMock(min_notional=10.0))
    exchange.get_oracle_prices = AsyncMock(return_value={"ETH-USD": 2000.0})
    exchange.get_open_orders_cloids = AsyncMock(return_value=[])
    exchange.get_trade_pnl_since = AsyncMock(return_value=None)  # error path

    pool = MagicMock()
    pool.read_price = AsyncMock(return_value=2000.0)
    beefy = MagicMock()
    beefy.read_position = AsyncMock(return_value=MagicMock(
        tick_lower=-100000, tick_upper=100000,
        amount0=0.01, amount1=0.0, share=1.0, raw_balance=10**18,
    ))

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=exchange, pool_reader=pool, beefy_reader=beefy,
        decimals0=18, decimals1=6,
    )

    await engine._iterate()

    breakdown = state.operation_pnl_breakdown
    # Fallback: legacy single-leg sum hedge_realized_since_baseline +
    # hedge_unrealized_since_baseline = 1.0 + 0.5 = 1.5
    assert breakdown.get("hedge_pnl") == 1.5
```

- [ ] **Step 2: Run to verify failure**

Run: `python -m pytest tests/test_engine_funding.py -k "trade_pnl_override or get_trade_pnl_since_returns_none" -v`
Expected: FAIL — engine doesn't query get_trade_pnl_since yet.

- [ ] **Step 3: Wire the engine**

In `engine/__init__.py`, locate the section in `_iterate` where `compute_operation_pnl` is called for both dual-leg and single-leg. There are two callsites (around the `if is_dual_leg:` block at ~line 974 and ~line 984). Both need the override.

BEFORE the `compute_operation_pnl` call, add the fetch:

```python
                    # Authoritative venue-side hedge_pnl since op.started_at
                    # (overrides in-memory accumulator that resets on uvicorn
                    # restart). None = fall back.
                    hedge_pnl_override = None
                    try:
                        op_started_at = float(op_row.get("started_at") or 0)
                    except Exception:
                        op_started_at = 0.0
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

Then add `hedge_pnl_aggregate_override=hedge_pnl_override` to BOTH `compute_operation_pnl` calls (the dual-leg one and the single-leg one).

The result for the dual-leg branch:

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

Confirm `import time` is already at the top of `engine/__init__.py` (it is — already used elsewhere).

- [ ] **Step 4: Run tests**

Run: `python -m pytest tests/test_engine_funding.py -v`
Expected: all 10 PASS (8 existing + 2 new).

- [ ] **Step 5: Commit + push everything**

```bash
git add engine/__init__.py tests/test_engine_funding.py
git commit -m "feat(engine): query venue-side hedge_pnl in _iterate

If the adapter exposes get_trade_pnl_since, _iterate fetches
(baseline, latest) cumulative trade_pnl since op.started_at and
passes the delta as hedge_pnl_aggregate_override into
compute_operation_pnl. Otherwise falls back to the legacy in-memory
accumulator. Closes the gap where Hedge PnL would reset on every
uvicorn restart.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
git push
```

---

### Task 6: Final verification

**Files:**
- Verify only

- [ ] **Step 1: Run full suite**

Run: `python -m pytest tests/ -q --no-header 2>&1 | tail -5`
Expected: all pass (~295 total: previous 286 + ~9 new tests across this plan).

- [ ] **Step 2: One-shot verification of `get_trade_pnl_since` against live Lighter**

Create `scripts/_verify_trade_pnl.py` (temporary):

```python
"""Probe live Lighter API: fetch trade_pnl since op.started_at for
the active operation. Doesn't touch the running uvicorn — uses its
own SDK clients."""
import asyncio
import os
import sqlite3
from dotenv import load_dotenv
load_dotenv()

async def main():
    from lighter import ApiClient, Configuration, AccountApi, SignerClient
    cfg = Configuration(host="https://mainnet.zklighter.elliot.ai")
    api = ApiClient(configuration=cfg)
    aa = AccountApi(api)
    acc_idx = int(os.environ["LIGHTER_ACCOUNT_INDEX"])
    api_key_idx = int(os.environ["LIGHTER_API_KEY_INDEX"])
    api_priv = os.environ["LIGHTER_API_PRIVATE_KEY"]
    signer = SignerClient(
        url="https://mainnet.zklighter.elliot.ai",
        account_index=acc_idx,
        api_private_keys={api_key_idx: api_priv},
    )
    token, _ = signer.create_auth_token_with_expiry(deadline=-1)
    # Read op_started_at from DB
    con = sqlite3.connect("automoney.db")
    cur = con.cursor()
    cur.execute(
        "SELECT id, started_at FROM operations WHERE status = 'active' LIMIT 1"
    )
    row = cur.fetchone()
    if not row:
        print("No active op")
        await signer.close()
        await api.close()
        return
    op_id, started_at = row
    import time as _t
    now = _t.time()
    print(f"op#{op_id} started_at={started_at:.0f} now={now:.0f}")
    cushion = max(0, int(started_at) - 7200)
    resp = await aa.pnl(
        by="index", value=str(acc_idx),
        resolution="1h",
        start_timestamp=cushion, end_timestamp=int(now),
        count_back=300, auth=token,
    )
    history = list(getattr(resp, "pnl", None) or [])
    print(f"pnl entries: {len(history)}")
    if history:
        latest = float(history[-1].trade_pnl)
        baseline = 0.0
        for entry in history:
            ts = float(entry.timestamp)
            if ts <= started_at:
                baseline = float(entry.trade_pnl)
            else:
                break
        delta = latest - baseline
        print(f"baseline={baseline} latest={latest} delta={delta:.4f}")
    await signer.close()
    await api.close()

asyncio.run(main())
```

Run: `"C:/Users/Wallace/Python313/python.exe" scripts/_verify_trade_pnl.py`
Expected: prints baseline + latest + delta. Delta should be the actual hedge_pnl since op start (compare against panel after restart).

- [ ] **Step 3: Delete the verification script**

```bash
rm scripts/_verify_trade_pnl.py
```

- [ ] **Step 4: Push everything (if not already pushed by Task 5)**

```bash
git push
```

---

## Self-Review

**Spec coverage:**
- §A funding poller auth: Task 1 implements + tests.
- §B `get_trade_pnl_since` + cache: Tasks 2 (base default), 3 (Lighter impl).
- §B `compute_operation_pnl` override: Task 4.
- §B engine wiring: Task 5.
- §Verification: Task 6.

**Placeholder scan:** All steps have concrete code or commands. No TBD/TODO.

**Type consistency:**
- `_trade_pnl_cache` typed `tuple[float, tuple[float, float]] | None` consistently.
- `get_trade_pnl_since` returns `tuple[float, float] | None` consistently across base + Lighter impl.
- `hedge_pnl_aggregate_override` typed `float | None = None` in pnl.py.

Plan ready for execution.
