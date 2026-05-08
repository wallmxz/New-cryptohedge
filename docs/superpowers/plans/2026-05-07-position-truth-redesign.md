# Adapter-owned Position Truth — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make over-hedge structurally impossible by giving the LighterAdapter sole ownership of "current short size", fusing WS-observed state with locally-stamped expected state from successful `create_order` calls. A 5 s background reconciler resolves persistent divergence via HTTP authoritative query at 30 s timeout.

**Architecture:** Two adapter-internal dicts (`_observed_short_size`, `_expected_short_size`) plus `_last_fire_at` timestamps. Single accessor `get_effective_position` returns `Position(size=max(observed, expected))`. Engine swaps one call site (`_safe_get_position`); cooldown logic gets removed (subsumed by the new guard). Lifecycle bootstrap inherits the protection automatically because it already calls `place_long_term_order`.

**Tech Stack:** Python 3.13, asyncio, lighter-sdk 1.0.9, pytest, pytest-asyncio. Spec: [docs/superpowers/specs/2026-05-07-position-truth-redesign-design.md](../specs/2026-05-07-position-truth-redesign-design.md) (commit 3003ad5).

---

## File Structure

| File | Responsibility | Change |
|---|---|---|
| `exchanges/lighter.py` | LighterAdapter (state, stamping, reconciler, accessors) | Modify — most work concentrated here |
| `engine/__init__.py` | GridMakerEngine `_safe_get_position` + cooldown removal | 2 small edits |
| `tests/test_lighter_adapter.py` | Unit tests for adapter state and reconciler | Add 6 tests; update existing tests that touched `_ws_account_positions` |
| `tests/test_engine_dual_leg.py` | Engine integration regression test | Add 1 test |

No new files. No DB schema changes. No `.env` or UI changes.

---

## Task 1: Rename `_ws_account_positions` → `_observed_short_size` + split metadata

Splits the existing observed-position dict into two parallel dicts: one for the float magnitude of the short size (the only thing the engine cares about for drift), and one for the diagnostic metadata (entry, unrealized, sign — kept so `get_position` can still return a fully-populated `Position` object).

**Files:**
- Modify: `exchanges/lighter.py:139-143` (init), `exchanges/lighter.py:441-469` (parser), `exchanges/lighter.py:818-835` (get_position)
- Test: `tests/test_lighter_adapter.py` (existing test `test_on_account_update_extracts_positions_and_collateral` and helper `_seed_position`)

- [ ] **Step 1: Update existing test to use new field names**

The existing test `test_on_account_update_extracts_positions_and_collateral` currently asserts `a._ws_account_positions[0]["sign"] == -1`. After the rename, the assertions move to `_observed_short_size[0] == 0.05` (unsigned magnitude) and `_observed_position_meta[0]["sign"] == -1`. Edit `tests/test_lighter_adapter.py`:

```python
# Replace the existing assertions block
# (around lines 257-261 and 268-271 of test_on_account_update_extracts_positions_and_collateral):
assert a._ws_collateral == 150.75
# Magnitude (engine-facing):
assert a._observed_short_size[0] == 0.05
assert a._observed_short_size[50] == 100.0
# Metadata (diagnostic-facing):
assert a._observed_position_meta[0]["sign"] == -1
assert a._observed_position_meta[0]["avg_entry_price"] == 2390.0
assert a._observed_position_meta[50]["sign"] == 1
```

Update the second sub-block (closure check) similarly:

```python
# After ETH closes:
assert 0 not in a._observed_short_size
assert a._observed_short_size[50] == 100.0
assert a._ws_collateral == 100.0
# After everything closes:
assert a._observed_short_size == {}
assert a._observed_position_meta == {}
```

Update the helper `_seed_position` (defined near the top of the test file) to write the new dicts:

```python
def _seed_position(a, market_index: int, *, sign: int, size: float,
                   avg_entry: float, unrealized: float = 0.0) -> None:
    """Seed the adapter's observed-short-size + metadata caches."""
    a._observed_short_size[market_index] = size if sign == -1 else 0.0
    a._observed_position_meta[market_index] = {
        "sign": sign,
        "position": size,
        "avg_entry_price": avg_entry,
        "unrealized_pnl": unrealized,
    }
```

(Note: the seeded test currently uses `sign=-1` for shorts, which is what the helper produces. Longs don't appear in `_observed_short_size`.)

- [ ] **Step 2: Run the test, expect it to FAIL because the new fields don't exist yet**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py::test_on_account_update_extracts_positions_and_collateral -v`
Expected: FAIL with `AttributeError: 'LighterAdapter' object has no attribute '_observed_short_size'` (or similar).

- [ ] **Step 3: Add the new fields to `LighterAdapter.__init__`**

In `exchanges/lighter.py`, replace the existing `_ws_account_positions` line and add the metadata dict alongside. Find the block at lines 139-143 (after the comment `# get_collateral, get_oracle_prices) read from here — no HTTP.`):

Replace:

```python
        self._ws_book_top: dict[int, dict] = {}
        self._ws_account_positions: dict[int, dict] = {}
        self._ws_collateral: float | None = None
```

With:

```python
        self._ws_book_top: dict[int, dict] = {}
        # Per the position-truth redesign (2026-05-07): split observed
        # state into the magnitude (engine-facing, unsigned) and the
        # metadata (diagnostic-facing). The engine drives drift off
        # `_observed_short_size` only; `_observed_position_meta` keeps
        # entry price / unrealized PnL / sign for `get_position`.
        self._observed_short_size: dict[int, float] = {}
        self._observed_position_meta: dict[int, dict] = {}
        self._ws_collateral: float | None = None
```

- [ ] **Step 4: Update `_on_account_update` to write the new fields**

In `exchanges/lighter.py:441-469`, replace the parser body that writes `new_positions`. Find the loop:

```python
            if isinstance(positions, dict):
                for key, pos in positions.items():
                    ...
                    new_positions[mid] = {
                        "sign": int(pos.get("sign", 1)),
                        "position": size,
                        "avg_entry_price": float(...),
                        "unrealized_pnl": float(...),
                    }
            self._ws_account_positions = new_positions
```

Replace with:

```python
            new_short_size: dict[int, float] = {}
            new_position_meta: dict[int, dict] = {}
            if isinstance(positions, dict):
                for key, pos in positions.items():
                    if not isinstance(pos, dict):
                        continue
                    try:
                        mid = int(pos.get("market_id", key))
                    except (TypeError, ValueError):
                        continue
                    try:
                        size = float(pos.get("position", 0) or 0)
                    except (TypeError, ValueError):
                        size = 0.0
                    if size <= 0:
                        # Closed position — skip both dicts.
                        continue
                    sign = int(pos.get("sign", 1))
                    # `_observed_short_size` is the engine-facing magnitude.
                    # Only count short positions (sign=-1); longs get 0 so
                    # the engine's drift math doesn't try to "hedge" them.
                    new_short_size[mid] = size if sign == -1 else 0.0
                    new_position_meta[mid] = {
                        "sign": sign,
                        "position": size,
                        "avg_entry_price": float(
                            pos.get("avg_entry_price", 0) or 0
                        ),
                        "unrealized_pnl": float(
                            pos.get("unrealized_pnl", 0) or 0
                        ),
                    }
            self._observed_short_size = new_short_size
            self._observed_position_meta = new_position_meta
```

- [ ] **Step 5: Update `get_position` to read from the new fields**

In `exchanges/lighter.py:818-835`, replace:

```python
    async def get_position(self, symbol: str) -> Position | None:
        """Read the cached position from the WS account_all subscription.
        Returns None if the position is closed or the WS hasn't sent a
        snapshot yet."""
        meta = self._market_meta_or_raise(symbol)
        cached = self._ws_account_positions.get(meta.market_index)
        if cached is None:
            return None
        size_signed = cached["position"] * cached["sign"]
        if abs(size_signed) < 1e-12:
            return None
        return Position(
            symbol=symbol,
            side="short" if size_signed < 0 else "long",
            size=abs(size_signed),
            entry_price=cached["avg_entry_price"],
            unrealized_pnl=cached["unrealized_pnl"],
        )
```

With:

```python
    async def get_position(self, symbol: str) -> Position | None:
        """RAW WS-observed position (no fusion with locally-stamped
        expected state). Used by the reconciler and diagnostic tooling.

        The hedge engine MUST NOT call this directly for drift math —
        use `get_effective_position` instead, which protects against
        the over-hedge race documented in the 2026-05-07 spec.
        """
        meta = self._market_meta_or_raise(symbol)
        meta_d = self._observed_position_meta.get(meta.market_index)
        if meta_d is None:
            return None
        size_signed = meta_d["position"] * meta_d["sign"]
        if abs(size_signed) < 1e-12:
            return None
        return Position(
            symbol=symbol,
            side="short" if size_signed < 0 else "long",
            size=abs(size_signed),
            entry_price=meta_d["avg_entry_price"],
            unrealized_pnl=meta_d["unrealized_pnl"],
        )
```

- [ ] **Step 6: Run the renamed test, expect it to PASS**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py::test_on_account_update_extracts_positions_and_collateral -v`
Expected: PASS.

- [ ] **Step 7: Run the dependent tests that use `_seed_position`, expect PASS**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py::test_get_position_reads_from_ws_cache -v`
Expected: PASS (the helper now writes both new dicts).

- [ ] **Step 8: Run the full lighter test file**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py -v`
Expected: all 14 tests PASS.

- [ ] **Step 9: Commit**

```bash
git add exchanges/lighter.py tests/test_lighter_adapter.py
git commit -m "$(cat <<'EOF'
refactor(lighter): split observed positions into short_size + metadata

Renames `_ws_account_positions` to `_observed_short_size` (unsigned
magnitude — the only field the hedge engine needs) and parks the
diagnostic fields (sign, entry, unrealized) in `_observed_position_meta`.
First task of the 2026-05-07 position-truth redesign — sets up the
shape that `_expected_short_size` and `get_effective_position` will
plug into in subsequent tasks.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: Add `_expected_short_size` + `_last_fire_at` fields

Adds the empty containers for the next layer. No behavior change yet — just the slots that subsequent tasks fill.

**Files:**
- Modify: `exchanges/lighter.py` (init block — same area as Task 1)
- Test: `tests/test_lighter_adapter.py` (new)

- [ ] **Step 1: Write a failing test that expects the new fields**

Add to `tests/test_lighter_adapter.py` (anywhere after the existing `test_size_below_step_raises`):

```python
@pytest.mark.asyncio
async def test_adapter_inits_expected_state_dicts():
    """The position-truth redesign requires `_expected_short_size` and
    `_last_fire_at` to start empty on a freshly constructed adapter.
    Subsequent tasks stamp them on `place_long_term_order` success."""
    a = _make_adapter()
    assert a._expected_short_size == {}
    assert a._last_fire_at == {}
```

- [ ] **Step 2: Run, expect FAIL**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py::test_adapter_inits_expected_state_dicts -v`
Expected: FAIL with `AttributeError: 'LighterAdapter' object has no attribute '_expected_short_size'`.

- [ ] **Step 3: Add the fields in `__init__`**

In `exchanges/lighter.py`, find the block right after `_observed_position_meta` from Task 1, and add the new dicts. Insert this AFTER `self._ws_collateral: float | None = None`:

```python
        # Locally-stamped expected magnitude per market_id. Written by
        # `place_long_term_order` when `create_order` returns
        # err=None (server-accept), regardless of `_verify_fill`.
        # Reset by `_reconciler_loop` when the WS-observed value catches
        # up, OR when an HTTP authoritative query confirms the truth.
        # See spec/2026-05-07-position-truth-redesign-design.md §
        # "Reconciliation logic" for the stamping/reset rules.
        self._expected_short_size: dict[int, float] = {}
        # Monotonic timestamp of the latest successful create_order on
        # this market_id. Reconciler measures HTTP-query timeout from
        # this; rewritten on every fire so a chain of fires within
        # 30 s waits for the latest fire to age out.
        self._last_fire_at: dict[int, float] = {}
```

- [ ] **Step 4: Run, expect PASS**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py::test_adapter_inits_expected_state_dicts -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add exchanges/lighter.py tests/test_lighter_adapter.py
git commit -m "$(cat <<'EOF'
feat(lighter): add _expected_short_size and _last_fire_at slots

Empty containers; behavior added in subsequent tasks (stamping,
reconciler, accessor). Part of the 2026-05-07 position-truth redesign.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: Stamp `_expected_short_size` on `place_long_term_order` success

The core behavior change. After `create_order` returns `err=None` (server accepted), the adapter assumes the order will fill and updates the local expected magnitude. **Independent of `_verify_fill` outcome** — that's the entire point.

**Files:**
- Modify: `exchanges/lighter.py:_place_long_term_order_unlocked` (around line 580–760, the success path inside the retry loop)
- Test: `tests/test_lighter_adapter.py` (new)

- [ ] **Step 1: Write a failing test**

Add to `tests/test_lighter_adapter.py`:

```python
@pytest.mark.asyncio
async def test_place_order_stamps_expected_on_server_accept():
    """When create_order returns err=None, _expected_short_size must be
    stamped with the order size — even if _verify_fill returns 0. This
    is the regression test for the 2026-05-07 over-hedge incidents.
    Sell increments; buy decrements (clamp at 0)."""
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta()
    _seed_book(a, 0, bid=2399.0, ask=2400.0)
    a._signer = MagicMock()
    a._signer.nonce_manager.next_nonce = MagicMock(return_value=(0, 1))
    a._signer.create_order = AsyncMock(
        return_value=(None, MagicMock(tx_hash="0xabc"), None)
    )
    # Force _verify_fill to LIE: return 0 (no fill confirmed) — this is
    # the exact failure mode that produced over-hedge today. Stamping
    # must happen anyway because err is None.
    async def fake_verify(meta, cloid_int, expected_size):
        return 0.0, 0.0
    a._verify_fill = fake_verify  # type: ignore
    a.get_position = AsyncMock(return_value=None)

    await a.place_long_term_order(
        symbol="ETH-USD", side="sell", size=0.0148, price=0,
        cloid_int=42,
    )
    # Stamped despite verify_fill=0:
    assert a._expected_short_size[0] == 0.0148
    # Timestamp is set:
    assert 0 in a._last_fire_at

    # A subsequent BUY 0.005 (covering the short) decrements:
    await a.place_long_term_order(
        symbol="ETH-USD", side="buy", size=0.005, price=0,
        cloid_int=43,
    )
    assert abs(a._expected_short_size[0] - (0.0148 - 0.005)) < 1e-9

    # A BUY larger than the current expected clamps at 0 (can't go below).
    await a.place_long_term_order(
        symbol="ETH-USD", side="buy", size=1.0, price=0,
        cloid_int=44,
    )
    assert a._expected_short_size[0] == 0.0
```

- [ ] **Step 2: Run, expect FAIL (KeyError on `_expected_short_size[0]`)**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py::test_place_order_stamps_expected_on_server_accept -v`
Expected: FAIL — the dict stays empty because no stamping happens yet.

- [ ] **Step 3: Add stamping to the success path of `_place_long_term_order_unlocked`**

Open `exchanges/lighter.py` and find `_place_long_term_order_unlocked`. Inside the `for attempt in range(_TAKER_RETRIES):` loop, locate the `if err is not None:` branch (followed by the success continuation right after). Find the lines that look like:

```python
            tx, resp, err = await self._signer.create_order(
                ...
                api_key_index=api_key_idx,
                nonce=nonce,
            )
            if err is not None:
                # Exchange-side error...
                ...
                continue

            # CRITICAL — once create_order returns err=None, the exchange
            # accepted the order. Whether IOC filled or auto-cancelled,
            # the tx exists. We MUST NOT retry — retrying after a server
            # accept means a SECOND order on the same side, which stacks
            # short positions...
            ...
            fill_size, fill_price = await self._verify_fill(...)
```

Right after the `if err is not None: ... continue` block ends and BEFORE the `fill_size, fill_price = await self._verify_fill(...)` call, insert the stamping logic:

```python
            # Server accepted the order. Stamp the LOCAL expected_short_size
            # IMMEDIATELY — independent of `_verify_fill`. The whole point
            # of the position-truth redesign (2026-05-07) is that
            # verify_fill and the WS account snapshot are both
            # eventually-consistent and have produced over-hedge stacks
            # by under-reporting position right after a fill. With the
            # stamp in place, `get_effective_position` returns the new
            # expected size on the very next iter, drift goes to 0,
            # engine doesn't re-fire. The reconciler resolves any genuine
            # non-fill (IOC auto-cancel) at the 30 s timeout via HTTP.
            mid = meta.market_index
            if is_ask:  # side == "sell" — short increases
                self._expected_short_size[mid] = (
                    self._expected_short_size.get(mid, 0.0) + size
                )
            else:  # side == "buy" — covering short, decrement clamped at 0
                cur = self._expected_short_size.get(mid, 0.0)
                self._expected_short_size[mid] = max(0.0, cur - size)
            self._last_fire_at[mid] = time.monotonic()

```

(`is_ask` is the local variable derived from `side == "sell"` near the top of the function. `meta` and `size` are also local. `time` is already imported.)

- [ ] **Step 4: Run the new test, expect PASS**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py::test_place_order_stamps_expected_on_server_accept -v`
Expected: PASS.

- [ ] **Step 5: Run the existing place-order tests to make sure nothing broke**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py -v -k "place_order"`
Expected: all PASS (4 existing place-order tests + 1 new one).

- [ ] **Step 6: Commit**

```bash
git add exchanges/lighter.py tests/test_lighter_adapter.py
git commit -m "$(cat <<'EOF'
feat(lighter): stamp _expected_short_size on create_order server-accept

Sell increments, buy decrements (clamp at 0). Independent of
_verify_fill outcome — the whole point of the redesign is that the
verify lookup is unreliable. Combined with `get_effective_position`
(next task) this makes the over-hedge race structurally impossible.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: Implement `get_effective_position`

The single accessor the engine will use for drift math. Returns `Position` whose size is `max(observed_short_size, expected_short_size)` for the symbol.

**Files:**
- Modify: `exchanges/lighter.py` (add new method below `get_position` — around line 836)
- Test: `tests/test_lighter_adapter.py` (new)

- [ ] **Step 1: Write a failing test that exercises all four state combinations**

Add to `tests/test_lighter_adapter.py`:

```python
@pytest.mark.asyncio
async def test_get_effective_position_uses_max_of_observed_and_expected():
    """`get_effective_position(symbol)` must return a Position whose
    size is max(observed, expected). This is what the engine reads as
    `current_short_size` — covering both the WS-observed-only case
    (steady state) and the just-stamped-not-yet-WS-confirmed case
    (immediately after fire, before WS catch-up)."""
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta()

    # Case 1: both empty → None.
    assert await a.get_effective_position("ETH-USD") is None

    # Case 2: only observed populated (WS pushed; we never fired).
    _seed_position(
        a, market_index=0, sign=-1, size=0.05,
        avg_entry=2390.0, unrealized=1.20,
    )
    pos = await a.get_effective_position("ETH-USD")
    assert pos is not None
    assert pos.size == 0.05
    assert pos.side == "short"

    # Case 3: only expected populated (just fired, WS hasn't caught up).
    a._observed_short_size = {}
    a._observed_position_meta = {}
    a._expected_short_size = {0: 0.0148}
    pos = await a.get_effective_position("ETH-USD")
    assert pos is not None
    assert pos.size == 0.0148
    assert pos.side == "short"

    # Case 4: both populated, expected is larger (just fired delta on
    # top of an existing short — engine sees the LARGER value to
    # avoid re-firing).
    _seed_position(
        a, market_index=0, sign=-1, size=0.0148,
        avg_entry=2390.0,
    )
    a._expected_short_size = {0: 0.020}
    pos = await a.get_effective_position("ETH-USD")
    assert pos.size == 0.020
```

- [ ] **Step 2: Run, expect FAIL with AttributeError**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py::test_get_effective_position_uses_max_of_observed_and_expected -v`
Expected: FAIL — `AttributeError: 'LighterAdapter' object has no attribute 'get_effective_position'`.

- [ ] **Step 3: Add `get_effective_position` to the adapter**

In `exchanges/lighter.py`, immediately after the existing `get_position` method (around line 836, before `get_oracle_prices`), add:

```python
    async def get_effective_position(self, symbol: str) -> Position | None:
        """Returns the position the hedge engine should drive drift
        against. Fuses the WS-observed magnitude with the locally-
        stamped expected magnitude:

            size = max(_observed_short_size, _expected_short_size)

        Right after a successful `place_long_term_order`, expected jumps
        to the new total. WS observed lags (eventually-consistent up to
        ~30 s under load). Without this fusion the engine would read
        observed=0 immediately after a fill, compute drift=target,
        fire ANOTHER order — that's the over-hedge stack from
        2026-05-07. The fusion makes the race structurally impossible.

        Returns None when both layers report 0 (closed position).
        """
        meta = self._market_meta_or_raise(symbol)
        mid = meta.market_index
        observed = self._observed_short_size.get(mid, 0.0)
        expected = self._expected_short_size.get(mid, 0.0)
        size = max(observed, expected)
        if size <= 0:
            return None
        # Metadata (entry, unrealized PnL) only available when WS has
        # reported observed. If we're in the just-fired window with
        # observed=0, return placeholder zeros — the engine doesn't
        # use these for drift, only for display.
        meta_d = self._observed_position_meta.get(mid)
        return Position(
            symbol=symbol,
            side="short",
            size=size,
            entry_price=(meta_d or {}).get("avg_entry_price", 0.0),
            unrealized_pnl=(meta_d or {}).get("unrealized_pnl", 0.0),
        )
```

- [ ] **Step 4: Run the new test, expect PASS**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py::test_get_effective_position_uses_max_of_observed_and_expected -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add exchanges/lighter.py tests/test_lighter_adapter.py
git commit -m "$(cat <<'EOF'
feat(lighter): add get_effective_position fusing observed + expected

Single accessor the engine uses for drift math. Returns Position with
size = max(observed_short_size, expected_short_size). Combined with
the stamping in Task 3, this makes the over-hedge race structurally
impossible: right after fire, expected jumps; engine reads it; drift
goes to 0; no re-fire even if WS lags.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: HTTP authoritative fetch helper

Tiny method that the reconciler will call when WS doesn't catch up after the timeout. Wraps `AccountApi.account` with the right error handling.

**Files:**
- Modify: `exchanges/lighter.py` (add private method, alongside `get_position` and friends)
- Test: `tests/test_lighter_adapter.py` (new)

- [ ] **Step 1: Write a failing test**

Add to `tests/test_lighter_adapter.py`:

```python
@pytest.mark.asyncio
async def test_fetch_short_size_via_http_returns_short_magnitude():
    """`_fetch_short_size_via_http(market_index)` must call /account,
    find the position for the given market_id, and return:
      - the unsigned magnitude if it's a short (sign=-1),
      - 0.0 if flat or long,
      - None on error (so the caller skips reconciliation that scan)."""
    a = _make_adapter()
    a._account_index = 724201

    # Stub: /account returns a short of 0.05 ETH on market_id=0.
    a._account_api = MagicMock()
    a._account_api.account = AsyncMock(return_value=MagicMock(
        accounts=[MagicMock(positions=[
            MagicMock(market_id=0, sign=-1, position="0.05"),
            MagicMock(market_id=50, sign=1, position="100.0"),  # long
        ])]
    ))
    assert await a._fetch_short_size_via_http(0) == 0.05
    assert await a._fetch_short_size_via_http(50) == 0.0  # long → 0
    assert await a._fetch_short_size_via_http(99) == 0.0  # not in list → 0

    # Stub: HTTP raises (network blip) → return None.
    a._account_api.account = AsyncMock(side_effect=RuntimeError("net err"))
    assert await a._fetch_short_size_via_http(0) is None
```

- [ ] **Step 2: Run, expect FAIL**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py::test_fetch_short_size_via_http_returns_short_magnitude -v`
Expected: FAIL — `AttributeError: ... no attribute '_fetch_short_size_via_http'`.

- [ ] **Step 3: Add the helper**

In `exchanges/lighter.py`, immediately after `get_effective_position` (which Task 4 added), add:

```python
    async def _fetch_short_size_via_http(self, market_index: int) -> float | None:
        """Authoritative HTTP query for the current short magnitude.
        Used by the reconciler when WS hasn't caught up to expected
        within the timeout. Returns:
          - the unsigned magnitude if the position is a short (sign=-1),
          - 0.0 if the position is flat or long (we don't track longs),
          - None on HTTP/parse error so the caller can skip and retry
            on the next reconciler scan without overwriting state.
        """
        try:
            resp = await self._account_api.account(
                by="index", value=str(self._account_index),
            )
        except Exception as e:
            logger.warning(
                f"_fetch_short_size_via_http({market_index}) failed: {e}"
            )
            return None
        accounts = getattr(resp, "accounts", None) or []
        if not accounts:
            return 0.0
        positions = getattr(accounts[0], "positions", None) or []
        for pos in positions:
            try:
                pos_mid = int(getattr(pos, "market_id"))
            except (TypeError, ValueError):
                continue
            if pos_mid != market_index:
                continue
            sign = int(getattr(pos, "sign", 1))
            if sign != -1:
                return 0.0  # long or flat → no short to track
            try:
                return float(getattr(pos, "position", 0))
            except (TypeError, ValueError):
                return 0.0
        return 0.0  # market not in account → no position
```

- [ ] **Step 4: Run the new test, expect PASS**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py::test_fetch_short_size_via_http_returns_short_magnitude -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add exchanges/lighter.py tests/test_lighter_adapter.py
git commit -m "$(cat <<'EOF'
feat(lighter): _fetch_short_size_via_http authoritative helper

Wraps AccountApi.account with the right error handling for the
reconciler. Returns short magnitude, 0 for long/flat, None on error.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 6: Race-safe `_reconcile_once` step

The body of one reconciler scan iteration. Snapshots `(expected, last_fire)` at scan start and re-checks `last_fire_at` after the HTTP await — if a new fire happened in between, abort the reconcile so we don't overwrite a fresher stamp with stale truth.

**Files:**
- Modify: `exchanges/lighter.py` (add private method, near the WS handlers)
- Test: `tests/test_lighter_adapter.py` (new — covers 3 cases: catch-up, timeout-confirms-fill, timeout-confirms-failure)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_lighter_adapter.py`:

```python
@pytest.mark.asyncio
async def test_reconcile_once_clears_expected_when_observed_catches_up():
    """If WS catch-up makes observed match expected within step_size,
    the reconciler pins expected to observed (no HTTP query)."""
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta(market_index=0, size_decimals=4)  # step=0.0001
    a._expected_short_size[0] = 0.0148
    a._observed_short_size[0] = 0.0148
    a._last_fire_at[0] = time.monotonic()
    a._account_api = MagicMock()
    a._account_api.account = AsyncMock(side_effect=AssertionError(
        "HTTP must NOT be called when observed already caught up"
    ))
    await a._reconcile_once()
    # Expected pinned to observed (effectively a no-op here since
    # they were already equal — the assertion is that no HTTP fired).


@pytest.mark.asyncio
async def test_reconcile_once_http_query_on_timeout_confirms_fill():
    """If divergence persists past RECONCILE_TIMEOUT_S since the last
    fire, query HTTP. When HTTP confirms the expected size, both
    observed and expected pin to that truth."""
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta(market_index=0, size_decimals=4)
    a._expected_short_size[0] = 0.0148
    a._observed_short_size[0] = 0.0  # WS lagging
    a._last_fire_at[0] = time.monotonic() - 31.0  # past timeout
    a._account_api = MagicMock()
    a._account_api.account = AsyncMock(return_value=MagicMock(
        accounts=[MagicMock(positions=[
            MagicMock(market_id=0, sign=-1, position="0.0148"),
        ])]
    ))
    await a._reconcile_once()
    assert a._observed_short_size[0] == 0.0148
    assert a._expected_short_size[0] == 0.0148


@pytest.mark.asyncio
async def test_reconcile_once_http_zero_means_real_failure():
    """If divergence persists and HTTP returns 0 (the order genuinely
    didn't fill — IOC auto-cancel), reset BOTH layers to 0. The engine
    will see drift again on the next iter and fire once more."""
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta(market_index=0, size_decimals=4)
    a._expected_short_size[0] = 0.0148
    a._observed_short_size[0] = 0.0
    a._last_fire_at[0] = time.monotonic() - 31.0
    a._account_api = MagicMock()
    a._account_api.account = AsyncMock(return_value=MagicMock(
        accounts=[MagicMock(positions=[])]  # no position on this market
    ))
    await a._reconcile_once()
    assert a._observed_short_size[0] == 0.0
    assert a._expected_short_size[0] == 0.0


@pytest.mark.asyncio
async def test_reconcile_once_aborts_on_concurrent_fire():
    """If a new place_long_term_order stamps `_last_fire_at` mid-await,
    the reconciler must NOT overwrite the new (higher) expected with
    stale HTTP truth. Race protection from spec § Reconciliation logic."""
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta(market_index=0, size_decimals=4)
    a._expected_short_size[0] = 0.0148
    a._observed_short_size[0] = 0.0
    initial_fire_at = time.monotonic() - 31.0
    a._last_fire_at[0] = initial_fire_at

    async def fake_account(**kw):
        # Simulate a new fire happening DURING the HTTP await.
        a._last_fire_at[0] = time.monotonic()
        a._expected_short_size[0] = 0.030  # stamped by hypothetical concurrent place_order
        return MagicMock(
            accounts=[MagicMock(positions=[
                MagicMock(market_id=0, sign=-1, position="0.0148"),
            ])]
        )
    a._account_api = MagicMock()
    a._account_api.account = AsyncMock(side_effect=fake_account)

    await a._reconcile_once()
    # Expected stayed at 0.030 (the new stamp), NOT 0.0148 (stale truth).
    assert a._expected_short_size[0] == 0.030
    # Observed unchanged — reconciler aborted the write.
    assert a._observed_short_size[0] == 0.0
```

- [ ] **Step 2: Run, expect FAIL on first test**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py -v -k "reconcile_once"`
Expected: 4 FAILs — method doesn't exist.

- [ ] **Step 3: Implement `_reconcile_once`**

In `exchanges/lighter.py`, near the WS callbacks (after `_on_account_update`, around line 472), add the constant and method:

```python
    # Reconciler tunables. The redesign treats 30 s as "WS should have
    # delivered by now under any normal load — if we're still seeing
    # divergence, query HTTP authoritative". Spec discusses the
    # tradeoffs in § Constants.
    RECONCILE_TIMEOUT_S = 30.0

    def _step_size_for_mid(self, mid: int) -> float:
        """Tolerance for treating observed as matching expected — one
        size tick. Pulled from the per-market metadata cached at connect."""
        for meta in self._markets.values():
            if meta.market_index == mid:
                return meta.step_size
        return 1e-9  # unknown market — strict equality

    async def _reconcile_once(self) -> None:
        """One pass over `_expected_short_size`. For each entry:
          1. If observed already matches expected (within step size),
             pin expected to observed and clear local credit.
          2. Else if elapsed since last fire > RECONCILE_TIMEOUT_S,
             query HTTP authoritative and pin both layers to the
             returned truth — UNLESS a concurrent fire stamped
             `_last_fire_at` mid-await, in which case abort to avoid
             overwriting fresher state with stale truth.
        """
        # Snapshot the current state so per-symbol decisions reference
        # a consistent picture, not values that mutated during awaits.
        snapshot = {
            mid: (
                self._expected_short_size.get(mid, 0.0),
                self._last_fire_at.get(mid, 0.0),
            )
            for mid in list(self._expected_short_size.keys())
        }
        for mid, (expected_at_scan, last_fire_at_scan) in snapshot.items():
            observed = self._observed_short_size.get(mid, 0.0)
            tol = self._step_size_for_mid(mid)
            # Catch-up case: WS already shows expected. Pin and move on.
            if abs(expected_at_scan - observed) <= tol:
                # Only commit if no new fire happened — otherwise the
                # newer expected is more current than `observed`.
                if self._last_fire_at.get(mid, 0.0) == last_fire_at_scan:
                    self._expected_short_size[mid] = observed
                continue
            # Timeout case: if not enough time since last fire, wait.
            elapsed = time.monotonic() - last_fire_at_scan
            if elapsed <= self.RECONCILE_TIMEOUT_S:
                continue
            # Authoritative query.
            truth = await self._fetch_short_size_via_http(mid)
            if truth is None:
                continue  # HTTP failed — retry next scan
            # Race guard: did a fire happen during our await?
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

- [ ] **Step 4: Run, expect 4 PASS**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py -v -k "reconcile_once"`
Expected: 4 PASS.

- [ ] **Step 5: Commit**

```bash
git add exchanges/lighter.py tests/test_lighter_adapter.py
git commit -m "$(cat <<'EOF'
feat(lighter): _reconcile_once race-safe authoritative reconciliation

One scan iteration: pin expected to observed when WS catches up,
or fall through to HTTP authoritative query on timeout. Re-checks
_last_fire_at after the HTTP await so concurrent fires don't get
overwritten by stale truth. Spec §§ Reconciliation logic.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 7: Background `_reconciler_loop` task wired into `connect`/`disconnect`

The persistent task that calls `_reconcile_once` every 5 s. Started in `connect`, cancelled in `disconnect`.

**Files:**
- Modify: `exchanges/lighter.py:connect` (around line 230, where `_ws_task` is started), `exchanges/lighter.py:disconnect` (around line 250)
- Test: `tests/test_lighter_adapter.py` (new)

- [ ] **Step 1: Write a failing test**

Add to `tests/test_lighter_adapter.py`:

```python
@pytest.mark.asyncio
async def test_reconciler_loop_invokes_reconcile_once_periodically():
    """The background loop must invoke `_reconcile_once` repeatedly,
    sleeping between iterations. Test by patching the method to count
    invocations and the sleep to no-op."""
    import asyncio
    a = _make_adapter()
    invocations = []

    async def fake_reconcile_once():
        invocations.append(time.monotonic())
        if len(invocations) >= 3:
            a._ws_closing = True  # exit the loop

    a._reconcile_once = fake_reconcile_once  # type: ignore

    # Patch sleep to a noop so the test doesn't take 15 s.
    real_sleep = asyncio.sleep
    async def fast_sleep(s):
        await real_sleep(0)
    import unittest.mock as mock
    with mock.patch("asyncio.sleep", fast_sleep):
        await a._reconciler_loop()
    assert len(invocations) >= 3
```

- [ ] **Step 2: Run, expect FAIL**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py::test_reconciler_loop_invokes_reconcile_once_periodically -v`
Expected: FAIL — method doesn't exist.

- [ ] **Step 3: Implement `_reconciler_loop`**

In `exchanges/lighter.py`, immediately after `_reconcile_once` from Task 6, add:

```python
    async def _reconciler_loop(self) -> None:
        """Background task that calls `_reconcile_once` every 5 s for
        the lifetime of the adapter. Started in `connect()`, cancelled
        in `disconnect()`. Catches per-iteration exceptions so a
        transient HTTP error doesn't crash the loop — next scan retries.
        """
        while not self._ws_closing:
            try:
                await self._reconcile_once()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(
                    f"Reconciler iteration failed: {type(e).__name__}: {e}"
                )
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                return
```

- [ ] **Step 4: Wire start/cancel into connect/disconnect**

In `exchanges/lighter.py`, find the existing `connect()` block where the WS task is started. Look for:

```python
        self._ws_closing = False
        self._ws_task = asyncio.create_task(self._run_ws_pump())
```

Append AFTER that, in the same block:

```python
        # Background reconciler — resolves persistent observed/expected
        # divergence via HTTP authoritative query. See spec/2026-05-07-
        # position-truth-redesign-design.md § Reconciliation logic.
        self._reconcile_task = asyncio.create_task(self._reconciler_loop())
```

In the existing `disconnect()` method (around line 250), find the existing `_ws_task` cancellation:

```python
        self._ws_closing = True
        if self._ws_task is not None and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
```

Append IMMEDIATELY AFTER (before the signer/api_client close lines):

```python
        # Cancel the reconciler task too — symmetric with WS.
        rec_task = getattr(self, "_reconcile_task", None)
        if rec_task is not None and not rec_task.done():
            rec_task.cancel()
            try:
                await rec_task
            except (asyncio.CancelledError, Exception):
                pass
```

Also add the field initialization in `__init__`. Find the line `self._ws_task: asyncio.Task | None = None` and add immediately after:

```python
        self._reconcile_task: asyncio.Task | None = None
```

- [ ] **Step 5: Run the loop test, expect PASS**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_lighter_adapter.py::test_reconciler_loop_invokes_reconcile_once_periodically -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add exchanges/lighter.py tests/test_lighter_adapter.py
git commit -m "$(cat <<'EOF'
feat(lighter): _reconciler_loop background task in connect/disconnect

5 s scan period. Catches per-iteration errors so a transient HTTP
blip doesn't kill the loop. Cancelled symmetrically alongside
_ws_task in disconnect.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 8: Engine `_safe_get_position` calls `get_effective_position`

The single line change in the engine. Adapter is now wired up; engine just consumes the new accessor.

**Files:**
- Modify: `engine/__init__.py:1079-1084` (`_safe_get_position`)
- Test: re-run existing engine tests to confirm nothing broke

- [ ] **Step 1: Update `_safe_get_position`**

In `engine/__init__.py`, find:

```python
    async def _safe_get_position(self, symbol: str | None = None):
        sym = symbol if symbol is not None else self._settings.dydx_symbol
        try:
            return await self._exchange.get_position(sym)
        except Exception:
            return None
```

Replace with:

```python
    async def _safe_get_position(self, symbol: str | None = None):
        """Returns the position the engine should drive drift against.

        On the LighterAdapter this returns `get_effective_position`,
        which fuses WS-observed state with locally-stamped expected
        state from recent fires — making the over-hedge race
        documented in 2026-05-07 structurally impossible. Adapters
        that don't implement `get_effective_position` (e.g. test
        mocks, alternative exchanges) fall back to `get_position`.
        """
        sym = symbol if symbol is not None else self._settings.dydx_symbol
        try:
            getter = getattr(
                self._exchange, "get_effective_position", None,
            )
            if getter is None:
                return await self._exchange.get_position(sym)
            return await getter(sym)
        except Exception:
            return None
```

- [ ] **Step 2: Run engine tests, expect PASS**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_grid.py tests/test_engine_dual_leg.py -v`
Expected: all PASS — the fallback to `get_position` keeps existing mock-based tests green; tests that use a real LighterAdapter pick up the new accessor.

- [ ] **Step 3: Commit**

```bash
git add engine/__init__.py
git commit -m "$(cat <<'EOF'
feat(engine): _safe_get_position prefers get_effective_position

Falls back to get_position when the adapter doesn't expose the new
accessor (test mocks, future exchanges). Single-line behavioral
swap that makes the position-truth redesign live in the hot path.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 9: Remove engine cooldown — subsumed by adapter guard

The 30 s per-leg cooldown was a band-aid for the verify_fill race. The adapter's expected_short_size guard makes it redundant.

**Files:**
- Modify: `engine/__init__.py:82-93` (init), `engine/__init__.py:1102-1132` (the cooldown branch in `_maybe_rebalance_leg`)
- Test: existing tests in `tests/test_engine_dual_leg.py` already cover the relevant paths; some need updating

- [ ] **Step 1: Update existing test that asserts the cooldown is enforced (if any)**

Search the test file:

Run: `grep -nE "REBALANCE_COOLDOWN_S|_last_rebalance_at_per_leg" tests/test_engine_dual_leg.py tests/test_engine_grid.py`

If a test asserts cooldown-skip-on-fast-second-fire, remove or rewrite it. The new design's protection is at the adapter layer; engine no longer cooldowns. If no such test exists, this step is a no-op.

- [ ] **Step 2: Remove cooldown init in `__init__`**

In `engine/__init__.py`, find:

```python
        # Per-leg taker cooldown — when we fire a correction taker, we
        # block any further taker on THAT leg for REBALANCE_COOLDOWN_S
        # seconds. Defense in depth against the over-hedge mechanism we
        # observed on 2026-05-07: order fills, but verify_fill or the
        # WS-cached position read briefly returns 0 (eventually-
        # consistent), engine reads drift = target → fires another
        # taker → fills → over-hedge. Even with a working WS parser,
        # this cooldown caps the damage at 1 fill per cooldown window.
        self._last_rebalance_at_per_leg: dict[str, float] = {}
        self.REBALANCE_COOLDOWN_S = 30.0
```

Delete it entirely (the whole block from `# Per-leg taker cooldown` through `self.REBALANCE_COOLDOWN_S = 30.0` inclusive).

- [ ] **Step 3: Remove the cooldown branch in `_maybe_rebalance_leg`**

In `engine/__init__.py`, find inside `_maybe_rebalance_leg`:

```python
        last = self._last_rebalance_at_per_leg.get(symbol, 0.0)
        cooldown_left = self.REBALANCE_COOLDOWN_S - (time.monotonic() - last)
        if cooldown_left > 0:
            logger.debug(
                f"Rebalance cooldown [{symbol}]: {cooldown_left:.1f}s left, skipping"
            )
            return
```

And also the stamp:

```python
        # Stamp the cooldown BEFORE the await — even if place_long_term_order
        # raises mid-flight (network blip), we don't want a tight retry
        # loop to fire the next iter and double-up.
        self._last_rebalance_at_per_leg[symbol] = time.monotonic()
```

Delete BOTH blocks. Update the docstring to remove the "Per-leg cooldown" paragraph (the one that starts with "Per-leg cooldown: after firing a taker, ..." and ends at the closing `"""`). The remaining docstring should mention that the over-hedge guard now lives in the adapter.

After edits, the relevant function should read approximately:

```python
    async def _maybe_rebalance_leg(
        self, *, symbol: str, target: float, current: float,
        min_notional: float, ref_price: float,
    ) -> None:
        """Level-triggered taker: fire market order when |drift| * ref_price >= min_notional.

        target: desired short size in token base units (e.g. 100.0 ARB).
        current: current absolute short size in same units.
        min_notional: exchange minimum order notional in USD.
        ref_price: USD price of the leg's token (used both as the filter
          threshold and to compute the cross-spread price for the market order).

        Cross-spread convention for taker:
          side=sell -> price = ref_price * 0.999 (cross the bid)
          side=buy  -> price = ref_price * 1.001 (cross the ask)

        Over-hedge protection lives in the LighterAdapter's
        `get_effective_position` (see 2026-05-07 position-truth redesign).
        Engine reads `current` via `_safe_get_position`, which now
        returns the fused observed+expected magnitude — drift goes to 0
        right after a successful fire, so re-fire is impossible during
        WS lag. No engine-level cooldown needed.
        """
        drift = target - current
        notional_drift_usd = abs(drift) * ref_price
        if notional_drift_usd < min_notional:
            return  # sub-level, idle

        side = "sell" if drift > 0 else "buy"
        size = abs(drift)
        cross_price = ref_price * (0.999 if side == "sell" else 1.001)
        cloid = self._next_cloid_for_leg(symbol)
        metrics.aggressive_corrections_total.inc()
        try:
            await self._exchange.place_long_term_order(
                symbol=symbol, side=side, size=size, price=cross_price,
                cloid_int=cloid, ttl_seconds=60,
            )
            # Lighter is zero-fee, so no slippage accumulator on this
            # path. (When dYdX support comes back, wire fee model from
            # adapter meta instead of hardcoding 0.05% here.)
            await self._db.insert_order_log(
                timestamp=time.time(), exchange=self._exchange.name,
                action="place", side=side, size=size, price=cross_price,
                reason=f"level_triggered_{symbol}",
                operation_id=self._hub.current_operation_id,
            )
            logger.info(
                f"Rebalance fire [{symbol}]: {side} {size:.6f} @ ~{cross_price:.4f}"
            )
        except Exception as e:
            logger.exception(f"Rebalance fire failed [{symbol}]: {e}")
```

- [ ] **Step 4: Run engine tests, expect PASS**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_dual_leg.py tests/test_engine_grid.py -v`
Expected: all PASS. If any test fails because it referenced the removed cooldown fields, update or remove it (it was testing a guard that no longer exists).

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_engine_dual_leg.py tests/test_engine_grid.py
git commit -m "$(cat <<'EOF'
refactor(engine): remove per-leg cooldown — subsumed by adapter guard

The cooldown was a band-aid for the verify_fill/WS race that the
position-truth redesign eliminates structurally at the adapter layer.
Engine `current` from `_safe_get_position` now returns the fused
observed+expected magnitude, so drift goes to 0 right after a fire
and re-fire is impossible during WS lag. No engine logic needed.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Task 10: Engine integration regression test

End-to-end test that verifies the over-hedge regression of 2026-05-07 stays fixed. Uses a real `LighterAdapter` (not a MagicMock) so we exercise the actual stamping and fusion paths — a pure-mock test wouldn't catch a regression where stamping breaks.

**Files:**
- Test: `tests/test_engine_dual_leg.py` (add new test at the end)

- [ ] **Step 1: Add the integration regression test**

Append to `tests/test_engine_dual_leg.py`:

```python
@pytest.mark.asyncio
async def test_engine_does_not_double_fire_during_ws_lag():
    """REGRESSION: 2026-05-07 over-hedge incidents (ops #25/#26/#27).

    The bot fired hedge orders, the orders filled on Lighter, but the
    bot's verify_fill returned 0 AND the WS account_all push lagged
    longer than expected. The engine read `current=0`, computed
    `drift=target`, and fired ANOTHER order — stacking 3-5× over.

    The position-truth redesign moved the guard into the adapter:
    `get_effective_position` returns max(observed, expected), and
    `place_long_term_order` stamps `_expected_short_size` on
    `create_order` server-accept (regardless of `_verify_fill`).

    This test wires a REAL LighterAdapter (with the existing sys.modules
    stub for the lighter SDK) into the engine, simulates two iters
    where the WS NEVER pushes the post-fire account update, and asserts
    that the engine fires `place_long_term_order` exactly ONCE.

    A MagicMock-only test wouldn't catch a regression where stamping
    is mistakenly wired back through verify_fill — the adapter's path
    must be exercised end-to-end.
    """
    import asyncio
    # `_install_lighter_stub` runs at import time; LighterAdapter
    # already importable here.
    from exchanges.lighter import LighterAdapter, _MarketMeta

    # Build a real adapter (no connect — we'll wire its internals
    # manually so we don't actually open WS or HTTP).
    a = LighterAdapter(
        url="https://stub", account_index=42,
        api_private_key="0x" + "1" * 64, api_key_index=2,
    )
    a._markets["ETH-USD"] = _MarketMeta(
        symbol_user="ETH-USD", symbol_lighter="ETH",
        market_index=0, price_decimals=2, size_decimals=4,
        tick_size=0.01, step_size=0.0001,
        min_base_amount=0.005, min_quote_amount=10.0,
    )
    # Seed a top-of-book so place_long_term_order can run without WS.
    a._ws_book_top[0] = {
        "best_bid": 2399.0, "best_ask": 2400.0, "ts": time.time(),
    }
    # Real signer is unwanted — replace with a stub that succeeds.
    a._signer = MagicMock()
    a._signer.nonce_manager.next_nonce = MagicMock(return_value=(2, 1))
    a._signer.create_order = AsyncMock(
        return_value=(None, MagicMock(tx_hash="0x" + "a" * 64), None)
    )
    # _verify_fill LIES (returns 0) — this is the failure mode that
    # produced over-hedge today.
    async def lying_verify(meta, cloid_int, expected_size):
        return 0.0, 0.0
    a._verify_fill = lying_verify  # type: ignore

    # WS account snapshot: NEVER updates. _observed_short_size stays
    # empty for the duration of the test. This simulates the worst-case
    # WS lag (>30 s) we observed today.
    # (The reconciler isn't started — we only test the get_effective_position
    # fusion path here, not HTTP authoritative reconciliation.)

    # Hook the adapter into the engine.
    state = StateHub(hedge_ratio=1.0)
    state.current_operation_id = 42
    state.operation_state = "active"
    settings = MagicMock()
    settings.dydx_symbol_token0 = "ETH-USD"
    settings.dydx_symbol_token1 = ""
    db = MagicMock()
    db.add_to_operation_accumulator = AsyncMock()
    db.insert_order_log = AsyncMock()
    pool = MagicMock(); beefy = MagicMock()

    engine = GridMakerEngine(
        settings=settings, hub=state, db=db,
        exchange=a, pool_reader=pool, beefy_reader=beefy,
    )

    # ITER 1: target = 0.0148, current = 0 → drift > min_notional → fire.
    await engine._maybe_rebalance_leg(
        symbol="ETH-USD", target=0.0148, current=0.0,
        min_notional=10.0, ref_price=2400.0,
    )
    assert a._signer.create_order.await_count == 1
    # After fire, expected was stamped:
    assert a._expected_short_size[0] == 0.0148

    # ITER 2: engine recomputes `current` via _safe_get_position →
    # _exchange.get_effective_position → max(observed=0, expected=0.0148)
    # = 0.0148. Drift = 0.0148 - 0.0148 = 0 → no fire.
    current = (await engine._safe_get_position("ETH-USD")).size
    assert current == 0.0148  # the fused value

    await engine._maybe_rebalance_leg(
        symbol="ETH-USD", target=0.0148, current=current,
        min_notional=10.0, ref_price=2400.0,
    )
    # Critical assertion: still only ONE create_order call.
    assert a._signer.create_order.await_count == 1, (
        f"Engine fired again during WS lag — over-hedge regression. "
        f"Got {a._signer.create_order.await_count} fires, expected 1."
    )
```

- [ ] **Step 2: Run, expect PASS**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_dual_leg.py::test_engine_does_not_double_fire_during_ws_lag -v`
Expected: PASS.

- [ ] **Step 3: Run the full test suite to make sure all green**

Run: `C:/Users/Wallace/Python313/python.exe -m pytest tests/ -q`
Expected: all PASS (~265 tests, depending on what existed before).

- [ ] **Step 4: Commit**

```bash
git add tests/test_engine_dual_leg.py
git commit -m "$(cat <<'EOF'
test(engine): integration regression for 2026-05-07 over-hedge

End-to-end test using a real LighterAdapter wired into the engine.
WS never delivers the post-fire account update; engine MUST not
fire a second time. A MagicMock-only test wouldn't catch a regression
where stamping is accidentally re-tied to verify_fill — this one does.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>
EOF
)"
```

---

## Self-Review

**Spec coverage:**
- § Problem / § Goal — covered by Task 10 (regression test) and the design as a whole.
- § Architecture (diagram) — Tasks 1, 2, 3, 4, 5, 6, 7 each cover one box/arrow.
- § State (table) — Task 1 (`_observed_short_size`, `_observed_position_meta`), Task 2 (`_expected_short_size`, `_last_fire_at`), Task 7 (`_reconcile_task`).
- § API — Task 4 (`get_effective_position`), Task 1 (`get_position` regression).
- § Reconciliation logic / stamping rule — Task 3.
- § Reconciliation logic / loop — Tasks 5, 6, 7.
- § Buy/sell convention — preserved untouched (no task needed; existing code already correct).
- § Engine integration — Tasks 8, 9.
- § Lifecycle integration — no changes needed (called out in spec).
- § Test strategy (7 tests) — Task 1 (existing test updated), Task 2 (init), Task 3 (stamping), Task 4 (effective), Task 5 (HTTP), Task 6 (3 reconcile cases — counts as 3 tests), Task 7 (loop), Task 10 (engine regression). Total: 8 covered (test 6 of the spec corresponds to Task 1's preserved `get_position`, covered there).
- § Migration steps 1–8 — distributed across Tasks 1–10.

**Placeholder scan:** No "TBD" / "TODO" / "implement later" / "similar to". All steps include actual code or actual commands. Several "find this block" instructions reference specific line numbers and surrounding context to anchor the engineer.

**Type consistency:**
- `_observed_short_size` / `_expected_short_size`: both `dict[int, float]` — consistent across Tasks 1, 2, 3, 4, 5, 6.
- `_last_fire_at`: `dict[int, float]` (monotonic) — Tasks 2, 3, 6.
- `Position` returned by `get_effective_position` matches the existing `exchanges.base.Position` shape — Task 4.
- `_fetch_short_size_via_http` returns `float | None` — Tasks 5, 6.

**Order of tasks:** Each task only depends on earlier tasks. Task 1 sets up renamed fields, Task 2 adds new fields, Task 3 writes them, Task 4 reads them, Task 5 + 6 + 7 build the reconciler, Task 8 + 9 wire engine, Task 10 verifies the whole chain end-to-end.
