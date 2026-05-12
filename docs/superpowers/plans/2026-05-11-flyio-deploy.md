# Fly.io Deploy Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Deploy AutoMoney to Fly.io with persistent volume, secrets, dual health check, and a documented cutover runbook that migrates op #28 (capital real $447) sem perda de estado.

**Architecture:** Single Fly machine in `iad` region, Volume mount `/data` for `automoney.db`, secrets via `fly secrets set` (all 21 .env keys), basic auth retained, new `/health/engine` endpoint as loop watchdog. Migration via `fly ssh sftp` of the local DB. Local DB stays intact as rollback path.

**Tech Stack:** Fly.io (Volumes, Secrets, Machines), Python 3.13, Docker, bash scripts.

**Spec:** `docs/superpowers/specs/2026-05-11-flyio-deploy-design.md`

---

## File Structure

**Add:**
- `scripts/flyio_secrets.sh` — push .env entries to Fly secrets
- `scripts/flyio_migrate_db.sh` — sftp local automoney.db to /data
- `scripts/flyio_smoke_snapshot.sh` — capture local SSE state for diff
- `scripts/flyio_smoke_compare.sh` — fetch Fly state and diff vs snapshot
- `tests/test_health_engine.py` — 3 unit tests for the new endpoint
- `docs/flyio-runbook.md` — cutover runbook (operational doc)

**Modify:**
- `app.py` — add `health_engine` handler + register `/health/engine` route + add to BasicAuthMiddleware exclude list
- `fly.toml` — add `[mounts]`, bump VM to 512mb, add second `[checks]` block, add DB_PATH/LOG_FORMAT to env
- `Dockerfile` — bump base to `python:3.13-slim` (match dev)
- `.gitignore` — exclude `tmp_out/snapshot_*.txt`

**No deletes.**

**User-only steps (manual, NOT subagent):** `fly auth login`, `fly volumes create`, `fly secrets set` (script wraps), `fly deploy`, `stop.bat`, sftp put, `fly machine restart`, browser smoke check.

---

## Task 1: `/health/engine` endpoint + 3 tests

**Files:**
- Modify: `app.py:175-225` (routes + middleware exclude)
- Create: `tests/test_health_engine.py`

- [ ] **Step 1: Write 3 failing tests**

Create `tests/test_health_engine.py`:

```python
"""Tests for /health/engine endpoint — Fly's loop watchdog."""
from __future__ import annotations

import time
import pytest
from starlette.testclient import TestClient
from app import create_app


def _build_client():
    """Build a test client with a hub instance whose last_update we can mutate."""
    app = create_app(start_engine=False)
    return TestClient(app), app


def test_health_engine_returns_200_when_loop_recent():
    """hub.last_update within last 30s → 200 + alive=true."""
    client, app = _build_client()
    app.state.hub.last_update = time.time()
    r = client.get("/health/engine")
    assert r.status_code == 200
    body = r.json()
    assert body["alive"] is True
    assert body["iter_age_s"] < 1.0


def test_health_engine_returns_503_when_loop_stale():
    """hub.last_update older than 30s → 503 + alive=false."""
    client, app = _build_client()
    app.state.hub.last_update = time.time() - 60
    r = client.get("/health/engine")
    assert r.status_code == 503
    body = r.json()
    assert body["alive"] is False
    assert body["iter_age_s"] > 30


def test_health_engine_works_without_auth():
    """Endpoint is in BasicAuthMiddleware exclude list (Fly probes don't auth)."""
    client, app = _build_client()
    app.state.hub.last_update = time.time()
    # No Authorization header
    r = client.get("/health/engine")
    assert r.status_code == 200
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_health_engine.py -v 2>&1 | tail -15`
Expected: 3 fail with 404 or auth error (route not registered yet)

- [ ] **Step 3: Add the handler in app.py**

In `app.py`, before `def create_app(...)`, add the handler:

```python
async def health_engine(request: Request) -> JSONResponse:
    """Loop watchdog for Fly's secondary health check.

    Returns 200 if the engine iter ran within the last 30s, 503 otherwise.
    Fly considers the machine unhealthy if 503, and will restart per its
    autoheal policy. Excluded from basic auth so Fly probes work.
    """
    hub = request.app.state.hub
    last_update = getattr(hub, "last_update", 0) or 0
    age = time.time() - last_update
    if age < 30:
        return JSONResponse({"alive": True, "iter_age_s": round(age, 1)})
    return JSONResponse(
        {"alive": False, "iter_age_s": round(age, 1)},
        status_code=503,
    )
```

(Imports `time`, `Request`, `JSONResponse` should already exist in app.py — verify with `grep -n "import" app.py | head -20` and add if missing.)

- [ ] **Step 4: Register the route + add to auth exclude**

In the routes list (around line 176, right after `Route("/health", ...)`), add:

```python
        Route("/health/engine", health_engine),
```

In the BasicAuthMiddleware exclude list (around line 223):

Change from:
```python
        exclude=["/health", "/metrics"],
```

To:
```python
        exclude=["/health", "/health/engine", "/metrics"],
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_health_engine.py -v 2>&1 | tail -10`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add app.py tests/test_health_engine.py
git commit -m "feat(app): /health/engine endpoint — Fly loop watchdog

Returns 200 if hub.last_update is < 30s old, 503 otherwise. Fly's
secondary check uses this to detect a hung engine loop (e.g., RPC
stall, asyncio deadlock) even when the HTTP server is responsive.

Excluded from BasicAuthMiddleware so Fly internal probes work
without credentials. Existing /health remains a basic liveness ping.

Spec: docs/superpowers/specs/2026-05-11-flyio-deploy-design.md
"
```

---

## Task 2: fly.toml — volume mount + 512mb + second health check + env

**Files:**
- Modify: `fly.toml`

- [ ] **Step 1: Read current fly.toml**

Run: `cat fly.toml`
Expected: shows existing app config (256mb, single health check, no mounts)

- [ ] **Step 2: Replace fly.toml with extended version**

Overwrite `fly.toml` with:

```toml
app = "automoney"
primary_region = "iad"

[build]
  dockerfile = "Dockerfile"

[env]
  PYTHONUNBUFFERED = "true"
  DB_PATH = "/data/automoney.db"
  LOG_FORMAT = "json"
  START_ENGINE = "true"

[mounts]
  source = "automoney_data"
  destination = "/data"

[http_service]
  internal_port = 8000
  force_https = true
  auto_stop_machines = "off"
  auto_start_machines = true
  min_machines_running = 1

[[vm]]
  memory = "512mb"
  cpu_kind = "shared"
  cpus = 1

[checks]
  [checks.health]
    type = "http"
    port = 8000
    path = "/health"
    interval = "15s"
    timeout = "5s"

  [checks.engine_alive]
    type = "http"
    port = 8000
    path = "/health/engine"
    interval = "30s"
    timeout = "5s"
    grace_period = "60s"
```

- [ ] **Step 3: Verify TOML parses**

Run: `"C:/Users/Wallace/Python313/python.exe" -c "import tomllib; d=tomllib.load(open('fly.toml','rb')); print('mount:', d.get('mounts')); print('vm_mem:', d.get('vm',[{}])[0].get('memory')); print('checks:', list(d.get('checks',{}).keys()))"`
Expected: mount populated, vm_mem=512mb, checks=['health','engine_alive']

- [ ] **Step 4: Commit**

```bash
git add fly.toml
git commit -m "chore(flyio): volume mount + 512mb + engine_alive check + DB_PATH env

- Volume 'automoney_data' mounted at /data (DB_PATH points there)
- VM bumped to 512mb (256mb tight with funding poller + WS pump)
- Second health check 'engine_alive' wired to /health/engine
- LOG_FORMAT=json for cleaner Fly log shipping
- START_ENGINE=true so the bot autostarts on machine boot

Spec: docs/superpowers/specs/2026-05-11-flyio-deploy-design.md
"
```

---

## Task 3: Dockerfile — bump to Python 3.13

**Files:**
- Modify: `Dockerfile`

- [ ] **Step 1: Replace Dockerfile**

Overwrite `Dockerfile`:

```dockerfile
FROM python:3.13-slim

# Build deps for native packages (eth-account, web3, dydx-v4-client may need)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

- [ ] **Step 2: Test build locally (optional but recommended)**

Run: `docker build -t automoney-test . 2>&1 | tail -20`
Expected: successful build, no pip install errors

If `dydx-v4-client` fails (native build issue):
1. Edit `requirements.txt`, comment out `dydx-v4-client>=1.1,<2.0`
2. Rebuild
3. Document removal in commit message

(If docker not available locally, skip and rely on Fly build to surface issues.)

- [ ] **Step 3: Commit**

```bash
git add Dockerfile
git commit -m "chore(docker): bump base to python:3.13-slim + add build-essential

- Matches dev Python (3.13.1 local)
- build-essential needed for any native pip wheel builds
  (eth-account, coincurve, etc) on slim base

Spec: docs/superpowers/specs/2026-05-11-flyio-deploy-design.md
"
```

---

## Task 4: scripts/flyio_secrets.sh

**Files:**
- Create: `scripts/flyio_secrets.sh`

- [ ] **Step 1: Create the script**

Create `scripts/flyio_secrets.sh`:

```bash
#!/usr/bin/env bash
# Push all .env entries as Fly secrets in a single bulk operation.
#
# Idempotent: re-running updates secrets that changed and triggers
# one machine restart (vs N restarts with N invocations).
#
# Usage: bash scripts/flyio_secrets.sh
# Prereq: fly auth login + fly app exists

set -euo pipefail

if [[ ! -f .env ]]; then
    echo "ERROR: .env not found in cwd. Run from project root." >&2
    exit 1
fi

if ! command -v fly &> /dev/null; then
    echo "ERROR: fly CLI not in PATH. Install from https://fly.io/docs/flyctl/install/" >&2
    exit 1
fi

# Build the args list. Skip blank lines, comments, and DB_PATH (set in fly.toml).
args=()
skipped_keys=()
while IFS= read -r line; do
    # Strip inline comment + trim
    line="${line%%#*}"
    line="$(echo "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')"
    [[ -z "$line" ]] && continue
    [[ "$line" != *=* ]] && continue

    key="${line%%=*}"
    # Skip keys set in fly.toml [env] block (DB_PATH overrides anything in secrets)
    if [[ "$key" == "DB_PATH" || "$key" == "LOG_FORMAT" || "$key" == "PYTHONUNBUFFERED" || "$key" == "START_ENGINE" ]]; then
        skipped_keys+=("$key")
        continue
    fi
    args+=("$line")
done < .env

if [[ ${#args[@]} -eq 0 ]]; then
    echo "No secrets to push (all keys filtered)." >&2
    exit 1
fi

echo "Pushing ${#args[@]} secret(s) to Fly..."
[[ ${#skipped_keys[@]} -gt 0 ]] && echo "  Skipped (set in fly.toml): ${skipped_keys[*]}"

fly secrets set "${args[@]}"

echo ""
echo "Done. Machines will restart automatically to pick up new secrets."
echo "Run: fly status   to confirm restart completes"
```

- [ ] **Step 2: Make it executable**

Run: `chmod +x scripts/flyio_secrets.sh && ls -la scripts/flyio_secrets.sh`
Expected: `-rwxr-xr-x ...`

- [ ] **Step 3: Smoke parse (don't run fly, just verify bash syntax)**

Run: `bash -n scripts/flyio_secrets.sh && echo "syntax OK"`
Expected: `syntax OK`

- [ ] **Step 4: Commit**

```bash
git add scripts/flyio_secrets.sh
git commit -m "chore(flyio): scripts/flyio_secrets.sh — bulk push .env to Fly

Reads .env, filters comments + .toml-set keys (DB_PATH/LOG_FORMAT/etc),
bulk-sets all in one fly invocation (single restart vs N).

Idempotent — safe to re-run after .env changes.

Spec: docs/superpowers/specs/2026-05-11-flyio-deploy-design.md
"
```

---

## Task 5: scripts/flyio_migrate_db.sh

**Files:**
- Create: `scripts/flyio_migrate_db.sh`

- [ ] **Step 1: Create the script**

Create `scripts/flyio_migrate_db.sh`:

```bash
#!/usr/bin/env bash
# Migrate local automoney.db to Fly volume /data/automoney.db.
#
# Run AFTER:
#   1. fly volumes create automoney_data --size 1 --region iad
#   2. First fly deploy (so /data exists in the running machine)
#   3. stop.bat (so local bot is NOT writing to automoney.db during copy)
#
# Usage: bash scripts/flyio_migrate_db.sh

set -euo pipefail

if [[ ! -f automoney.db ]]; then
    echo "ERROR: automoney.db not found in cwd. Run from project root." >&2
    exit 1
fi

if ! command -v fly &> /dev/null; then
    echo "ERROR: fly CLI not in PATH." >&2
    exit 1
fi

# Snapshot what we're about to migrate, so user can verify post-deploy
echo "=== Local DB snapshot (op activa) ==="
"C:/Users/Wallace/Python313/python.exe" -c "
import sqlite3, json
c = sqlite3.connect('automoney.db')
c.row_factory = sqlite3.Row
op = c.execute('SELECT id, status, baseline_deposit_usd, pnl_window_since_ts, funding_paid_token0, funding_paid_token1 FROM operations WHERE status=\"active\"').fetchone()
if op:
    print(json.dumps(dict(op), indent=2))
else:
    print('NO ACTIVE OPERATION')
"
echo ""

read -r -p "Continue with migration? (yes/no): " confirm
if [[ "$confirm" != "yes" ]]; then
    echo "Aborted."
    exit 0
fi

echo ""
echo "Copying automoney.db -> Fly volume /data/automoney.db..."
echo "put automoney.db /data/automoney.db" | fly ssh sftp shell

echo ""
echo "Done. Restart the machine to load the migrated DB:"
echo "  fly machine list"
echo "  fly machine restart <machine_id>"
echo ""
echo "Then verify with:"
echo "  fly logs --no-tail | grep -E 'Restored|HedgeModel'"
```

- [ ] **Step 2: Make executable + parse check**

Run: `chmod +x scripts/flyio_migrate_db.sh && bash -n scripts/flyio_migrate_db.sh && echo "syntax OK"`
Expected: `syntax OK`

- [ ] **Step 3: Commit**

```bash
git add scripts/flyio_migrate_db.sh
git commit -m "chore(flyio): scripts/flyio_migrate_db.sh — sftp put + safety prompt

Snapshots active op state before copy (so user can verify Fly matches
post-restart). Confirms via prompt before sftp. Documents the restart
+ verify steps.

Spec: docs/superpowers/specs/2026-05-11-flyio-deploy-design.md
"
```

---

## Task 6: scripts/flyio_smoke_snapshot.sh + flyio_smoke_compare.sh

**Files:**
- Create: `scripts/flyio_smoke_snapshot.sh`
- Create: `scripts/flyio_smoke_compare.sh`

- [ ] **Step 1: Create snapshot script**

Create `scripts/flyio_smoke_snapshot.sh`:

```bash
#!/usr/bin/env bash
# Pre-cutover: capture local /sse/state for post-deploy diff.
# Output: tmp_out/snapshot_local.txt (gitignored)

set -euo pipefail
mkdir -p tmp_out

LOCAL_URL="http://127.0.0.1:8000/sse/state"
USER_PASS="admin:Wallace1"

echo "Fetching local state from $LOCAL_URL..."
curl -s -u "$USER_PASS" -N "$LOCAL_URL" 2>&1 | head -c 5000 > tmp_out/snapshot_local.txt

# Extract just the first SSE event JSON (deterministic, easier to diff)
"C:/Users/Wallace/Python313/python.exe" -c "
import sys, json, re
raw = open('tmp_out/snapshot_local.txt').read()
m = re.search(r'data: (\{.+?\})\n', raw)
if m:
    d = json.loads(m.group(1))
    # Strip volatile fields (timestamps, timings) for clean diff
    for k in ['last_update', 'last_iter_timings', 'log_lines']:
        d.pop(k, None)
    open('tmp_out/snapshot_local.json', 'w').write(json.dumps(d, indent=2, sort_keys=True))
    print('Saved tmp_out/snapshot_local.json (volatile fields stripped)')
else:
    print('FAILED to parse SSE event from local /sse/state')
    sys.exit(1)
"
```

- [ ] **Step 2: Create compare script**

Create `scripts/flyio_smoke_compare.sh`:

```bash
#!/usr/bin/env bash
# Post-cutover: fetch Fly state and diff vs local snapshot.

set -euo pipefail

if [[ ! -f tmp_out/snapshot_local.json ]]; then
    echo "ERROR: tmp_out/snapshot_local.json missing. Run flyio_smoke_snapshot.sh first." >&2
    exit 1
fi

FLY_URL="https://automoney.fly.dev/sse/state"
USER_PASS="admin:Wallace1"

echo "Fetching Fly state from $FLY_URL..."
curl -s -u "$USER_PASS" -N "$FLY_URL" 2>&1 | head -c 5000 > tmp_out/snapshot_fly.txt

"C:/Users/Wallace/Python313/python.exe" -c "
import sys, json, re
raw = open('tmp_out/snapshot_fly.txt').read()
m = re.search(r'data: (\{.+?\})\n', raw)
if not m:
    print('FAILED to parse SSE event from Fly')
    print('First 500 bytes:', raw[:500])
    sys.exit(1)
d = json.loads(m.group(1))
for k in ['last_update', 'last_iter_timings', 'log_lines']:
    d.pop(k, None)
open('tmp_out/snapshot_fly.json', 'w').write(json.dumps(d, indent=2, sort_keys=True))
print('Saved tmp_out/snapshot_fly.json')
"

echo ""
echo "=== DIFF (local vs fly, after stripping volatile fields) ==="
diff tmp_out/snapshot_local.json tmp_out/snapshot_fly.json && echo "IDENTICAL ✓" || echo ""
echo ""
echo "Expected differences: NONE in critical fields. If you see"
echo "operation_state, hedge_positions, pool_value_usd, baseline_deposit_usd"
echo "diverging — investigate before committing to the deploy."
```

- [ ] **Step 3: Make executable + syntax check**

Run: `chmod +x scripts/flyio_smoke_snapshot.sh scripts/flyio_smoke_compare.sh && bash -n scripts/flyio_smoke_snapshot.sh && bash -n scripts/flyio_smoke_compare.sh && echo "both syntax OK"`
Expected: `both syntax OK`

- [ ] **Step 4: Commit**

```bash
git add scripts/flyio_smoke_snapshot.sh scripts/flyio_smoke_compare.sh
git commit -m "chore(flyio): smoke snapshot + compare scripts

Pre-cutover: snapshot script captures /sse/state from local, strips
volatile fields (timings, timestamps), saves to tmp_out/snapshot_local.json.

Post-cutover: compare script fetches the same from Fly and runs diff.
Identical = safe to keep Fly running. Divergence in critical fields
(operation_state, hedge_positions, pool_value_usd, baseline_deposit_usd)
= rollback to local.

Spec: docs/superpowers/specs/2026-05-11-flyio-deploy-design.md
"
```

---

## Task 7: docs/flyio-runbook.md

**Files:**
- Create: `docs/flyio-runbook.md`

- [ ] **Step 1: Create the runbook**

Create `docs/flyio-runbook.md`:

```markdown
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
```

- [ ] **Step 2: Commit**

```bash
git add docs/flyio-runbook.md
git commit -m "docs(flyio): operational runbook for cutover + rollback

Step-by-step procedure for the user to:
1. Snapshot local pre-cutover
2. Create volume + push secrets
3. First (idle) deploy
4. Migrate DB + restart machine
5. Smoke compare local vs fly
6a. Continue (success) | 6b. Rollback (revert to local)

Plus post-deploy operational notes (logs, restart, SSH, backups).

Spec: docs/superpowers/specs/2026-05-11-flyio-deploy-design.md
"
```

---

## Task 8: .gitignore + final push

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Verify gitignore already has tmp_out/**

Run: `grep -c "^tmp_out" .gitignore`
Expected: `1` (added in earlier commit)

If 0, add it:
```bash
echo 'tmp_out/' >> .gitignore
```

- [ ] **Step 2: Run full test suite to confirm no regression**

Run: `"C:/Users/Wallace/Python313/python.exe" -m pytest tests/test_health_engine.py tests/test_engine_dual_leg.py tests/test_state.py tests/test_web.py --tb=no -q 2>&1 | tail -5`
Expected: all green (3 new + existing)

- [ ] **Step 3: Push branch**

```bash
git push -u origin feature/flyio-deploy
```

- [ ] **Step 4: Open PR**

```bash
printf 'protocol=https\nhost=github.com\n\n' | git credential fill 2>/dev/null \
  | grep '^password=' | head -1 | sed 's/^password=//' | (read tok; export GH_TOKEN="$tok"; \
gh pr create --base master --head feature/flyio-deploy \
  --title "feat: Fly.io deploy — volume, secrets, /health/engine watchdog, runbook" \
  --body "$(cat <<'EOF'
Sobe AutoMoney pro Fly.io (`iad` region) com persistência de DB via volume,
secrets gerenciados, segundo health check (loop watchdog), e runbook
operacional pra cutover + rollback da op #28 (capital real \$447).

## What changed

- **\`app.py\`** — novo endpoint \`/health/engine\` retorna 200 se hub.last_update < 30s, 503 senão. Excluído de basic auth (Fly probes são internos).
- **\`fly.toml\`** — volume mount em \`/data\`, VM bumped pra 512mb, segundo \`[checks]\` block (\`engine_alive\`), env vars (\`DB_PATH\`, \`LOG_FORMAT\`, \`START_ENGINE\`).
- **\`Dockerfile\`** — bumped pra \`python:3.13-slim\`, adiciona \`build-essential\` pra wheels nativas.
- **\`scripts/flyio_secrets.sh\`** — push idempotente do \`.env\` pra Fly secrets.
- **\`scripts/flyio_migrate_db.sh\`** — sftp put do \`automoney.db\` pra \`/data\`, com snapshot prévio.
- **\`scripts/flyio_smoke_snapshot.sh\`** + **\`flyio_smoke_compare.sh\`** — captura state local pré-cutover, fetcha state Fly pós-cutover, gera diff.
- **\`docs/flyio-runbook.md\`** — runbook operacional passo-a-passo.

## Test plan

Code (subagent-driven):
- [x] \`tests/test_health_engine.py\` — 3 testes: 200 quando recente, 503 quando stale, sem auth
- [x] Existing engine + state + web tests verdes

Operacional (user-executed após merge):
- [ ] Snapshot local
- [ ] \`fly volumes create\`
- [ ] \`bash scripts/flyio_secrets.sh\`
- [ ] \`fly deploy\` (idle)
- [ ] \`stop.bat\` + \`flyio_migrate_db.sh\` + \`fly machine restart\`
- [ ] \`bash scripts/flyio_smoke_compare.sh\` → verify identical
- [ ] Aguardar 30 min observando logs
- [ ] Se OK: declarar deploy estável, encerrar local permanentemente

## Risks & mitigations
- **\`dydx-v4-client\` build issue:** mitigated by \`build-essential\` in Dockerfile; if still fails, comentar do requirements (não usamos).
- **WAF Lighter no IP do Fly:** improvável com Alchemy + WS; se acontecer, runbook documenta rollback.
- **10-min janela unhedged:** ~\$0.50-\$2 exposição estimada (LP \$447, mercado normal).
- **DB corruption durante migração:** rollback pra local (DB intacto) em 1 comando.

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)" 2>&1 | tail -3)
```

- [ ] **Step 5: Update WORKING_ON.md + push**

(Will be done by controller after PR is open — not subagent task.)

---

## Verification (post-PR-merge, user-executed)

See `docs/flyio-runbook.md`. Code changes are validated by `tests/test_health_engine.py`. Deploy itself is operational and not test-automatable.

## Self-review notes

- ✅ Spec coverage:
  - §1 fly.toml extended → T2 ✓
  - §2 /health/engine → T1 ✓
  - §3 Dockerfile bump → T3 ✓
  - §4 dydx-v4-client risk → noted in T3 + addressed via build-essential
  - §5 migrate script → T5 ✓
  - §6 secrets script → T4 ✓
  - §7 smoke snapshot/compare → T6 ✓
  - Cutover sequence → T7 (runbook) ✓
- ✅ No placeholders; every step has full code or full command
- ✅ Type/method signatures consistent (only one new function `health_engine`, used consistently)
- ⚠ Tasks 4-7 are NOT TDD because they are bash scripts + docs — `bash -n` syntax check is the only automated validation. Acceptable per the operational nature.
- ⚠ Task 1 test fixture relies on `app.state.hub` being settable post-create_app. Verified in app.py:211 that `app.state.hub = state` happens before any route handler runs.
