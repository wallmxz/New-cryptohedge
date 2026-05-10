# Manual Deposit Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the operation panel's "IL natural" row with a user-controlled "Pool $" row computed against `pool_value_now − op.baseline_deposit_usd`. The user enters the deposit USD via a small ✏️ Editar button on the op card; the bot persists and displays.

**Architecture:** One nullable column on `operations` (`baseline_deposit_usd REAL`). One DB helper (`update_operation_baseline_deposit`). One REST endpoint (`POST /operations/<id>/baseline`). pnl.py uses the user-set value when present; otherwise falls back to the existing HODL formula (back-compat). `Operation` dataclass gains a matching field. UI adds a small inline "Baseline" row + edit modal in the operation card.

**Tech Stack:** Python 3.13, aiosqlite, starlette, Alpine.js, pytest-asyncio.

---

## File Structure

| File | Responsibility |
|---|---|
| `db.py` | Add `baseline_deposit_usd REAL` column to `operations` migration block. Add `update_operation_baseline_deposit(op_id, usd_value)` helper with `WHERE status = 'active'` guard. |
| `engine/operation.py` | Add `baseline_deposit_usd: float \| None = None` field on the `Operation` dataclass + read in `from_db_row`. |
| `engine/pnl.py` | Replace the `il_natural` calc with `pool_dollar` that prefers `op.baseline_deposit_usd` over the HODL fallback. Add `breakdown["pool_dollar"]` and `breakdown["baseline_deposit_usd"]`. Keep `breakdown["il_natural"]` as alias for back-compat. |
| `web/routes.py` | New `update_operation_baseline` handler. POST `/operations/<int:op_id>/baseline` body `{usd_value: float}`. Validates >0, calls `db.update_operation_baseline_deposit`, returns success+value. |
| `app.py` | Register the new route. |
| `web/templates/partials/operation.html` | Append "Baseline (depósito)" inline row below the breakdown table + edit modal. Rename "IL natural" label to "Pool $". |
| `web/static/app.js` | Add `editBaseline()` / `saveBaseline()` handlers. Modal state. Swap `b.il_natural` → `b.pool_dollar` in `get op()`. |
| `tests/test_db.py` | Extend with 2 tests for the new helper. |
| `tests/test_pnl_dual_leg.py` | Extend with 3 tests for the new override path + fallbacks. |
| `tests/test_web.py` | Extend with 3 tests for the new endpoint (success, negative rejection, missing-value rejection). |

---

### Task 1: DB migration + helper

**Files:**
- Modify: `db.py` (add column to migration block ~line 177, add helper ~line 499 after `add_to_operation_accumulator`)
- Test: `tests/test_db.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_db.py`:

```python
async def test_update_operation_baseline_deposit_persists_value(db):
    # Insert an active operation
    op_id = await db.insert_operation(
        started_at=1700000000.0,
        baseline_eth_price=2000.0,
        baseline_pool_value_usd=50.0,
        baseline_amount0=0.01,
        baseline_amount1=100.0,
        baseline_collateral=100.0,
    )
    await db.update_operation_status(op_id, "active")
    await db.update_operation_baseline_deposit(op_id, 50.03)
    op = await db.get_operation(op_id)
    assert op["baseline_deposit_usd"] == 50.03


async def test_update_operation_baseline_deposit_only_writes_when_active(db):
    # Closed op must not be writable
    op_id = await db.insert_operation(
        started_at=1700000000.0,
        baseline_eth_price=2000.0,
        baseline_pool_value_usd=50.0,
        baseline_amount0=0.01,
        baseline_amount1=100.0,
        baseline_collateral=100.0,
    )
    await db.close_operation(
        op_id, ended_at=1700001000.0, final_net_pnl=0.0, close_reason="test",
    )
    await db.update_operation_baseline_deposit(op_id, 99.99)
    op = await db.get_operation(op_id)
    # No-op write — column stays NULL on the closed op
    assert op["baseline_deposit_usd"] is None
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_db.py::test_update_operation_baseline_deposit_persists_value tests/test_db.py::test_update_operation_baseline_deposit_only_writes_when_active -v`
Expected: FAIL — column doesn't exist OR helper undefined OR `KeyError: 'baseline_deposit_usd'`.

- [ ] **Step 3: Add migration column**

In `db.py`, locate the cross-pair dual-leg migration block at ~line 176–189 (the `for col_def in (...)` block adding `baseline_token0_usd_price`, etc.). Add a new `try/except` block after that one for the new column:

```python
        # Manual deposit baseline (Pool $ feature, 2026-05-08)
        try:
            await self._conn.execute(
                "ALTER TABLE operations ADD COLUMN baseline_deposit_usd REAL"
            )
            await self._conn.commit()
        except aiosqlite.OperationalError:
            pass  # already added
```

- [ ] **Step 4: Add helper**

In `db.py`, after `add_to_operation_accumulator` at ~line 499, add:

```python
    async def update_operation_baseline_deposit(
        self, op_id: int, usd_value: float,
    ) -> None:
        """Persist the user-set baseline_deposit_usd on an ACTIVE operation.
        Used by POST /operations/<id>/baseline so the panel's Pool $ row
        reflects the user's actual cost basis (versus the HODL fallback
        for ops without it set).

        WHERE status='active' guard prevents accidental writes against
        closed ops during a teardown race or stale UI session.
        """
        await self._conn.execute(
            "UPDATE operations SET baseline_deposit_usd = ? "
            "WHERE id = ? AND status = 'active'",
            (usd_value, op_id),
        )
        await self._conn.commit()
```

- [ ] **Step 5: Run tests**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_db.py::test_update_operation_baseline_deposit_persists_value tests/test_db.py::test_update_operation_baseline_deposit_only_writes_when_active -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add db.py tests/test_db.py
git commit -m "feat(db): baseline_deposit_usd column + update helper

Per spec 2026-05-08-manual-deposit-baseline-design. New nullable
column on operations; helper writes only when status='active' so a
race during teardown or a stale UI session can't overwrite a closed
op's value.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: Operation dataclass field

**Files:**
- Modify: `engine/operation.py` (add field + from_db_row)
- Test: `tests/test_pnl_dual_leg.py` (covered transitively by Task 3 tests; this task makes them parseable)

- [ ] **Step 1: Add field on dataclass**

In `engine/operation.py`, locate the `Operation` dataclass. Add the new field at the end of the cross-pair dual-leg fields block (around `funding_paid_token1: float = 0.0`):

```python
    # Manual deposit baseline (2026-05-08): user-set USD cost basis.
    # When None, panel falls back to HODL formula (legacy behavior).
    baseline_deposit_usd: float | None = None
```

- [ ] **Step 2: Read in from_db_row**

In the same file, in the `from_db_row` classmethod, add the corresponding line near the other `row.get(...)` lookups:

```python
            baseline_deposit_usd=row.get("baseline_deposit_usd"),
```

(No `or 0.0` — preserve None vs 0.0 distinction so pnl.py can fall back correctly.)

- [ ] **Step 3: Run existing tests to make sure nothing broke**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_pnl_dual_leg.py tests/test_engine_funding.py tests/test_engine_dual_leg.py -q`
Expected: all pass (unchanged behavior — field defaults to None).

- [ ] **Step 4: Commit**

```bash
git add engine/operation.py
git commit -m "feat(operation): baseline_deposit_usd field on dataclass

Per spec 2026-05-08-manual-deposit-baseline-design. Field defaults to
None; from_db_row preserves None vs 0.0 distinction so pnl.py can pick
the right branch (override vs HODL fallback).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: `pnl.py` formula change

**Files:**
- Modify: `engine/pnl.py` (replace `il_natural` calc ~line 61–63, augment breakdown ~line 121–124)
- Test: `tests/test_pnl_dual_leg.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_pnl_dual_leg.py`:

```python
def test_compute_operation_pnl_uses_baseline_deposit_usd_when_set():
    """When op.baseline_deposit_usd > 0, pool_dollar = pool_now - baseline,
    overriding the HODL formula. il_natural alias mirrors the same value."""
    op = _op(baseline_deposit_usd=50.03)
    bd = compute_operation_pnl(
        op,
        current_pool_value_usd=51.58,
        current_token0_usd_price=1.75,
        current_token1_usd_price=4200.0,
        hedge_realized_per_symbol={},
        hedge_unrealized_per_symbol={},
    )
    assert bd["pool_dollar"] == 1.55  # 51.58 - 50.03
    assert bd["il_natural"] == 1.55  # alias = same
    assert bd["baseline_deposit_usd"] == 50.03


def test_compute_operation_pnl_falls_back_to_hodl_when_baseline_deposit_null():
    """Without baseline set, pool_dollar uses the HODL formula (legacy)."""
    op = _op(baseline_deposit_usd=None)
    bd = compute_operation_pnl(
        op,
        current_pool_value_usd=326.20,
        current_token0_usd_price=1.75,
        current_token1_usd_price=4200.0,
        hedge_realized_per_symbol={},
        hedge_unrealized_per_symbol={},
    )
    # HODL: 100 * 1.75 + 0.0375 * 4200 = 175.0 + 157.5 = 332.5
    # pool_dollar = 326.20 - 332.5 = -6.30
    assert bd["pool_dollar"] == -6.3
    assert bd["il_natural"] == -6.3
    assert bd["baseline_deposit_usd"] is None


def test_compute_operation_pnl_falls_back_to_hodl_when_baseline_deposit_zero():
    """Defensive: 0 or negative values fall back to HODL too."""
    op = _op(baseline_deposit_usd=0.0)
    bd = compute_operation_pnl(
        op,
        current_pool_value_usd=326.20,
        current_token0_usd_price=1.75,
        current_token1_usd_price=4200.0,
        hedge_realized_per_symbol={},
        hedge_unrealized_per_symbol={},
    )
    # Same HODL fallback as the null case: -6.30
    assert bd["pool_dollar"] == -6.3
```

The existing `_op(**overrides)` fixture in this file does `base.update(overrides)` so passing `baseline_deposit_usd=...` works once Task 2's dataclass field is present.

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_pnl_dual_leg.py::test_compute_operation_pnl_uses_baseline_deposit_usd_when_set tests/test_pnl_dual_leg.py::test_compute_operation_pnl_falls_back_to_hodl_when_baseline_deposit_null tests/test_pnl_dual_leg.py::test_compute_operation_pnl_falls_back_to_hodl_when_baseline_deposit_zero -v`
Expected: FAIL — `pool_dollar` and `baseline_deposit_usd` keys don't exist in the breakdown.

- [ ] **Step 3: Replace the formula in pnl.py**

In `engine/pnl.py`, locate the existing block at ~line 61–63:

```python
    # IL natural: LP USD value - HODL USD value at current prices.
    hodl_value = op.baseline_amount0 * p0_now + op.baseline_amount1 * p1_now
    il_natural = current_pool_value_usd - hodl_value
```

Replace with:

```python
    # Pool $ — primary metric. When the user has set a baseline_deposit_usd
    # (via POST /operations/<id>/baseline), use it as the cost basis; the
    # row in the panel reads "Pool $ = pool_now - what_user_invested".
    # Otherwise fall back to the HODL divergence formula (legacy IL natural)
    # so ops created before the user has clicked Editar still show
    # something sensible — the panel labels this state explicitly.
    if op.baseline_deposit_usd is not None and op.baseline_deposit_usd > 0:
        pool_dollar = current_pool_value_usd - op.baseline_deposit_usd
    else:
        hodl_value = op.baseline_amount0 * p0_now + op.baseline_amount1 * p1_now
        pool_dollar = current_pool_value_usd - hodl_value
```

In the breakdown dict at ~line 121–124, replace:

```python
        "il_natural": round(il_natural, 4),
```

with:

```python
        "pool_dollar": round(pool_dollar, 4),
        "baseline_deposit_usd": op.baseline_deposit_usd,
        # Alias for back-compat with any external consumer of the
        # breakdown (analytics scripts, older test fixtures).
        "il_natural": round(pool_dollar, 4),
```

- [ ] **Step 4: Run tests**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_pnl_dual_leg.py -v`
Expected: all PASS — existing tests still green (they use `il_natural` which now equals `pool_dollar`), plus the 3 new tests pass.

- [ ] **Step 5: Run full suite to confirm no regressions**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/ -q --no-header 2>&1 | tail -5`
Expected: all pass.

- [ ] **Step 6: Commit**

```bash
git add engine/pnl.py tests/test_pnl_dual_leg.py
git commit -m "feat(pnl): pool_dollar replaces il_natural with user-set baseline

When op.baseline_deposit_usd > 0, breakdown.pool_dollar =
current_pool_value_usd - baseline_deposit_usd (cost basis metric the
user actually wants). Otherwise falls back to the HODL divergence
formula so ops without the field still render. il_natural kept as an
alias for back-compat. baseline_deposit_usd surfaced in the breakdown
so the UI can render the line 'Baseline (depósito): \$X.XX'.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: REST endpoint

**Files:**
- Modify: `web/routes.py` (add `update_operation_baseline` handler)
- Modify: `app.py` (register the route)
- Test: `tests/test_web.py`

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_web.py`:

```python
def test_post_baseline_updates_db(app, monkeypatch):
    """Endpoint persists the value via db.update_operation_baseline_deposit."""
    from unittest.mock import AsyncMock, MagicMock
    fake_db = MagicMock()
    fake_db.update_operation_baseline_deposit = AsyncMock(return_value=None)
    app.state.db = fake_db
    client = TestClient(app)
    creds = base64.b64encode(b"admin:secret").decode()
    resp = client.post(
        "/operations/42/baseline",
        json={"usd_value": 50.03},
        headers={"Authorization": f"Basic {creds}"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["success"] is True
    assert body["baseline_deposit_usd"] == 50.03
    fake_db.update_operation_baseline_deposit.assert_awaited_once_with(42, 50.03)


def test_post_baseline_rejects_negative_value(app):
    client = TestClient(app)
    creds = base64.b64encode(b"admin:secret").decode()
    resp = client.post(
        "/operations/42/baseline",
        json={"usd_value": -10.0},
        headers={"Authorization": f"Basic {creds}"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
    assert "positive" in body["error"].lower() or "value" in body["error"].lower()


def test_post_baseline_rejects_missing_value(app):
    client = TestClient(app)
    creds = base64.b64encode(b"admin:secret").decode()
    resp = client.post(
        "/operations/42/baseline",
        json={},
        headers={"Authorization": f"Basic {creds}"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert body["success"] is False
```

- [ ] **Step 2: Run to verify failure**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_web.py::test_post_baseline_updates_db tests/test_web.py::test_post_baseline_rejects_negative_value tests/test_web.py::test_post_baseline_rejects_missing_value -v`
Expected: FAIL — route doesn't exist yet (404).

- [ ] **Step 3: Add the handler**

In `web/routes.py`, near `hedge_existing` at ~line 276, add:

```python
async def update_operation_baseline(request: Request):
    """POST /operations/<int:op_id>/baseline body {usd_value: float} →
    set the cost-basis baseline used by the panel's Pool $ row. Per spec
    2026-05-08-manual-deposit-baseline. Validates >0; persists via
    db.update_operation_baseline_deposit (which guards on status='active').
    """
    if not hasattr(request.app.state, "db"):
        return JSONResponse(
            {"success": False, "error": "DB not available"}, status_code=503,
        )
    db = request.app.state.db
    try:
        op_id = int(request.path_params["op_id"])
    except (KeyError, ValueError):
        return JSONResponse(
            {"success": False, "error": "invalid op_id"}, status_code=400,
        )
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            {"success": False, "error": "Body must be JSON {usd_value: float}"},
            status_code=400,
        )
    raw_value = body.get("usd_value")
    if raw_value is None:
        return JSONResponse(
            {"success": False, "error": "missing usd_value"},
            status_code=400,
        )
    try:
        usd_value = float(raw_value)
    except (TypeError, ValueError):
        return JSONResponse(
            {"success": False, "error": "usd_value must be a number"},
            status_code=400,
        )
    if usd_value <= 0:
        return JSONResponse(
            {"success": False, "error": "usd_value must be positive"},
            status_code=400,
        )
    try:
        await db.update_operation_baseline_deposit(op_id, usd_value)
    except Exception as e:
        return JSONResponse(
            {"success": False, "error": str(e)}, status_code=500,
        )
    return JSONResponse(
        {"success": True, "baseline_deposit_usd": usd_value},
        status_code=200,
    )
```

- [ ] **Step 4: Register the route**

In `app.py`, locate the `routes = [...]` list at ~line 174. Find the `Route("/operations/hedge-existing", ...)` line and add the import + route immediately after it.

In the imports at the top of `app.py` (~line 18):

```python
from web.routes import (
    ...,
    hedge_existing,
    update_operation_baseline,  # NEW
    ...,
)
```

In the routes list:

```python
        Route("/operations/hedge-existing", hedge_existing, methods=["POST"]),
        Route(
            "/operations/{op_id:int}/baseline",
            update_operation_baseline, methods=["POST"],
        ),
```

- [ ] **Step 5: Run tests**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_web.py -v`
Expected: all PASS (existing + 3 new).

- [ ] **Step 6: Commit**

```bash
git add web/routes.py app.py tests/test_web.py
git commit -m "feat(routes): POST /operations/{op_id}/baseline endpoint

Validates body {usd_value: float}, requires positive, calls
db.update_operation_baseline_deposit (which is itself guarded by
status='active'). Wired in app.py routes list. Returns
{success, baseline_deposit_usd} on 200, {success: false, error} on
4xx/5xx.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: UI — operation card row + edit modal

**Files:**
- Modify: `web/templates/partials/operation.html` (add inline baseline row + modal at end of card; rename label "IL natural" → "Pool $")
- Modify: `web/static/app.js` (add `editBaseline` / `saveBaseline` / state slots; swap `il_natural` → `pool_dollar` reference)
- Test: smoke via running uvicorn (no automated UI tests in the codebase yet)

- [ ] **Step 1: Add Alpine state slots and handlers in `web/static/app.js`**

In `app.js`, locate the data-init block (around `operation_pnl_breakdown: {},` at ~line 22). Add after that line:

```javascript
            baselineModal: false,
            baselineInput: "",
```

Locate `get op()` at ~line 122 and within the `breakdown` array, replace this entry:

```javascript
                { label: "IL natural", value: b.il_natural || 0 },
```

with:

```javascript
                { label: "Pool $", value: b.pool_dollar || 0 },
```

Also update the `pool` aggregate getter at ~line 116:

```javascript
            const pool = (b.lp_fees_earned || 0) + (b.beefy_perf_fee || 0) + (b.pool_dollar || 0);
```

(swap `b.il_natural` for `b.pool_dollar` — semantically the same since pnl.py aliases them, but cleaner to read.)

Add the two handlers somewhere in the methods block (e.g., after `loadHistory()` or near the bottom of the data-init's methods):

```javascript
        editBaseline() {
            const cur = this.state.operation_pnl_breakdown?.baseline_deposit_usd;
            this.baselineInput = (cur ?? "").toString();
            this.baselineModal = true;
        },

        async saveBaseline() {
            const op = this.state.current_operation;
            if (!op || !op.id) return;
            const value = parseFloat(this.baselineInput);
            if (!(value > 0)) return;
            try {
                const resp = await fetch(`/operations/${op.id}/baseline`, {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ usd_value: value }),
                });
                const data = await resp.json();
                if (data.success) {
                    this.baselineModal = false;
                    this.baselineInput = "";
                } else {
                    alert(`Erro ao salvar baseline: ${data.error || resp.status}`);
                }
            } catch (e) {
                alert(`Erro ao salvar baseline: ${e}`);
            }
        },
```

The handler reads the active op id from `this.state.current_operation.id` (already populated by the SSE state stream).

- [ ] **Step 2: Add template UI in `web/templates/partials/operation.html`**

Locate the operation card body (after the breakdown table, before the Encerrar button). Append:

```html
<div class="mt-3 flex items-center gap-2 text-xs text-slate-500">
  <span x-show="state.operation_pnl_breakdown?.baseline_deposit_usd">
    Baseline (depósito):
    <span class="font-mono"
          x-text="'$' + (state.operation_pnl_breakdown.baseline_deposit_usd || 0).toFixed(2)"></span>
  </span>
  <span x-show="!state.operation_pnl_breakdown?.baseline_deposit_usd"
        class="italic">
    Baseline: snapshot de import (HODL fallback)
  </span>
  <button @click="editBaseline()"
          class="ml-auto text-xs px-2 py-1 rounded bg-slate-100 hover:bg-slate-200">
    ✏️ Editar
  </button>
</div>

<!-- Modal (rendered alongside the card) -->
<div x-show="baselineModal"
     x-cloak
     class="fixed inset-0 z-50 flex items-center justify-center bg-black/30">
  <div class="bg-white rounded-lg shadow-lg p-4 w-80">
    <h3 class="font-semibold mb-2">Atualizar baseline (depósito USD)</h3>
    <p class="text-xs text-slate-500 mb-2">
      Atual:
      $<span x-text="(state.operation_pnl_breakdown?.baseline_deposit_usd ?? 0).toFixed(2)"></span>.
      Insira o total que você investiu nesta operação (após qualquer
      depósito ou retirada).
    </p>
    <input type="number" step="0.01" min="0.01"
           class="w-full border rounded px-2 py-1 text-sm"
           x-model="baselineInput"
           placeholder="50.03" />
    <div class="flex justify-end gap-2 mt-3">
      <button @click="baselineModal = false"
              class="text-xs px-3 py-1 rounded bg-slate-100">
        Cancelar
      </button>
      <button @click="saveBaseline()"
              :disabled="!baselineInput || parseFloat(baselineInput) <= 0"
              class="text-xs px-3 py-1 rounded bg-emerald-600 text-white disabled:opacity-50">
        Aplicar
      </button>
    </div>
  </div>
</div>
```

- [ ] **Step 3: Smoke test in browser**

Run the existing test suite first to make sure JS/template changes didn't break Python tests:

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_web.py -v`
Expected: all pass (template-syntax errors would surface here when the dashboard endpoint renders).

Then load the dashboard locally (no need to restart uvicorn — controller does that as a separate verification step). Just confirm the partial template parses with no Jinja error.

Run: `"C:/Users/Wallace/Python313/python.exe" -c "from jinja2 import Environment, FileSystemLoader; e = Environment(loader=FileSystemLoader('web/templates')); t = e.get_template('partials/operation.html'); print('parsed OK, length=', len(t.render()))"`
Expected: prints "parsed OK, length=N" with N > 1000.

(If the template has Jinja-side variables that don't resolve, render with a stub context — the script above passes empty context which is fine for purely Alpine-side templates. Existing partials work this way.)

- [ ] **Step 4: Commit**

```bash
git add web/templates/partials/operation.html web/static/app.js
git commit -m "feat(ui): Pool \$ row + Baseline editor on operation card

Inline 'Baseline (depósito): \$X.XX [✏️ Editar]' row below the
breakdown table. Click opens a modal with a numeric input + Aplicar
button that POSTs /operations/<id>/baseline. Italic 'HODL fallback'
note when baseline isn't set yet (matches the engine's fallback
formula). Renames 'IL natural' label to 'Pool \$'; the pool aggregate
in get pnl() now reads b.pool_dollar (semantically identical via the
pnl.py alias, just clearer to read).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Final verification + push

**Files:**
- None modified (verification + push only)

- [ ] **Step 1: Run full test suite**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/ -q --no-header 2>&1 | tail -5`
Expected: all pass (~305 = previous 297 + 8 new from this plan).

- [ ] **Step 2: Push the branch**

```bash
git push
```

- [ ] **Step 3: Manual smoke verification (controller's responsibility — describe in handoff)**

After uvicorn restart (controller restarts when ready), the user:
1. Loads the dashboard.
2. Sees the operation card now showing "Baseline: snapshot de import (HODL fallback)" italic + ✏️ Editar button.
3. Clicks ✏️, types `50.03`, clicks Aplicar.
4. Modal closes; within 1 iter the panel shows "Baseline: $50.03" (non-italic) and the "Pool $" row reads `current_pool_value_usd − 50.03`.
5. Cross-check against Beefy's PNL value — should match within a few cents.

- [ ] **Step 4: Commit any final docs touch-up if needed**

If the smoke verification reveals a bug, fix in a follow-up commit. Otherwise no commit needed.

---

## Self-Review

**Spec coverage:**
- §Architecture (DB col, endpoint, pnl override, UI button) → Tasks 1, 4, 3, 5.
- §Components > db.py helper → Task 1. ✓
- §Components > REST endpoint → Task 4. ✓
- §Components > UI changes → Task 5. ✓
- §Components > pnl.py formula → Task 3. ✓
- §Components > Operation dataclass → Task 2. ✓
- §Workflow examples (op #28 case, $500 add, withdraw) → covered by the manual edit flow in Task 5.
- §Risks 1–4 → mitigations in code (status='active' guard, fallback when None, italic HODL note when null).
- §Testing 1–8 → 3 in test_pnl_dual_leg.py (Task 3) + 3 in test_web.py (Task 4) + 2 in test_db.py (Task 1) = 8 tests total. ✓

**Placeholder scan:** every step has concrete code or commands. No TBDs, no "add error handling" hand-waves. Test code is complete in each test step.

**Type consistency:**
- `baseline_deposit_usd: float | None` consistent across `Operation` dataclass (Task 2), `from_db_row` (Task 2), `pnl.py` `op.baseline_deposit_usd` checks (Task 3), and the breakdown dict surfaces it (Task 3). ✓
- DB column name `baseline_deposit_usd` matches the dataclass field, the pnl read, the helper signature, the endpoint, and the JS state path (`state.operation_pnl_breakdown.baseline_deposit_usd`). ✓
- Endpoint path `/operations/<int:op_id>/baseline` is the same in routes.py registration (Task 4), tests (Task 4), and JS handler (Task 5). ✓
- Breakdown key `pool_dollar` consistent in pnl.py (Task 3), JS `b.pool_dollar` (Task 5). ✓

Plan ready for execution.
