# Fase 1.3 — Observability + Cleanup

## Objetivo

Tornar o bot **operável em produção** sem virar arqueologia em debug. Adicionar:

1. Métricas Prometheus em endpoint `/metrics`
2. Logs estruturados JSON (stdout, Fly.io coleta)
3. Latency tracing por step do loop principal, exposto na UI
4. Cleanup de campos legacy do `Settings` (Hyperliquid)

**Fora do escopo:**
- Adaptive grid spacing — analisado e descartado (grade já está em densidade máxima do exchange)
- Backtesting framework — Fase 1.4 separada
- Grafana/dashboards externos — usuário plugs no que quiser via `/metrics`

## Modelo conceitual

### Métricas

Prometheus-style. Três tipos:
- **Counter**: monotônico crescente (fills_total, alerts_fired_total)
- **Gauge**: valor atual (margin_ratio, pool_value_usd, grid_orders_open)
- **Histogram**: distribuição de durações (loop_duration_seconds com buckets)

Endpoint `/metrics` retorna texto plain Prometheus exposition format. Sem auth (padrão na indústria — quem hospeda decide se restringe via firewall/proxy).

### Logs estruturados

Substitui `logging.basicConfig` atual por handler JSON. Cada log carrega:
- Campos padrão: `level`, `logger`, `timestamp`, `message`
- Contexto: `operation_id` (quando ativo), `iter` (loop count), `op` (action like `grid_place`, `fill`, `aggressive`)
- Dados: chave-valor relevante ao evento

Modo controlado por env var `LOG_FORMAT`:
- `json` (default em produção / Fly.io)
- `plain` (default em dev local quando não setado)

### Latency tracing

Cada iteração do loop principal mede tempo de cada step:
- `chain_read` — read_position + read_price
- `margin_check` — get_position + get_collateral + margin calc
- `grid_compute` — target grid generation
- `grid_diff_apply` — diff + batch_place + batch_cancel
- `pnl_breakdown` — operation PnL update
- `total` — wall-clock da iteração

Armazenado em `hub.last_iter_timings: dict[str, float]` (ms). Atualizado a cada loop. Exibido no dashboard como pequeno health card mostrando timings + sparkline simples.

Também alimenta histograma Prometheus `bot_loop_duration_seconds{step="<name>"}`.

### Cleanup Settings legacy

`config.py::Settings` ainda tem:
- `hyperliquid_api_key: str`
- `hyperliquid_api_secret: str`
- `hyperliquid_symbol: str`

E `web/routes.py:68` ainda escolhe symbol baseado em `active_exchange == "hyperliquid"`. O adapter Hyperliquid foi removido na Fase 1.2 — esses campos nunca são lidos por código vivo. Removê-los simplifica o tipo e elimina confusão.

`Settings.active_exchange` também pode ser simplificado pra `dydx` fixo (mas mantemos como string config pra futura abertura, caso queira voltar Hyperliquid).

## Arquitetura

### Módulos novos

| Módulo | Função |
|---|---|
| `engine/metrics.py` | Prometheus registry + helpers (counter/gauge/histogram factories) |
| `web/logging_config.py` | Configuração de logger (JSON vs plain) baseada em `LOG_FORMAT` |

### Módulos modificados

| Módulo | Mudança |
|---|---|
| `requirements.txt` | adiciona `prometheus-client`, `python-json-logger` |
| `app.py` | route `/metrics`; chama `setup_logging()` no startup |
| `engine/__init__.py` | instrumenta `_iterate` (timings + counters); chama `metrics.observe_*` |
| `state.py` | adiciona `last_iter_timings: dict` |
| `web/routes.py` | endpoint `/metrics`; remove fallback hyperliquid_symbol |
| `config.py` | remove `hyperliquid_*` fields |
| `web/templates/partials/health.html` (novo) | card de health/timings |
| `web/templates/dashboard.html` | inclui partial novo |
| `web/static/app.js` | state field + render do card |

### Tests

| Test | Cobertura |
|---|---|
| `tests/test_metrics.py` | Registry, helpers, formato Prometheus do output |
| `tests/test_logging_config.py` | LOG_FORMAT controla handler |
| `tests/test_engine_grid.py` (estendido) | _iterate popula `hub.last_iter_timings` |
| `tests/test_web.py` (estendido) | GET /metrics retorna 200 com Content-Type Prometheus |
| `tests/test_config.py` (atualizado) | Não tem mais hyperliquid_* fields |

## Métricas detalhadas

```python
# engine/metrics.py registers:

# Counters
fills_total = Counter("bot_fills_total", "Total fills", ["liquidity", "side"])
alerts_total = Counter("bot_alerts_total", "Alerts fired", ["level"])
operations_total = Counter("bot_operations_total", "Operations", ["status"])  # started, closed, failed
aggressive_corrections_total = Counter("bot_aggressive_corrections_total", "Taker escalations")

# Gauges
margin_ratio = Gauge("bot_margin_ratio", "Current margin ratio")
pool_value_usd = Gauge("bot_pool_value_usd", "Current pool value")
hedge_position_size = Gauge("bot_hedge_position_size", "Current short size in base units")
grid_orders_open = Gauge("bot_grid_orders_open", "Currently open grid orders")
operation_state = Gauge("bot_operation_state", "1 if operation active, 0 otherwise")
out_of_range = Gauge("bot_out_of_range", "1 if pool out of range")

# Histograms
loop_duration = Histogram("bot_loop_duration_seconds", "Iteration duration", ["step"],
                          buckets=[0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 5.0])
```

Padrão de instrumentation no `_iterate`:

```python
async def _iterate(self):
    iter_start = time.monotonic()
    self._iter_count += 1
    timings = {}

    t = time.monotonic()
    beefy_pos = await self._beefy_reader.read_position()
    p_now = await self._pool_reader.read_price()
    timings["chain_read"] = (time.monotonic() - t) * 1000
    metrics.loop_duration.labels(step="chain_read").observe(timings["chain_read"] / 1000)

    # ... continues with timing each major step ...

    timings["total"] = (time.monotonic() - iter_start) * 1000
    metrics.loop_duration.labels(step="total").observe(timings["total"] / 1000)
    self._hub.last_iter_timings = timings
```

## /metrics endpoint

```python
# web/routes.py
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

async def metrics(request: Request):
    body = generate_latest()
    return Response(body, media_type=CONTENT_TYPE_LATEST)
```

Sem auth — caller na frente (Fly.io ingress, nginx, etc.) decide. Adicionar `/metrics` à lista de paths excluídos do BasicAuthMiddleware.

## Logging config

```python
# web/logging_config.py
import logging
import os
from pythonjsonlogger import jsonlogger


def setup_logging():
    fmt = os.environ.get("LOG_FORMAT", "plain").lower()
    handler = logging.StreamHandler()

    if fmt == "json":
        formatter = jsonlogger.JsonFormatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            rename_fields={"levelname": "level", "name": "logger", "asctime": "timestamp"},
        )
    else:
        formatter = logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s: %(message)s"
        )

    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)
```

Chamado em `app.py::create_app()`. Substitui o `logging.basicConfig` existente.

Loggers no engine usam `logger.info("Operation started", extra={"operation_id": op_id, "iter": self._iter_count})`. JSON formatter renderiza como:
```json
{"timestamp":"2026-04-28 10:30:01,123","level":"INFO","logger":"engine","message":"Operation started","operation_id":42,"iter":12}
```

## Health card no dashboard

Novo partial `web/templates/partials/health.html`:

```html
<div class="card">
    <p class="card-title">Saúde do loop</p>
    <div class="grid grid-cols-2 md:grid-cols-4 gap-3">
        <template x-for="step in healthSteps" :key="step.name">
            <div>
                <p class="text-xs text-slate-400 mb-1" x-text="step.label"></p>
                <p class="text-sm font-mono"
                   :class="step.ms > 1000 ? 'text-red-500' : step.ms > 500 ? 'text-amber-600' : 'text-slate-700'"
                   x-text="step.ms.toFixed(0) + 'ms'"></p>
            </div>
        </template>
    </div>
</div>
```

`healthSteps` é getter no Alpine que mapeia `state.last_iter_timings` pra lista ordenada com labels pt-BR.

Posicionado na aba Painel, depois do "Status da grade".

## Cleanup legacy

Remover de `config.py`:
- Field `hyperliquid_api_key: str`
- Field `hyperliquid_api_secret: str`
- Field `hyperliquid_symbol: str`
- Linhas correspondentes em `from_env()`

Atualizar `web/routes.py:68`:
```python
# antes:
"symbol": settings.hyperliquid_symbol if settings.active_exchange == "hyperliquid" else settings.dydx_symbol,
# depois:
"symbol": settings.dydx_symbol,
```

Atualizar `tests/test_config.py` — remover assertions que mencionam `hyperliquid_*`.

Atualizar `.env.example` — remover bloco "Legacy Hyperliquid" (que tinha sido posto na T1 da Fase 1.1).

## Não-objetivos

- Adaptive grid spacing (descartado nesta sessão — grade já em densidade máxima)
- Grafana provisioning, alerting rules
- Tracing distribuído (OpenTelemetry, Jaeger)
- Métricas customizadas pra dYdX/Beefy (só engine internas por ora)
- Mudanças em `engine/__init__.py` além de instrumentation

## Critérios de aceitação

1. `GET /metrics` retorna 200 com Content-Type `text/plain; version=0.0.4` e expõe pelo menos os counters/gauges/histograms listados
2. Setando `LOG_FORMAT=json` e iniciando o app, todos os logs saem como JSON válido com fields enriquecidos
3. Após uma iteração do engine, `hub.last_iter_timings` tem todos os 5 steps preenchidos com valores ms
4. Dashboard exibe o card de saúde com os timings
5. `Settings` não tem mais campos `hyperliquid_*`
6. Suite completa: 111 + ~10 novos = ~121 tests passando

## Riscos

| Risco | Mitigação |
|---|---|
| Prometheus client tem registry global | usar custom registry em `engine/metrics.py` ou aceitar global (mais simples) |
| python-json-logger formatter quebra logs existentes | testar `extra={}` patterns existentes; fallback graceful |
| Cleanup do hyperliquid_* quebra tests existentes | atualizar tests no mesmo commit |
| `/metrics` aberto sem auth pode vazar info | risco baixo (só métricas internas, nada sensível) — documentar pra colocar atrás de proxy se preocupar |
