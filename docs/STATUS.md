# Status atual do AutoMoney

## Objetivo do sistema

Bot delta-neutro que balanceia uma posicao em **Beefy CLM (Concentrated Liquidity Manager)** na Arbitrum
com um **short em perpetuo** na Hyperliquid ou dYdX v4.

Fluxo:
1. Le a pool on-chain via `EVMChainReader` (poll 1s)
2. Calcula a quantidade do token0 que voce detem no vault (`calc_pool_position`)
3. Abre/ajusta short na perp ate `token0_pool * hedge_ratio` contratos
4. Usa ordens maker no topo do book; reposta se sair do nivel. Se exposicao passa `max_exposure_pct`, vira taker
5. Salva snapshots a cada 10s para o grafico e fills em cada execucao
6. Entra em Safe Mode se algo trava (nao implementado ainda)

## O que funciona após Fase 1.1

- Grid Maker Engine completo (engine/)
- Beefy CLM reader (chains/beefy.py)
- Uniswap V3 pool reader (chains/uniswap.py)
- dYdX v4 exchange adapter completo (exchanges/dydx.py)
- Reconciler periódico (engine/reconciler.py)
- Margin monitor + webhook alerts (engine/margin.py + web/alerts.py)
- Recovery on restart
- Dashboard com painel de grade + margin ratio + status badges

## Pré-requisitos pra rodar real

- Wallet Arbitrum com WETH/USDC depositados em vault Beefy CLM
- Mnemonic dYdX v4 com USDC depositado na subaccount 0
- .env preenchido com DYDX_MNEMONIC, DYDX_ADDRESS, CLM_VAULT_ADDRESS, CLM_POOL_ADDRESS

## Não implementado nesta fase (próximas fases)

- Operation Lifecycle UI (start/stop) — Fase 1.2
- PnL por operação com IL breakdown — Fase 1.2
- Auto-deleverage e auto-emergency-close — Fase 1.2
- Swap Uniswap automático — Fase 1.3
- Beefy deposit/withdraw automático — Fase 1.3
