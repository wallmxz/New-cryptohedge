# Manual Deposit Baseline (Pool $) — Design Spec

**Date:** 2026-05-08 (rewrite of earlier on-chain detection spec — simplified per user feedback)
**Status:** Approved through brainstorming
**Branch:** `feature/cross-pair-dual-hedge`

## Problem

The operation panel's "IL natural" row shows pool divergence vs HODL using the bot's snapshot baseline (captured when `hedge-existing` import ran or when the bot bootstrapped). For op #28 it currently displays ~−$0.64. Beefy's UI displays a different number (+$1.55) because it uses the **actual deposit USD value** as the baseline.

The user wants the panel to match Beefy's metric: **`pool_value_now − cumulative_deposit_usd`** = "how much money did I make/lose since I put dollars in." They want to control the baseline themselves (no Beefy API, no on-chain oracle dependency, no auto-detection edge cases) — they enter the USD they actually invested, and the panel reflects PnL against that exact number.

## Goal

Replace the panel's "IL natural" row with "Pool $", computed against a user-maintained `baseline_deposit_usd` field that they edit when they add or remove capital from the LP.

## Non-goals

- On-chain detection of deposits (rejected during brainstorming — too much code/oracle/risk for ambiguity it introduces with multi-deposit and withdraw cases).
- LP fees attribution / Beefy harvest event tracking (separate spec, deferred).
- Automatic baseline updates when the user moves money on Beefy (the user explicitly clicks edit + types the new number — manual is the source of truth).

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  USER INTERACTION                                            │
│                                                              │
│  Card "OPERAÇÃO" exibe:                                       │
│    Baseline (depósito): $50.03   [✏️ Editar]                  │
│                                                              │
│  Click [✏️] → modal:                                          │
│    "Total atual após mudança: $____"  (default = current)    │
│    [Aplicar]                                                  │
└────────────┬────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────┐
│  POST /operations/<id>/baseline                              │
│  body: {"usd_value": 550.03}                                 │
│                                                              │
│  → db.update_operation_baseline_deposit(op_id, 550.03)       │
│  → returns {success: true, baseline_deposit_usd: 550.03}     │
└────────────┬────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────┐
│  DB: 1 new column on operations                              │
│  • baseline_deposit_usd  REAL  (NULL when never set)         │
└────────────┬────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────┐
│  engine/pnl.py — replace il_natural calc                     │
│                                                              │
│  if op.baseline_deposit_usd is not None and > 0:             │
│      pool_dollar = current_pool_value_usd - baseline_deposit │
│  else:                                                       │
│      pool_dollar = HODL fallback (existing formula)          │
│                                                              │
│  breakdown["pool_dollar"] = pool_dollar                      │
│  breakdown["baseline_deposit_usd"] = op.baseline_deposit_usd │
│  Panel renders as "Pool $" (replaces "IL natural")           │
└─────────────────────────────────────────────────────────────┘
```

## Components

### DB schema (`db.py`)

One new nullable column on `operations`:

```sql
ALTER TABLE operations ADD COLUMN baseline_deposit_usd REAL;
```

NULL means the user hasn't set a baseline yet → engine uses the HODL fallback (current behavior, preserves back-compat with all existing ops).

### REST endpoint (`web/routes.py`)

```python
POST /operations/<int:op_id>/baseline
  body: {"usd_value": float}
  response: {"success": true, "baseline_deposit_usd": float}
  errors: {"success": false, "error": str}

  Validates:
    - op_id exists and is in ACTIVE state
    - usd_value > 0
  Persists via db.update_operation_baseline_deposit(op_id, usd_value).
```

Sync handler (one DB write, instant). No background task needed.

### `db.py` helper

```python
async def update_operation_baseline_deposit(
    self, op_id: int, usd_value: float,
) -> None:
    async with self._conn() as conn:
        await conn.execute(
            "UPDATE operations SET baseline_deposit_usd = ? "
            "WHERE id = ? AND status = 'active'",
            (usd_value, op_id),
        )
        await conn.commit()
```

The `status = 'active'` guard prevents accidental writes to closed ops (race during teardown).

### UI (`web/templates/partials/operation.html` + `web/static/app.js`)

**Card row (compact, below the breakdown table):**

```html
<div class="mt-3 flex items-center gap-2 text-xs text-slate-500">
  <span x-show="state.operation_breakdown?.baseline_deposit_usd">
    Baseline (depósito):
    <span class="font-mono"
          x-text="'$' + state.operation_breakdown.baseline_deposit_usd.toFixed(2)"></span>
  </span>
  <span x-show="!state.operation_breakdown?.baseline_deposit_usd"
        class="italic">
    Baseline: snapshot de import (HODL fallback)
  </span>
  <button @click="editBaseline()"
          class="ml-auto text-xs px-2 py-1 rounded bg-slate-100 hover:bg-slate-200">
    ✏️ Editar
  </button>
</div>
```

**Modal:**

```html
<div x-show="baselineModal" x-transition>
  <h3>Atualizar baseline (depósito USD)</h3>
  <p>Atual: $<span x-text="(state.operation_breakdown?.baseline_deposit_usd ?? 0).toFixed(2)"></span></p>
  <p class="text-xs text-slate-500 mb-2">
    Insira o total que você investiu nesta operação (após qualquer
    depósito ou retirada adicional).
  </p>
  <input type="number" step="0.01" min="0.01" x-model="baselineInput" />
  <button @click="saveBaseline()" :disabled="!baselineInput || baselineInput <= 0">
    Aplicar
  </button>
  <button @click="baselineModal = false">Cancelar</button>
</div>
```

**Alpine.js handlers:**

```javascript
editBaseline() {
  this.baselineInput = (this.state.operation_breakdown?.baseline_deposit_usd ?? "").toString();
  this.baselineModal = true;
},

async saveBaseline() {
  const op_id = this.state.operation?.id;
  const value = parseFloat(this.baselineInput);
  if (!op_id || !(value > 0)) return;
  const resp = await fetch(`/operations/${op_id}/baseline`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({usd_value: value}),
  });
  const data = await resp.json();
  if (data.success) {
    this.toast(`Baseline atualizado: $${data.baseline_deposit_usd.toFixed(2)}`);
    this.baselineModal = false;
  } else {
    this.toast(`Erro: ${data.error}`);
  }
},
```

The "IL natural" label in the breakdown table is renamed to **"Pool $"**; the corresponding entry in `app.js:get op()` swaps `b.il_natural` for `b.pool_dollar`.

### `engine/pnl.py` — formula change

Replace the existing IL natural block in `compute_operation_pnl`:

```python
# Pool $ — preferred metric vs HODL when user has set a baseline.
if op.baseline_deposit_usd is not None and op.baseline_deposit_usd > 0:
    # User-set cost basis: pool_now − cumulative_deposit_usd.
    pool_dollar = current_pool_value_usd - op.baseline_deposit_usd
else:
    # Fallback for ops where the user hasn't set baseline yet:
    # the original HODL divergence formula. Documented as such in the
    # UI ("Baseline: snapshot de import (HODL fallback)").
    hodl_value = op.baseline_amount0 * p0_now + op.baseline_amount1 * p1_now
    pool_dollar = current_pool_value_usd - hodl_value

breakdown["pool_dollar"] = round(pool_dollar, 4)
breakdown["baseline_deposit_usd"] = op.baseline_deposit_usd  # for UI display
# Keep il_natural as alias for back-compat with any external consumer
# of the breakdown (analytics, tests, etc).
breakdown["il_natural"] = breakdown["pool_dollar"]
```

The `Net PnL` aggregate sums the existing breakdown fields (excluding per-leg) and continues to work unchanged — `il_natural` is aliased to `pool_dollar`, no double-counting.

## Workflow examples

**1) Op started via hedge-existing, user deposited $50:**
- Op is ACTIVE with `baseline_deposit_usd = NULL`.
- Panel shows "Baseline: snapshot de import (HODL fallback)" + Pool $ computed via HODL formula.
- User clicks ✏️ Editar, types `50.03`, clicks Aplicar.
- DB: `baseline_deposit_usd = 50.03`.
- Next iter, panel shows "Baseline: $50.03" + Pool $ = `pool_now − 50.03`.

**2) User adds $500 more capital later:**
- User deposits $500 on Beefy directly. Engine reads new on-chain LP composition next iter; hedge targets grow proportionally; bot fires additional shorts. Already works (no code change).
- User clicks ✏️ Editar, sees "Atual: $50.03", types `550.03`, Aplicar.
- DB: `baseline_deposit_usd = 550.03`.
- Panel: Pool $ = `pool_now − 550.03`.

**3) User withdraws $100 partial:**
- User withdraws on Beefy. Engine reads smaller LP composition; reduces hedge accordingly.
- User clicks ✏️ Editar, types whatever cost basis they want to track (e.g., `450.03` if they consider the withdrawn $100 as recovered capital).
- The interpretation is the user's choice. Manual gives them control.

## Sign convention

- `pool_dollar > 0` → up since baseline. UI prefix `+$`.
- `pool_dollar < 0` → down since baseline. UI prefix `-$`.
- Net PnL aggregate continues to sum all breakdown components correctly.

## Risks

1. **User mistypes baseline.** Damage is bounded (panel shows wrong number until they edit again). No financial loss — bot's hedging is independent of this field. Mitigation: confirmation toast shows the saved value so user can sanity-check.

2. **Race during teardown.** User clicks Aplicar while op is being closed. SQL `WHERE status = 'active'` makes the write a no-op for closed ops. Safe.

3. **Forgotten baseline.** User starts op, never sets baseline. Panel falls back to HODL formula and shows the italic "(HODL fallback)" note so they know they haven't configured it. No silent failure.

4. **No back-fill for closed ops.** Spec doesn't migrate historical operations — the field stays NULL for them. They keep showing the HODL fallback in any historical view (acceptable; closed ops are read-only anyway).

## Testing

`tests/test_pnl_dual_leg.py` (extend):
1. `test_compute_operation_pnl_uses_baseline_deposit_usd_when_set`
   — `op.baseline_deposit_usd=50.03`, pool_value_usd=51.58 → pool_dollar=1.55, il_natural alias=1.55.
2. `test_compute_operation_pnl_falls_back_to_hodl_when_baseline_deposit_null`
   — `op.baseline_deposit_usd=None` → pool_dollar via HODL formula (existing behavior).
3. `test_compute_operation_pnl_falls_back_to_hodl_when_baseline_deposit_zero_or_negative`
   — defensive: `op.baseline_deposit_usd=0` falls back too.

`tests/test_routes.py` (extend):
4. `test_post_baseline_endpoint_updates_db`
   — POST /operations/X/baseline with `{usd_value: 550}`, verify db.update_operation_baseline_deposit called with (X, 550).
5. `test_post_baseline_endpoint_rejects_negative_value`
   — `{usd_value: -10}` → 400 with success=false.
6. `test_post_baseline_endpoint_rejects_missing_value`
   — empty body → 400.

`tests/test_db.py` (extend or new):
7. `test_update_operation_baseline_deposit_only_writes_when_active`
   — op in `closed` state, call helper → no row update (status guard).
8. `test_update_operation_baseline_deposit_persists_value`
   — active op, call helper → row reflects new value.

## Verification (post-deploy)

After uvicorn restart with the fix:
1. Panel for op #28 shows "Baseline: snapshot de import (HODL fallback)" italic note + Pool $ computed via HODL.
2. Click ✏️ Editar, type `50.03`, Aplicar.
3. Toast confirms. Within 1 iter, panel shows "Baseline: $50.03" and Pool $ ≈ `current_pool − 50.03`.
4. Compare against Beefy's PNL value — should match within a few cents (Beefy's price source vs current pool valuation).
