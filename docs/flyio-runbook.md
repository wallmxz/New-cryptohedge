# Fly.io Deploy Runbook

Operational steps for migrating AutoMoney to Fly.io. Code changes already
landed via PR (Tasks 1-6 of the implementation plan). This runbook is
USER-EXECUTED — there is no test suite for the deploy itself; verification
is via smoke comparison.

## Pre-flight (do once)

1. Install Fly CLI: https://fly.io/docs/flyctl/install/
2. `fly auth login` (one-time per machine)
3. From project root: `fly apps create automoney --org personal` (skip if app already exists)

## Cutover procedure

### Step 1 — Pre-cutover snapshot (~2 min)

While bot is still running locally on :8000:

```bash
bash scripts/flyio_smoke_snapshot.sh
```

Verify `tmp_out/snapshot_local.json` exists and contains `operation_state: "active"`,
`current_operation_id: 28`, hedge_positions, pool_value_usd, etc.

### Step 2 — Volume + secrets (~3 min)

```bash
fly volumes create automoney_data --size 1 --region iad
bash scripts/flyio_secrets.sh
```

Verify:
```bash
fly volumes list   # should show automoney_data, 1GB, iad
fly secrets list   # should show ~17 secrets (not their values)
```

### Step 3 — First deploy (idle bot) (~5 min)

```bash
fly deploy
```

Watch for build success + machine start. Tail logs:
```bash
fly logs --no-tail | tail -30
```

Expected: `Lighter CheckClient: OK`, `LighterAdapter connected`, `HedgeModel ...`.
Bot will be in `operation_state: NONE` because the volume DB is empty.

DO NOT click anything in the Fly dashboard yet — bot has no op loaded.

### Step 4 — DB migration (~5 min, this is the unhedged window) ⚠

```bash
# Stop local bot
./stop.bat
# (Confirm uvicorn process is gone via Task Manager or 'tasklist | findstr uvicorn')

# Migrate DB
bash scripts/flyio_migrate_db.sh
# Type 'yes' to confirm.

# Restart Fly machine to reload DB
fly machine list                         # note the machine ID
fly machine restart <machine_id>

# Wait for restart + watch
fly logs --no-tail | tail -20
# Look for: "Restored active operation 28" and "HedgeModel ... active"
```

### Step 5 — Smoke compare (~3 min)

```bash
bash scripts/flyio_smoke_compare.sh
```

Inspect the diff. **Acceptable differences:**
- Numeric jitter (pool_value_usd ±$1 due to price tick movement during cutover)
- pnl_window_since_ts identical
- baseline_deposit_usd identical

**RED FLAGS — abort if any:**
- operation_state different (e.g. local "active", fly "none")
- hedge_positions sizes differ by >1%
- baseline_deposit_usd differs
- current_operation_id differs

### Step 6a — DEPLOY OK

If smoke compare clean and `fly logs --follow | grep -E "Rebalance fire|verify_diverging"`
shows healthy operation for 5+ min:

- Open browser: `https://admin:Wallace1@automoney.fly.dev/`
- Confirm card "Operação" shows op #28 ACTIVE
- **Do NOT restart local `start.bat`** (would create dual-bot competing on same wallet)
- Document deploy success in WORKING_ON.md

### Step 6b — ROLLBACK

If anything diverges in smoke compare or you see errors in logs:

```bash
# Stop Fly machine (does NOT delete data)
fly scale count 0

# Bring local back up (DB locally is intact)
./start.bat
```

Local bot resumes from where it stopped. No capital lost — just spent
~10 min unhedged in a $447 LP.

After rollback, investigate:
- `fly logs --no-tail | tail -100` for crash reasons
- Local snapshot vs DB to ensure local DB wasn't somehow corrupted (very unlikely)

## Post-deploy operational

- **Logs:** `fly logs --follow` (live tail) or `fly logs --no-tail | tail -N`
- **Restart:** `fly machine restart <id>`
- **SSH:** `fly ssh console` (drops into running container)
- **Volume backup:** automatic Fly snapshot daily, retained 5 days. List with `fly volumes snapshots list <vol_id>`
- **Cost:** ~$3-5/mo (256mb-512mb shared CPU + 1GB volume + small bandwidth)

## Updating after merging new code

```bash
git pull origin master
fly deploy
fly logs --no-tail | tail -20   # confirm restart success + op restored
```

That's it. Volume + secrets persist across deploys.
