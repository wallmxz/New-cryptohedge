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
- 🚧 Phase 1.2 — Operation Lifecycle (em planejamento)

Ver `docs/STATUS.md` para detalhes.

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

97 testes passando na Phase 1.1.

## Deploy

Fly.io shared-cpu-1x. Ver `fly.toml`.

## Licença

Privado.
