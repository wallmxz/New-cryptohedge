# Fly.io Deploy — Design

**Date:** 2026-05-11
**Status:** Approved (brainstorm)
**Branch:** new branch off master (suggest `feature/flyio-deploy`)

## Problem

Bot atualmente roda em Windows local (`uvicorn` na :8000), com `start.bat`/`stop.bat`. Riscos:
- Máquina dorme/desliga → bot offline → posição $447 unhedged
- Power outage / Windows update → restart manual
- ARBITRUM_RPC_URL via internet residencial SP → 1.4s latência (já mitigado parcialmente com Alchemy, vai pra ~30ms do `iad`)
- Sem HA, sem monitoramento remoto, sem reboot automático

Solução: subir o bot pro Fly.io com:
- Volume persistente pro DB (não perder estado entre deploys)
- Secrets gerenciados (não expor `.env` em git/imagem)
- HTTPS automático com basic auth mantido
- Health check duplo (HTTP + engine alive)
- Migração da op #28 ATIVA sem perda de estado
- DB local fica como backup pra rollback de emergência

## Goal

Deploy operacional do AutoMoney no Fly.io com:
1. Op #28 (capital real $447) migrada com janela de unhedge ≤15 min
2. Bot resume operação automaticamente do DB persistido
3. Smoke test verificável: números do `/sse/state` do Fly batem com snapshot pré-cutover do local
4. Rollback de emergência em 1 comando (`fly scale count 0` + `start.bat`)

## Non-goals

- Multi-instance HA (single-user, single-vault)
- CI/CD pipeline (deploy manual via `fly deploy`)
- Custom domain (usa `automoney.fly.dev`)
- Métricas externas (Datadog, Grafana cloud) — Prometheus `/metrics` já existe e basta
- Modo "shadow" / dual-bot — descartado durante brainstorm (overengineering)

## Architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  USER BROWSER (SP)                                               │
│    https://admin:Wallace1@automoney.fly.dev/                     │
└────────────────────┬─────────────────────────────────────────────┘
                     │ HTTPS (Fly TLS)
                     ▼
┌──────────────────────────────────────────────────────────────────┐
│  FLY MACHINE (iad region, 256mb RAM, 1 shared cpu)               │
│                                                                   │
│  uvicorn :8000 (basic auth admin/Wallace1 via web/auth.py)       │
│                                                                   │
│  /health        → {"status":"ok"} (Fly checks every 15s)         │
│  /health/engine → {"alive":true|false} (NEW — checks loop age)   │
│  /              → dashboard                                      │
│  /sse/state, /metrics, /operations/* → existing                  │
│                                                                   │
│  ENGINE: predictive hedge model (post-PR #2)                     │
│         + funding window (post-PR #3)                            │
│         polls Alchemy + Lighter at ~1Hz                          │
│                                                                   │
│  DB: /data/automoney.db (Fly Volume mount)                       │
│  Volume: automoney_data (1GB, snapshots 5d)                      │
└────────────────────┬─────────────────────────────────────────────┘
                     │
        ┌────────────┴────────────┐
        ▼                         ▼
┌─────────────────┐     ┌────────────────────┐
│  Alchemy RPC    │     │  Lighter API/WS    │
│  Arbitrum       │     │  perp markets      │
│  (~30ms iad)    │     │  (~20ms iad)       │
└─────────────────┘     └────────────────────┘
```

## Components

### 1. `fly.toml` — extended

Update existing (already has app/region/dockerfile/health). Add:

```toml
[mounts]
  source = "automoney_data"
  destination = "/data"

[env]
  PYTHONUNBUFFERED = "true"
  DB_PATH = "/data/automoney.db"
  LOG_FORMAT = "json"

[[vm]]
  memory = "512mb"  # bump from 256mb — Phase 1.4 backtest module + funding poller
  cpu_kind = "shared"
  cpus = 1

[checks]
  [checks.health]
    type = "http"
    port = 8000
    path = "/health"
    interval = "15s"
    timeout = "5s"
  [checks.engine_alive]                    # NEW
    type = "http"
    port = 8000
    path = "/health/engine"
    interval = "30s"
    timeout = "5s"
    grace_period = "60s"  # boot time
```

### 2. `app.py` — `/health/engine` endpoint (new)

Add a route that returns 200 if engine loop has run within last 30s, else 503:

```python
async def health_engine(request: Request) -> JSONResponse:
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

Register in routes list. No auth (Fly's health check is internal).

### 3. `Dockerfile` — minor update

Current is fine. Bump base to `python:3.13-slim` to match dev:
```dockerfile
FROM python:3.13-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
EXPOSE 8000
CMD ["python", "-m", "uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
```

### 4. `requirements.txt` — verify dydx-v4-client install path

Current adapters: ACTIVE_EXCHANGE=lighter. dydx-v4-client is in requirements but its native deps fail on some build envs. The conftest stub lets tests pass without it locally. For Fly:
- Linux build env has all needed compilers → should install cleanly
- If it fails: add `--no-deps` workaround or remove dydx-v4-client from requirements (since we use Lighter)

Plan: try clean build first; if fails, exclude dydx-v4-client (we don't use it).

### 5. Migration script: `scripts/flyio_migrate_db.sh`

```bash
#!/usr/bin/env bash
# Migrates local automoney.db to Fly volume.
# Run AFTER `fly volumes create automoney_data --size 1`
# and AFTER first successful `fly deploy` (so the volume is mounted).

set -euo pipefail
echo "Snapshot atual da op ativa (pra comparar pós-deploy):"
"C:/Users/Wallace/Python313/python.exe" -c "
import sqlite3
c = sqlite3.connect('automoney.db')
op = c.execute('SELECT * FROM operations WHERE status=\"active\"').fetchone()
print(op)
"
echo ""
echo "Copiando automoney.db pra Fly volume /data..."
fly ssh sftp shell -C "put automoney.db /data/automoney.db"
echo "Done. Restart machine pra carregar o DB novo:"
echo "  fly machine restart <machine_id>"
```

### 6. Secrets setup: `scripts/flyio_secrets.sh`

```bash
#!/usr/bin/env bash
# One-shot: read .env and push all entries as Fly secrets.
# Idempotent — safe to re-run.

set -euo pipefail
echo "Pushing .env entries to Fly secrets..."
secrets=()
while IFS= read -r line; do
    line="${line%%#*}"  # strip comments
    line="${line## }"; line="${line%% }"  # trim
    [[ -z "$line" ]] && continue
    [[ "$line" != *=* ]] && continue
    secrets+=("$line")
done < .env

# Bulk push (one fly invocation = single restart of machines)
fly secrets set "${secrets[@]}"
echo "Secrets pushed. ${#secrets[@]} keys total."
```

### 7. Smoke test snapshot: `scripts/flyio_smoke_snapshot.sh`

Captures current local state for post-deploy comparison.

```bash
#!/usr/bin/env bash
# Pre-cutover: capture local state so we can verify Fly matches it.

mkdir -p tmp_out
curl -s -u admin:Wallace1 -N http://127.0.0.1:8000/sse/state \
    | head -c 5000 > tmp_out/snapshot_local.txt
echo "Local snapshot saved to tmp_out/snapshot_local.txt"
echo "After Fly deploy completes, run:"
echo "  curl -s -u admin:Wallace1 https://automoney.fly.dev/sse/state | head -c 5000 > tmp_out/snapshot_fly.txt"
echo "  diff tmp_out/snapshot_local.txt tmp_out/snapshot_fly.txt"
```

## Cutover sequence (operational runbook)

Documented in plan, but conceptually:

1. **Pre-cutover (5 min):**
   - Run `scripts/flyio_smoke_snapshot.sh` → save local state
   - `fly volumes create automoney_data --size 1 --region iad`
   - `bash scripts/flyio_secrets.sh` → push all .env entries

2. **First deploy (5 min, app idle on Fly):**
   - `fly deploy` → builds image, starts machine
   - Volume is empty so DB doesn't exist; bot boots in operation_state=NONE
   - `fly logs` → confirm Lighter WS connects, HedgeModel ready

3. **Migration window (10 min unhedged):**
   - `stop.bat` → uvicorn local mata, op #28 fica sem hedge ATIVO mas LP intacta
   - `bash scripts/flyio_migrate_db.sh` → SFTP copia DB pro volume
   - `fly machine restart <id>` → bot reload com DB
   - `fly logs` → confirm "Restored active operation 28"

4. **Smoke verify (5 min):**
   - Browser: `https://admin:Wallace1@automoney.fly.dev/`
   - Compare with `snapshot_local.txt` — números devem bater
   - `hedge_model_status: active`, `pool_value_usd ≈ $447`, hedge sizes iguais
   - Aguardar primeiro fire (deve acontecer em <30s se há drift acumulado)

5. **OK ou rollback:**
   - **OK:** deixa rodando. `tail -f` log opcional via `fly logs --follow`.
   - **Rollback:** `fly scale count 0` (para máquina) + `start.bat` local. DB local intacto.

## Testing

Não dá pra testar fly deploy via pytest (precisa cloud real). Tests cobrem:

`tests/test_health_engine.py` (NEW, 3 tests):
1. `test_health_engine_returns_200_when_loop_recent` — mock hub.last_update = now
2. `test_health_engine_returns_503_when_loop_stale` — last_update = now - 60
3. `test_health_engine_returns_503_when_last_update_missing` — hub no attr

Migration scripts são bash — sem pytest. Documentar contra-execução manual.

## Risks

1. **dydx-v4-client install no Fly build** — pode quebrar deploy. Mitigação: tentar primeiro; se falhar, comentar do requirements (não usamos). Test: rebuild local Docker antes de deploy real.

2. **Volume não monta na primeira vez** — Fly às vezes tem race entre create/deploy. Mitigação: `fly volumes list` antes de deploy pra confirmar criação.

3. **WS Lighter rejeita conexão do IP do Fly** — possível CloudFront WAF. Mitigação: testar `fly ssh console` + curl Lighter API antes de subir engine.

4. **DB SQLite + Fly volume = corrupção em crash** — improvável (SQLite é robusto), mas worst case = rollback pro local. Snapshots diários cobrem.

5. **Op #28 com state inconsistente após migração** — improvável (DB é cópia byte-exata) mas verify obrigatório no smoke test.

6. **Cold start engine — `_hedge_model` cache vazio** — primeiro iter no Fly precisa popular cache. Já testado — graceful degradation pra Beefy actual.

7. **Janela de 10 min unhedged** — risco real. Pra LP de $447 a 1% movimento = $4.47 exposição. Tipicamente ~$0.50-$2 em 10min de movimento natural.

8. **Build/deploy taking >15 min** — Fly builds podem demorar. Mitigação: pré-buildar imagem local com `docker build` pra testar antes de `fly deploy`.

## Verification

Post-deploy checklist (incluído no plano de cutover):
- [ ] `fly status` → machine running
- [ ] `fly logs` → "Restored active operation 28" presente
- [ ] `curl https://automoney.fly.dev/health` → `{"status":"ok"}`
- [ ] `curl https://automoney.fly.dev/health/engine` → `{"alive":true}`
- [ ] `curl -u admin:Wallace1 https://automoney.fly.dev/sse/state` → JSON com `operation_state: active`, `current_operation_id: 28`
- [ ] Diff snapshot local vs fly → diferenças apenas em timings/timestamps
- [ ] Aguardar 30 min observando — se nada exotérico no log, considerado estável
- [ ] Após 24h estável: declarar deploy concluído, encerrar `stop.bat` local permanentemente

## Out of scope (futuro)

- Auto-scaling (single-user, sempre 1 machine)
- Multi-region failover
- Encrypted DB at rest (SQLite + Fly volume = at-rest é discos cifrados pelo Fly já)
- Webhook alerts auto-config (já tem infra, user configura via UI)
- Backups offsite (snapshots Fly são suficientes pra single-user $447)
