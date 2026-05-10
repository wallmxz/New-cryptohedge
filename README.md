# AutoMoney

Bot de hedge delta-neutro para Beefy CLM (Concentrated Liquidity Manager) na Arbitrum + dYdX v4 perpétuo.

Mantém uma grade densa de ordens maker no perp espelhando a curva de exposição da LP V3, capturando fees de market making enquanto se mantém delta-neutral em relação à pool.

## Stack

- Python 3.14, asyncio
- Starlette + Alpine.js + uPlot (dashboard)
- web3.py (Arbitrum RPC)
- dydx-v4-client (perp signing)
- aiosqlite (persistência)

## Como rodar localmente

1. Instalar deps: `pip install -r requirements.txt`
2. Copiar `.env.example` → `.env` e preencher
3. Subir o dashboard: `python -m uvicorn app:app --host 127.0.0.1 --port 8000`
4. Acessar `http://admin:Wallace1@127.0.0.1:8000/` (basic auth com creds do .env)
5. Pra ligar o engine real: `START_ENGINE=true` no `.env` + restart

Ver `docs/grid-engine-runbook.md` para detalhes de operação.

## Estado atual

- ✅ Phase 1.1 — Grid Maker Engine (tag `fase-1.1-completa`)
- ✅ Phase 1.2 — Operation Lifecycle (tag `fase-1.2-completa`)
- ✅ Phase 1.3 — Observability + Cleanup (tag `fase-1.3-completa`)
- ✅ Phase 1.4 — Backtesting Framework (tag `fase-1.4-completa`)
- ✅ Phase 2.0 — On-chain Execution Automatica (tag `fase-2.0-completa`)
- ✅ Pair Picker (tag `fase-pair-picker-completa`)

Ver `CLAUDE.md` (autoridade) e `docs/STATUS.md` para detalhes.

## Documentação

- `docs/STATUS.md` — estado atual do projeto
- `docs/grid-engine-runbook.md` — operações
- `docs/superpowers/specs/` — design docs por fase
- `docs/superpowers/plans/` — plans de implementação task-a-task
- `CLAUDE.md` — contexto pra sessões Claude Code

## Tests

```
python -m pytest tests/ -v
```

190 testes verdes (após cleanup pass; pre-cleanup eram 203 incluindo 13 do `test_orderbook.py` que foi deletado junto com `engine/orderbook.py`, módulo do design pre-grid).

Em Windows sem MSVC Build Tools, `dydx-v4-client` não compila — `tests/conftest.py` mocka o SDK em `sys.modules` pra a suite rodar mesmo assim. Em Linux/Fly.io o SDK instala normal e o stub não kicka.

## Deploy

Fly.io shared-cpu-1x. Ver `fly.toml`.

## Licença

Privado.
