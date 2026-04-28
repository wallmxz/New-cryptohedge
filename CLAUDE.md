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

- ✅ **Phase 1.1 — Grid Maker Engine** (commit tag `fase-1.1-completa`, master)
  - 28 tasks, 97 testes passando
  - Engine que mantém grade de ordens maker no dYdX espelhando curva V3 da LP
  - Reconciler periódico, recovery após restart, margin monitor com webhook alerts
  - Spec: `docs/superpowers/specs/2026-04-27-grid-maker-engine-design.md`
  - Plan: `docs/superpowers/plans/2026-04-27-grid-maker-engine.md`

### Em andamento

- 🚧 **Phase 1.2 — Operation Lifecycle** (spec + plan escritos, **execução não iniciada**)
  - Adiciona ciclo de vida explícito de operação (start/stop), PnL detalhado por operação, cleanup de código legacy
  - Spec: `docs/superpowers/specs/2026-04-27-operation-lifecycle-design.md`
  - Plan: `docs/superpowers/plans/2026-04-27-operation-lifecycle.md`
  - **Próxima ação ao retomar:** invocar `superpowers:subagent-driven-development` no plan da Phase 1.2 e executar as 15 tasks

### Não iniciado

- Phase 1.3 — Refinamentos do engine (adaptive grid spacing + observability)
- Phase 1.4 — Backtesting framework
- Pré-produção — Testnet rehearsal antes de mainnet

## Decisões já tomadas (não revisitar sem motivo)

- **Exchange:** dYdX v4 (não Hyperliquid) — min notional $1 vs $3, maker fee 33% mais barato
- **Pool:** WETH/USDC 0,05% (não ARB/ETH cross-pair) — mais simples pra validar tese
- **Grid sizing:** densidade máxima — cada ordem = `min_notional` da exchange (~$3 ETH-USD)
- **Single concurrent operation** (uma operação ativa por vez)
- **Auto-defenses:** **NÃO IMPLEMENTAR** auto-deleverage; só auto-emergency-close em margem crítica (decisão Phase 1.2: usuário NÃO QUER essa mecânica por enquanto, só alerts)
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

**Cenário 1: Continuar Phase 1.2**

```
1. Ler docs/superpowers/plans/2026-04-27-operation-lifecycle.md (Phase 1.2 plan)
2. Verificar git log pra ver qual task já tem commit (caso execução tenha começado)
3. Criar branch: git checkout -b feature/operation-lifecycle (se ainda não existe)
4. Invocar skill: superpowers:subagent-driven-development
5. Dispatchar primeira task pendente
```

**Cenário 2: Phase 1.2 concluída, continuar pra outra fase**

```
1. Brainstormar a próxima fase (Phase 1.3 ou outro escopo) com superpowers:brainstorming
2. Criar spec em docs/superpowers/specs/
3. Invocar superpowers:writing-plans
4. Executar
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
- **Tests:** `python -m pytest tests/ -v` (97 verdes na Phase 1.1)
- **Caveat Windows:** rodar `pytest | tail` direto pode travar (pipe buffering). Use `pytest -v 2>&1 | tail -10` ou rode subsets

## Limitações conhecidas

- **Sem dados reais ainda:** `.env` tem placeholders fake (wallet 0x0001 etc). Pra rodar mainnet precisa preencher com dados verdadeiros (wallet Arbitrum + mnemonic dYdX + endereços do vault Beefy CLM)
- **Hyperliquid SDK install no Windows:** pode falhar em `ed25519-blake2b` se não tiver MSVC Build Tools. Workaround: tests mockam o SDK; execução real funciona na Linux (Fly.io)
- **dydx-v4-client versão:** PyPI max é 1.1.6 (nosso requirements diz `>=1.1,<2.0`)
- **LP fees attribution:** Phase 1.2 não implementa listener de Beefy `Harvest` — `lp_fees_earned` fica em 0 até a gente adicionar isso
- **Tests da suite completa às vezes hangam no Windows** quando rodada inteira; rodar em batches funciona
