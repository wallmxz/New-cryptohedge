# CLAUDE.md — context for new sessions

This file is read by Claude Code at session start. Keep it current.

## What this project is

**AutoMoney** — bot delta-neutro que market-makeia o hedge de uma LP concentrada (Beefy CLM WETH/USDC na Arbitrum) com short em perpétuo na dYdX v4. A grade densa de ordens maker no perp replica continuamente a curva de exposição da LP, capturando fees de market making enquanto se mantém delta-neutral em relação à pool.

Stack: Python 3.14, asyncio, Starlette + Alpine.js, web3.py, dydx-v4-client, aiosqlite. Single-user. Deploy Fly.io.

## Onde procurar primeiro

**Antes de qualquer trabalho novo, ler na ordem:**

1. **`docs/STATUS.md`** — estado atual do projeto, o que funciona, o que não
2. **`docs/grid-engine-runbook.md`** — como rodar o bot, troubleshooting
3. **`docs/superpowers/specs/`** — specs de cada fase (decisões de arquitetura)
4. **`docs/superpowers/plans/`** — plans de implementação task-a-task
5. **`docs/grid-engine-implementation-summary.md`** — resumo da Phase 1.1
6. `git log --oneline | head -50` — histórico recente

## Estado atual

### Concluído

- ✅ **Phase 1.1 — Grid Maker Engine** (tag `fase-1.1-completa`, master)
  - 28 tasks, 97 testes
  - Engine que mantém grade de ordens maker no dYdX espelhando curva V3 da LP
  - Reconciler periódico, recovery após restart, margin monitor com webhook alerts
  - Spec: `docs/superpowers/specs/2026-04-27-grid-maker-engine-design.md`
  - Plan: `docs/superpowers/plans/2026-04-27-grid-maker-engine.md`

- ✅ **Phase 1.2 — Operation Lifecycle** (tag `fase-1.2-completa`, master)
  - 15 tasks, 111 testes
  - Lifecycle explícito (NONE→STARTING→ACTIVE→STOPPING→CLOSED) via `engine/operation.py`
  - `start_operation()` / `stop_operation()` com baseline snapshot + bootstrap/close via taker
  - PnL por operação detalhado (LP fees, Beefy perf fee, IL natural, hedge PnL, funding, perp fees, bootstrap slippage)
  - REST endpoints: `/operations` (GET list), `/operations/current` (GET), `/operations/start` (POST), `/operations/stop` (POST)
  - UI: card de operação + aba histórico
  - Recovery: restaura operação ativa do DB no startup
  - Cleanup: removidos `engine/hedge.py`, `chains/evm.py`, `exchanges/hyperliquid.py` + tests
  - Spec: `docs/superpowers/specs/2026-04-27-operation-lifecycle-design.md`
  - Plan: `docs/superpowers/plans/2026-04-27-operation-lifecycle.md`

- ✅ **Phase 1.3 — Observability + Cleanup** (tag `fase-1.3-completa`, branch feature/observability)
  - 11 tasks, 124 testes
  - Prometheus metrics em `/metrics` (counters/gauges/histograms; auth bypassed)
  - Logs estruturados JSON via `LOG_FORMAT=json` env var
  - Latency tracing: `hub.last_iter_timings` populado a cada loop, exposto em health card no dashboard
  - Cleanup: removidos `hyperliquid_api_key/secret/symbol` do `Settings`
  - Stack adicionada: `prometheus-client>=0.21,<1.0`, `python-json-logger>=2.0,<3.0`
  - Spec: `docs/superpowers/specs/2026-04-28-observability-design.md`
  - Plan: `docs/superpowers/plans/2026-04-28-observability.md`

- ✅ **Phase 1.4 — Backtesting Framework** (tag `fase-1.4-completa`, branch feature/backtesting)
  - 12 tasks (T0–T13), 142 testes (124 da base + 18 backtest)
  - CLI: `python -m backtest --vault X --pool Y --from <date> --to <date> [--capital 300] [--margin 130]`
  - Reusa `GridMakerEngine` real com mocks de exchange (`MockExchangeAdapter`) e chain (`MockPoolReader`/`MockBeefyReader`)
  - Data layer: ETH price (Coinbase, paginado), dYdX funding (indexer com dedupe), Beefy APR (API com fallback constante)
  - Cache SQLite local (`backtest_cache.db`) pra evitar re-fetches
  - Output: net PnL via `state.operation_pnl_breakdown` (inclui IL natural, hedge PnL, funding, fees, slippage); APR LP + APR total; max drawdown; fills maker/taker; JSON opcional via `--output`
  - Mock exchange enforça margin gate (5x collateral) — modela rejeição de dYdX em produção
  - T0 cleanup: removidos `max_exposure_pct`, `repost_depth`, `threshold_recovery`, `pool_deposited_usd`, `engine/pnl.py::calc_pnl`, `PnLBreakdown`; `threshold_aggressive` agora 1% default
  - Spec: `docs/superpowers/specs/2026-04-29-backtesting-design.md`
  - Plan: `docs/superpowers/plans/2026-04-29-backtesting.md`

- ✅ **Phase 2.0 — On-chain Execution Automatica** (tag `fase-2.0-completa`, branch feature/onchain-execution)
  - 13 tasks (T0–T12 + T13 follow-up), 170 testes (~33 novos: lp_math, chain_executor, uniswap_executor, beefy_executor, lifecycle, lifecycle_recovery)
  - **1-click start:** bot faz approve + swap USDC->WETH (same-pool 0,05%) + deposit Beefy CLM + snapshot + open dYdX short
  - **1-click stop:** cancel grid + close short + withdraw Beefy + (opcional) swap WETH->USDC
  - **Custo round-trip:** ~$0,08 steady-state (~30× redução vs ~$3 manual atual = ~31% APR consumido)
  - Modulos novos: `chains/executor.py` base + `uniswap_executor.py` + `beefy_executor.py`; `engine/lifecycle.py` (state machine 16-state) + `engine/lp_math.py` (V3 split math)
  - State machine + tx_hash idempotency persistida no DB (`bootstrap_state` enum: pending → approving → swap_pending → swap_confirmed → deposit_pending → ... → active → teardown_* → closed/failed)
  - Crash recovery: `OperationLifecycle.resume_in_flight()` chamado em startup; MVP marca operações in-flight como `failed` pra revisão manual via UI
  - REST API: POST `/operations/start` aceita `{usdc_budget}`; `/operations/stop` aceita `{swap_to_usdc}`; novo POST `/operations/cashout`; novo GET `/wallet`
  - UI: modal Start com input USDC + Max wallet button; operation card mostra progress + Arbiscan tx links; settings exibe slippage, WETH residual + Cash out button, wallet ETH balance com warning low-balance
  - Pre-flight: rejeita start se wallet tem < 0.005 ETH (gas reserve)
  - Backwards compat: ops legacy (sem `usdc_budget`) continuam usando path Phase 1.2 quando lifecycle está configurado mas op não tem `bootstrap_state` (evita drain acidental de LP pre-existente)
  - Gap conhecido: ABI da Beefy CLM (`abi/beefy_clm_strategy_write.json`) usa shape canônico; verificar contra contrato deployed via Arbiscan antes de mainnet
  - Spec: `docs/superpowers/specs/2026-04-29-onchain-execution-design.md`
  - Plan: `docs/superpowers/plans/2026-04-29-onchain-execution.md`

### Não iniciado

- Pré-produção — Testnet rehearsal antes de mainnet (verificar ABIs reais, smoke flow real, ETH mainnet)
- Adaptive grid spacing — descartado em Phase 1.3 (grade já em densidade máxima do exchange)
- Lifecycle: full auto-resume de in-flight ops (atualmente MVP marca como `failed`; UI pode evoluir pra ter botão "Retry from state" ao invés de só "Force close")
- Anvil fork test pra validar ABIs e calldata de Uniswap/Beefy contra contratos reais sem queimar gas

### Fixes aplicados pós-Phase 2.0

- ✅ **Engine `_aggressive_correct` cooldown** (`9016741`): adicionado cooldown in-memory de 30s pra prevenir re-fire de takers de correção quando o anterior ainda não filled. Antes, em latência alta ou no simulator, o mesmo correction fired toda iteration → stack de takers que blow up no próximo cross. Fix: `engine/__init__.py` checa `_last_aggressive_correction_at` antes de chamar `_aggressive_correct`. 3 tests novos (`tests/test_engine_grid.py`).

## Decisões já tomadas (não revisitar sem motivo)

- **Exchange:** dYdX v4 (não Hyperliquid) — min notional $1 vs $3, maker fee 33% mais barato
- **Pool:** WETH/USDC 0,05% (não ARB/ETH cross-pair) — mais simples pra validar tese
- **Grid sizing:** densidade máxima — cada ordem = `min_notional` da exchange (~$3 ETH-USD)
- **Single concurrent operation** (uma operação ativa por vez)
- **Auto-defenses:** **NÃO IMPLEMENTAR** auto-deleverage; só auto-emergency-close em margem crítica (decisão Phase 1.2: usuário NÃO QUER essa mecânica por enquanto, só alerts)
- **Threshold semantics:** A grade É a predição (replica matemática da curva LP). `threshold_aggressive` é safety net pra falhas (bot offline, exchange congestion, price gaps), NÃO tuning estratégico. Em operação saudável, drift é <0.5% e nunca dispara. Default 1% (apertado, coerente com modelo preditivo).
- **Hedge ratio default:** 1.0 (full hedge), pode ajustar no UI
- **Capital de validação:** $300 LP + $113-150 dYdX margin = ~$430 total
- **Config sensível** (mnemonic, private key) só via `.env`, nunca via UI
- **Senha auth dashboard:** `Wallace1` (no .env)

## Convenções do código

- TDD estrito: test failing → impl → test passing → commit
- Commits em format `feat(task-N):`, `fix(task-N):`, `test(task-N):`, `docs(task-N):`, `chore:`
- Cada feature em branch própria, merge-back ao master ao concluir
- Cada Phase tem seu spec → plan → execução → tag
- Tasks executadas via `superpowers:subagent-driven-development` (subagent fresco por task + reviews)
- Imports legacy (`engine/hedge.py`, `chains/evm.py`, `exchanges/hyperliquid.py`) — agendados pra deletion na Phase 1.2 Task 14

## Como retomar trabalho de onde parou

**Cenário 1: Começar Phase 1.3 (próxima fase planejada)**

```
1. Brainstormar com superpowers:brainstorming (Adaptive grid spacing + Observability)
2. Criar spec em docs/superpowers/specs/
3. Invocar superpowers:writing-plans
4. Branch: git checkout -b feature/<nome>
5. Invocar superpowers:subagent-driven-development
```

**Cenário 2: Outra fase / outro escopo**

```
1. Brainstormar com superpowers:brainstorming
2. Criar spec
3. Plan + execução com subagents
```

**Cenário 3: Bug ou ajuste num componente existente**

```
1. Ler o spec da fase relevante pra entender as decisões
2. Rodar testes existentes do módulo (tests/test_<module>.py)
3. TDD: test pra reproduzir o bug → fix → green
4. Commit + branch + merge se for grande, commit direto se for pequeno
```

## Stack rodando localmente

- **Preview server:** `python -m uvicorn app:app --host 127.0.0.1 --port 8000`
- **Auth:** admin / Wallace1 (basic auth — URL: `http://admin:Wallace1@127.0.0.1:8000/`)
- **Engine:** desligado por default (`START_ENGINE=false`). Pra ligar, setar `START_ENGINE=true` no `.env` antes de subir uvicorn
- **Tests:** `python -m pytest tests/ -v` (111 verdes após Phase 1.2 — rodar em batches no Windows pra evitar hang)
- **Caveat Windows:** rodar `pytest | tail` direto pode travar (pipe buffering). Use `pytest -v 2>&1 | tail -10` ou rode subsets

## Limitações conhecidas

- **Sem dados reais ainda:** `.env` tem placeholders fake (wallet 0x0001 etc). Pra rodar mainnet precisa preencher com dados verdadeiros (wallet Arbitrum + mnemonic dYdX + endereços do vault Beefy CLM)
- **Hyperliquid SDK install no Windows:** pode falhar em `ed25519-blake2b` se não tiver MSVC Build Tools. Workaround: tests mockam o SDK; execução real funciona na Linux (Fly.io)
- **dydx-v4-client versão:** PyPI max é 1.1.6 (nosso requirements diz `>=1.1,<2.0`)
- **LP fees attribution:** Phase 1.2 NÃO implementa listener de Beefy `Harvest` — `lp_fees_earned` fica em 0 até a gente adicionar isso (ficou como gap conhecido)
- **Engine config legacy:** `Settings` ainda tem campos `hyperliquid_api_key/secret/symbol` mesmo após o cleanup do adapter; nenhum código usa, mas pode-se tirar num cleanup pass futuro
- **Tests da suite completa às vezes hangam no Windows** quando rodada inteira; rodar em batches funciona
