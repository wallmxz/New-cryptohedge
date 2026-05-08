# On-chain Deposit Baseline — Design Spec

**Date:** 2026-05-08
**Status:** Approved through brainstorming (§1-§5)
**Branch:** `feature/cross-pair-dual-hedge`

## Problem

The operation panel's "IL natural" row shows pool divergence vs HODL using the bot's snapshot baseline (captured when `hedge-existing` import ran or when the bot bootstrapped). For op #28 it currently displays ~−$0.64. Beefy's UI displays a different number (+$1.55) because it uses the **actual deposit USD value** as the baseline, not a later snapshot.

The user wants the panel to match Beefy's metric: **`pool_value_now − cumulative_deposit_usd`** = "how much money did I make/lose since I put dollars in." They also want the baseline read **on-chain** (not via Beefy's API which has cache delay and discrepancy).

## Goal

Replace the panel's "IL natural" row with "Pool $", computed against the cumulative USD value of all deposits the wallet made into the earn vault, detected from on-chain events.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│  TRIGGER                                                     │
│  ├─ Auto: hedge-existing flow runs detect_baseline_onchain  │
│  └─ Manual: POST /operations/<id>/remark-baseline            │
│       └─ button "Remarcar baseline on-chain" no card        │
└────────────┬────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────┐
│  chains/onchain_baseline.py (NEW)                            │
│                                                              │
│  detect_baseline_deposit_usd(w3, wallet, vault, lookback) →  │
│    1. eth_getLogs Transfer(0x0, wallet) on Beefy earn vault  │
│       (paginated 50k blocks, default 7 days back)            │
│    2. For each mint event:                                   │
│       a. Get tx hash, fetch receipt logs                     │
│       b. Find ERC20 Transfers FROM wallet → {anywhere}       │
│          (captures direct deposits AND zap path)             │
│       c. Sum amounts × historical USD prices via Chainlink   │
│    3. Return cumulative USD total                            │
└────────────┬────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────┐
│  chains/chainlink.py (NEW)                                   │
│                                                              │
│  get_price_at_block(token, block_number) → float             │
│    └─ Reads AggregatorV3 AnswerUpdated events around block,  │
│       returns latest price ≤ block_number. Pure log query,   │
│       no archive node needed.                                │
└─────────────────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────┐
│  DB: 3 new columns on operations                             │
│  • baseline_deposit_usd       REAL                           │
│  • baseline_deposit_block     INTEGER                        │
│  • baseline_deposit_detected_at REAL                         │
└────────────┬────────────────────────────────────────────────┘
             │
             ▼
┌─────────────────────────────────────────────────────────────┐
│  engine/pnl.py — replace il_natural calc                     │
│                                                              │
│  if op.baseline_deposit_usd > 0:                             │
│      pool_dollar = current_pool_value_usd - baseline_deposit │
│  else:                                                       │
│      pool_dollar = HODL fallback (current formula)           │
│                                                              │
│  breakdown["pool_dollar"] = pool_dollar                       │
│  Panel renders as "Pool $" (replaces "IL natural")           │
└─────────────────────────────────────────────────────────────┘
```

## Components

### `chains/onchain_baseline.py` — detection algorithm

```python
async def detect_baseline_deposit_usd(
    w3: AsyncWeb3, wallet: str, vault: str,
    *, max_blocks: int = 2_000_000, chunk: int = 50_000,
) -> dict:
    """Returns {baseline_deposit_usd, baseline_deposit_block, mints[]}.

    Phase 1 — find every Transfer(0x0 → wallet) on the earn vault since
    `current - max_blocks`. These are the wallet's mint events (one per
    deposit).

    Phase 2 — for each mint, fetch the tx receipt and find Transfers
    FROM the wallet to ANY address EXCEPT the WETH contract (those
    are ETH→WETH wrap operations, not deposits — see Risk #5). These
    are what the wallet actually spent, regardless of direct vs zap
    path. Sum amounts × historical Chainlink prices at the mint block.

    Phase 3 — sum across mints; that's the cumulative cost basis.

    On RPC failure: bubble up; caller logs and skips persisting.
    On Chainlink failure for one outflow: log warning, skip THAT
    outflow (other outflows in the same mint still count).
    """
```

Pagination keeps `eth_getLogs` block ranges within the 50k limit common to public RPCs. The default `max_blocks=2_000_000` covers ~7 days on Arbitrum at ~0.25s/block — adequate for hedge-existing imports done within the past week. The button can re-run with a wider window if the user has older deposits (parameter exposed via the REST endpoint).

### `chains/chainlink.py` — historical price source

```python
CHAINLINK_AGGREGATORS = {
    # token contract address (lowercase) → (aggregator, decimals)
    "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": (  # WETH
        "0x639Fe6ab55C921f74e7fac1ee960C0B6293ba612",  # ETH/USD
        8,
    ),
    "0x912ce59144191c1204e64559fe8253a0e49e6548": (  # ARB
        "0xb2A824043730FE05F3DA2efaFa1CBbe83fa548D6",  # ARB/USD
        8,
    ),
}

STABLECOINS = {  # treated as $1, no oracle query
    "0xaf88d065e77c8cc2239327c5edb3a432268e5831",  # USDC native
    "0xff970a61a04b1ca14834a43f5de4533ebddb5cc8",  # USDC.e bridged
    "0xfd086bc7cd5c481dcc9c85ebe478a1c0b69fcbb9",  # USDT
    "0xda10009cbd5d07dd0cecc66161fc93d7c9000da1",  # DAI
}


async def get_price_at_block(w3, token: str, block_number: int) -> float:
    """Returns the USD price of `token` at `block_number` using
    Chainlink AggregatorV3 AnswerUpdated events.

    Stablecoins shortcut to 1.0 (no RPC).
    Unlisted tokens raise ValueError.
    Window expands progressively (1k → 4k → 16k → 64k → 200k) until
    an event is found or 200k blocks (~14 h) is exhausted.
    """
```

Algorithm: query `eth_getLogs` for `AnswerUpdated(int256,uint256,uint256)` on the aggregator within an expanding window ending at `block_number`. The latest event with `blockNumber ≤ block_number` is the price valid at that block (Chainlink prices remain authoritative until the next update).

Topic 1 of `AnswerUpdated` is the price encoded as int256 (signed). Decode with `int.from_bytes(topic, "big", signed=True)`, divide by `10**decimals`.

### DB schema additions (`db.py`)

Three nullable columns on `operations`:

```sql
ALTER TABLE operations ADD COLUMN baseline_deposit_usd REAL;
ALTER TABLE operations ADD COLUMN baseline_deposit_block INTEGER;
ALTER TABLE operations ADD COLUMN baseline_deposit_detected_at REAL;
```

Existing operations stay NULL → engine falls back to the current HODL formula. Once `detect_baseline_deposit_usd` runs (auto on import or manual via button), the three columns get populated and the panel switches to the on-chain metric.

### REST endpoint (`web/routes.py`)

```python
POST /operations/<int:op_id>/remark-baseline
  body: {} (no params; uses op's vault address from DB)
  response: {
    success: true,
    baseline_deposit_usd: float,
    baseline_deposit_block: int,
    mints: [{block, tx_hash, usd_value, timestamp}, ...],
  }
  errors: {success: false, error: str}
```

Sync handler — detection takes 5–30 s depending on lookback. UI shows a spinner. Persists the three columns via `db.update_operation_baseline_deposit(op_id, usd, block, timestamp)`.

### UI changes (`web/templates/partials/operation.html` + `web/static/app.js`)

Append to the operation card body, below the PnL rows:

```html
<div class="mt-3 flex items-center gap-2 text-xs text-slate-500">
  <span x-show="state.operation_breakdown?.baseline_deposit_usd">
    Baseline (depósito on-chain):
    <span class="font-mono"
          x-text="'$' + state.operation_breakdown.baseline_deposit_usd.toFixed(2)"></span>
  </span>
  <span x-show="!state.operation_breakdown?.baseline_deposit_usd" class="italic">
    Baseline: snapshot de import (HODL fallback)
  </span>
  <button @click="remarkBaseline()" :disabled="remarkLoading"
          class="ml-auto text-xs px-2 py-1 rounded bg-slate-100 hover:bg-slate-200">
    <span x-show="!remarkLoading">Remarcar on-chain</span>
    <span x-show="remarkLoading">Detectando...</span>
  </button>
</div>
```

`remarkBaseline()` calls the REST endpoint, displays a toast with the result, and refreshes state.

The "IL natural" label in the breakdown table is renamed to **"Pool $"**; the array entry in `app.js:get op()` swaps `b.il_natural` for `b.pool_dollar`.

### `engine/pnl.py` — formula change

```python
# Replaces existing il_natural block
if op.baseline_deposit_usd is not None and op.baseline_deposit_usd > 0:
    pool_dollar = current_pool_value_usd - op.baseline_deposit_usd
else:
    # Fallback for ops created before detection ran
    hodl_value = op.baseline_amount0 * p0_now + op.baseline_amount1 * p1_now
    pool_dollar = current_pool_value_usd - hodl_value

breakdown["pool_dollar"] = round(pool_dollar, 4)
breakdown["baseline_deposit_usd"] = op.baseline_deposit_usd  # for UI
# Keep il_natural as alias for back-compat with any external consumer
breakdown["il_natural"] = breakdown["pool_dollar"]
```

### `engine/lifecycle.py` — auto-trigger on hedge-existing

After `open_shorts_for_existing_position` creates the operation row, schedule detection in the background:

```python
asyncio.create_task(self._detect_and_persist_baseline(op_id, vault_address))
```

`_detect_and_persist_baseline` calls `detect_baseline_deposit_usd`, persists via `db.update_operation_baseline_deposit`. On exception, logs warning (operation still works with HODL fallback).

## Sign convention

`pool_dollar > 0` → user is up vs cumulative deposit. UI renders with `+$X.XX` prefix.
`pool_dollar < 0` → user is down. UI renders `-$X.XX`.

The `Net PnL` aggregate stays the same formula (sum of breakdown components, excluding per-leg fields). With `il_natural` aliased to `pool_dollar`, no double-counting.

## Out of scope

- LP fees attribution / Beefy harvest event tracking (separate spec, deferred)
- Withdraw + redeposit reset semantics (cost basis accumulates indefinitely; a future "Reset baseline" button can zero the columns and re-detect post-withdraw)
- Migration to populate `baseline_deposit_usd` for closed (historical) operations
- Mainnet support (CHAINLINK_AGGREGATORS only includes Arbitrum addresses)
- Persistent cache of historical Chainlink prices (add later if performance becomes a concern)

## Risks

1. **`eth_getLogs` block-range limits.** Public RPCs cap at 10k–50k blocks per call. Mitigation: chunk size 50k by default, configurable via env var.
2. **Token without Chainlink aggregator.** ValueError raised; user sees a warning and skips that outflow. Adding new tokens means extending `CHAINLINK_AGGREGATORS`.
3. **Detection latency 5–30 s.** UI spinner + sync handler. If a real op exceeds 60 s on a slow RPC, upgrade to a background task with status polling.
4. **Withdraw + redeposit accumulates.** Documented as v1 limitation. Acceptable for the user's current single-deposit case (op #28).
5. **Outflow false positive: WETH wrap inside deposit tx.** Detection sums Transfers FROM wallet of WETH. If user wrapped ETH in the same tx and deposited the WETH, they'd be counted twice (the ETH→WETH wrap shows as a Transfer too). Mitigation: skip Transfers where `to == WETH contract` (those are wrap operations, not deposits).
6. **Race between detection and teardown.** User clicks "Encerrar operação" while detection is running. Persist with `WHERE op_id = X AND status != 'closed'` — write is no-op if op closed mid-flight.

## Testing

`tests/test_chainlink.py` (new):
1. `test_get_price_at_block_returns_eth_price_from_aggregator`
2. `test_get_price_at_block_returns_one_for_stablecoin`
3. `test_get_price_at_block_widens_window_when_no_events`
4. `test_get_price_at_block_raises_on_unlisted_token`
5. `test_get_price_at_block_raises_after_max_window`

`tests/test_onchain_baseline.py` (new):
6. `test_detect_baseline_finds_single_mint`
7. `test_detect_baseline_sums_multiple_mints`
8. `test_detect_baseline_handles_zap_path`
9. `test_detect_baseline_paginates_chunks`
10. `test_detect_baseline_skips_mint_when_chainlink_fails`

`tests/test_pnl_dual_leg.py` (extend):
11. `test_compute_operation_pnl_uses_baseline_deposit_usd_when_set`
12. `test_compute_operation_pnl_falls_back_to_hodl_when_baseline_deposit_null`

`tests/test_routes.py` (extend):
13. `test_remark_baseline_endpoint_updates_db_columns`

## Verification (post-deploy)

After uvicorn restart with the fix:
1. Click "Remarcar on-chain" on op #28's card.
2. Wait ~10 s. Toast confirms baseline detected (~$50 expected for op #28).
3. Panel's "Pool $" row should show approximately `pool_now − $50`. For current pool ≈ $51.58, result ≈ +$1.58 (Beefy shows +$1.55; difference is from Chainlink price snapshots vs Beefy's possibly different price source).
4. If `pool_dollar` doesn't match Beefy's PNL within a few cents, debug via the REST response's `mints` array (shows per-mint USD breakdown).
