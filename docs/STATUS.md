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

## O que funciona hoje

- Dashboard Starlette + SSE + Alpine + uPlot, tema claro pt-BR
- Basic auth no dashboard (usuario `admin`, senha no `.env`)
- Estrutura completa: chain reader, 2 exchange adapters, hedge logic, PnL calc, DB SQLite
- Config via `.env` + overrides persistidos por UI (`hedge_ratio`, `max_exposure_pct`, `repost_depth`, `pool_deposited_usd`)
- Hedge logic com unidades consistentes (base units em vez de USD)
- Chart vazio quando nao ha historico, book vazio quando nao ha conexao
- 43 testes unitarios passando

## Bloqueadores para operacao real

Codigo esta pronto em estrutura mas **NAO opera com dinheiro real** sem os itens abaixo.

### 1. Credenciais e wallet
- `.env` atual tem wallet `0x0001`, chaves demo — nao conecta em lugar nenhum
- Precisa: wallet Arbitrum com os LP tokens, chave privada, key/mnemonic da exchange

### 2. Hyperliquid exchange precisa assinar ordens (EIP-712)
- [`exchanges/hyperliquid.py:137`](../exchanges/hyperliquid.py) `_post_action` so faz POST sem assinatura
- `/exchange` endpoint rejeita sem signature
- Fix: integrar `hyperliquid-python-sdk` OU implementar EIP-712 com `eth_account`
- `_asset_index` tambem esta hardcoded (BTC=0, ETH=1, ARB=2), precisa buscar do `/info` meta

### 3. dYdX v4 exchange nao assina nada
- [`exchanges/dydx.py:74-80`](../exchanges/dydx.py) retorna Order fake, so loga
- Precisa: `dydx-v4-client` SDK, assinar + transmitir transacao Cosmos

### 4. Funding nao e coletado
- `insert_funding` existe no DB mas nenhum codigo chama
- Precisa: polling periodico do endpoint de funding da exchange + chamada

### 5. WS sem reconexao
- Se WS cai, so loga warning — nao reconecta
- Fix: loop com backoff exponencial ao redor de `_ws_loop`

### 6. Safe Mode nunca e ativado
- Campo `hub.safe_mode` existe mas nada seta para True
- Triggers sugeridos: N falhas consecutivas da chain, WS down > 30s, exposicao > 2 * max_exposure_pct

### 7. Webhook de alertas nao dispara
- Config `alert_webhook_url` existe, nenhum codigo posta

### 8. Hot-reload de exchange/symbol via UI
- Form `/settings` salva no DB, mas o engine so le `settings.*` no `start()` — nao muda em runtime
- Fix: engine precisa reassinar WS/reiniciar adapter ao detectar mudanca

## Itens menores

- `total_fees_earned` nunca e populado (so `total_fees_paid`)
- `fee_currency` hardcoded USDC
- Tailwind CDN em producao (warning)
- `pool_token0/1_symbol` ainda nao expostos no UI (so via `.env`)

## Para testar operacao real

1. Preencher `.env` com credenciais verdadeiras
2. Implementar signing Hyperliquid (item #2)
3. Ligar engine com `START_ENGINE=true`
4. Monitorar logs e o dashboard
