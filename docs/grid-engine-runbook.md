# Grid Engine Runbook

## Para começar

1. Configurar `.env` (ver `.env.example`)
2. Depositar WETH/USDC em vault Beefy CLM (manualmente via app.beefy.com)
3. Depositar USDC na subaccount 0 da dYdX (~$130 pra $300 LP)
4. `python -m uvicorn app:app --host 0.0.0.0 --port 8000`
5. Abrir http://localhost:8000, login admin/<senha>
6. `START_ENGINE=true` no .env e restart pra ligar o bot

## Operação normal

- Bot roda loop de 1Hz: lê pool, calcula grade, ajusta ordens
- Você acompanha pelo dashboard: range, margin_ratio, fills, PnL
- Reconciler corre a cada 30s pra pegar drifts

## Sinais de alerta

- margin_ratio < 0.6 → alert WARNING (webhook)
- margin_ratio < 0.4 → alert URGENT
- margin_ratio < 0.2 → alert CRITICAL
- out_of_range = true por > 1h → checar se Beefy esta rebalanceando

## Troubleshooting

- "Reconciler: cancelled orphan X" → cloid orfão da exchange foi limpo, normal
- "Engine loop error" + traceback → bug; ver logs detalhados
- Margin caindo mas sem motivo aparente → checar funding rate atual
