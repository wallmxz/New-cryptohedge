# Fase 1.4 — Backtesting framework (MVP)

## Objetivo

Rodar a estratégia em dados históricos (preço ETH/USDC + APR Beefy + funding dYdX) pra medir:
- APR líquido projetado em períodos passados
- Max drawdown
- Distribuição de fills, range-outs, e custos

Plus: **cleanup de configs mortas** como T0 (pré-requisito).

**Fora de escopo do MVP:**
- Replay de cada swap individual da Uniswap (usaremos APR aproximado do Beefy)
- Replay de orderbook real da dYdX (assumimos fills determinísticos quando preço cruza nível)
- UI de backtest no dashboard (CLI primeiro)
- Sweep multi-parâmetro automático (futuro `--sweep`)
- On-chain execution simulation (Phase 2.0 trata)

## T0 — Cleanup de configs mortas

Pré-requisito antes do backtest pra isolar do legacy.

**Remover dos arquivos:**
- `config.py::Settings`: `max_exposure_pct`, `repost_depth`, `threshold_recovery`, `pool_deposited_usd`
- `config.py::from_env()`: linhas correspondentes
- `state.py::StateHub`: `max_exposure_pct`, `repost_depth`, `pool_deposited_usd` (manter `hedge_ratio`)
- `app.py::_load_persisted_config`: tuplas dos 3
- `web/routes.py::update_settings`: blocos de form-handling pros 4
- `web/routes.py::get_config`: linhas pros 3 (threshold_recovery, max_exposure_pct, repost_depth)
- `web/templates/partials/settings.html`: inputs de "Exposicao maxima", "Reposicionar no nivel", "Threshold de recovery", "Valor depositado na pool"
- `web/static/app.js::config`: keys correspondentes
- `tests/test_config.py`, `tests/test_state.py`: asserts removidos
- `.env.example`: env vars

**Refinar `threshold_aggressive`:**
- Mudar default de `0.05` para `0.01` (1%) — coerente com sistema preditivo onde drift normal é <0.5%
- Adicionar comentário no código clarificando: "safety net para falhas, não tuning estratégico"
- Atualizar tests pra usar 0.01

**Atualizar CLAUDE.md:**
Adicionar nota: "**A grade É a predição; threshold_aggressive é safety net pra falhas (bot offline, exchange congestion, price gaps), não tuning estratégico. Em operação saudável, nunca dispara.**"

## Modelo conceitual do backtest

### Premissas (MVP)

1. **Fills determinísticos**: toda ordem maker filla quando preço cruza seu nível de cima ou de baixo. Sem modelo de fila de orderbook ou competição com outros makers.
2. **LP fees aproximadas**: usa APR diário histórico do vault Beefy (extraído da API deles), aplicado proporcional à participação do usuário no vault. Trade real-life: `fee_per_day = APR_day * LP_value / 365`.
3. **Funding cobrado por hora**: extraído de `indexer.dydx.trade/v4/historicalFunding`, aplicado ao notional do short.
4. **Preço ETH/USD**: granularidade de **5 minutos** (uma observação de mid price a cada 5min). Trade-off: 5min cobre todos os ticks da Beefy sem ser custoso. Fonte: Coinbase API ou Binance klines.
5. **Beefy range histórico**: extraído via leitura on-chain dos eventos `Rebalance` da strategy, OU via API histórica do Beefy se disponível. Grid é reconstruída a cada rebalance.

### Pontos onde o real divergerá do simulado

- Latência de fills: assumimos zero — real-life tem 100-500ms
- Slippage de taker: assumimos 5 bps fixo, real varia com depth
- Funding pode ter spikes de minutos não capturados na granularidade horária
- Bot offline simulado como "sempre online" — real-life tem downtime

Esses fatores tornam o backtest **otimista**. Resultado real tipicamente 5-15% pior.

## Arquitetura

```
backtest/
  __init__.py          # marker (vazio)
  __main__.py          # CLI entry: python -m backtest --from ... --to ...
  data.py              # fetch + cache historical: prices, funding, beefy events
  cache.py             # SQLite local cache pra evitar re-fetch
  exchange_mock.py     # deterministic fill engine (substitui DydxAdapter)
  chain_mock.py        # replay range/L from Beefy timeline (substitui pool/beefy readers)
  simulator.py         # event-driven main loop, advances simulated time
  report.py            # PnL aggregation + stats output
```

### Componentes

**`data.py`** — busca histórica:
- `fetch_eth_prices(start, end, interval=300)` → list[(ts, price)]
- `fetch_dydx_funding(symbol, start, end)` → list[(ts, rate)]
- `fetch_beefy_apr_history(vault, start, end)` → list[(ts, apr_pct)]
- `fetch_beefy_range_events(vault, start, end)` → list[(ts, tick_lower, tick_upper, L)]

Todas com cache (SQLite local em `backtest_cache.db`).

**`exchange_mock.py`** — `MockExchangeAdapter` implementa `ExchangeAdapter`:
- Mantém em memória posição + lista de ordens abertas
- `place_long_term_order` registra ordem
- A cada tick simulado, `_process_fills(price)` checa orders cruzadas
- `get_position()`, `get_collateral()`, `subscribe_fills()` operam sobre estado interno
- Funding aplicado a cada hora simulada

**`chain_mock.py`** — `MockPoolReader` + `MockBeefyReader`:
- Stateful, avançam no tempo simulado
- `read_price()` retorna preço da timeline
- `read_position()` retorna range/L vigente naquele timestamp

**`simulator.py`** — `Simulator`:
- Inicializa `GridMakerEngine` com mocks injetados
- Loop principal: avança tempo em ticks de 5min ou na próxima `Rebalance` event
- Em cada tick: chama `engine._iterate()`, processa fills, aplica funding/LP fees
- Coleta métricas em listas (timestamps, PnL series, etc.)

**`report.py`** — gera relatório:
- APR anualizado, max drawdown, fill counts, range-outs
- Output texto + JSON (pra futura comparação)
- Sharpe ratio diário simples

### CLI

```bash
python -m backtest \
    --vault 0xVaultBeefy \
    --pool 0xPoolUniswap \
    --from 2024-01-01 \
    --to 2024-06-30 \
    --capital 300 \
    --hedge-ratio 1.0 \
    --margin 130
```

**Output exemplo:**
```
Backtest WETH/USDC | 2024-01-01 → 2024-06-30 (181 days)
Capital: $300 LP + $130 dYdX margin

Fills:        1240 maker, 12 taker
Range resets: 18 (Beefy)
Out-of-range: 4 events (3.2 hours total)

LP fees gross:    $187.40
Beefy perf fee:   -$18.74
Perp maker fees:  -$2.10
Funding (net):    +$8.20
Slippage (boot):  -$0.15
Net PnL:          $174.61 (58.2% APR annualized on $300 LP)
Max drawdown:     -$3.40 (-1.1%)
Sharpe (daily):   2.1
```

JSON parallel output em `--output result.json` (opcional).

## Tasks estimadas

```
T0 - Cleanup configs mortas + threshold default 1%

Data layer (4):
T1 - backtest/cache.py + backtest/data.py: fetch ETH price (Coinbase API)
T2 - backtest/data.py: fetch dYdX funding history (indexer)
T3 - backtest/data.py: fetch Beefy range events (on-chain via web3)
T4 - backtest/data.py: fetch Beefy APR history (their public API)

Simulator (5):
T5 - backtest/exchange_mock.py: deterministic fill engine
T6 - backtest/chain_mock.py: replay range/L
T7 - backtest/simulator.py: time-stepped main loop
T8 - Plug existing GridMakerEngine into mocked context
T9 - Operation lifecycle: simulated start at t0, stop at end

Reporting (2):
T10 - backtest/report.py: PnL aggregation + stats
T11 - CLI runner (backtest/__main__.py)

Tests (1):
T12 - tests/test_backtest.py: end-to-end com synthetic data (não chama APIs externas)
```

13 tasks total (incluindo T0).

## Riscos

| Risco | Mitigação |
|---|---|
| API do Beefy mudou ou não tem APR histórico | Fallback: usar média de APRs publicados, ou estimar pelo volume da pool |
| Coinbase rate-limit em pulls grandes | Cache local, paginar requests com sleep entre |
| Beefy `Rebalance` event com nome/signature diferente do esperado | T3 começa explorando event signature da strategy real |
| Latência simulada otimista demais | Documentar como "best case" no output, sugerir 10% buffer |
| dYdX indexer não tem funding antigo (ex: pré-2024) | Limitar período válido na CLI; documentar |
| Resultado da backtest dá over-confidence | Output explicita "best-case simulation" + lista premissas |

## Critérios de aceitação

1. `python -m backtest --vault X --pool Y --from <date> --to <date> --capital 300` produz relatório completo
2. Re-rodar mesmo comando usa cache (sem re-fetch APIs externas)
3. `tests/test_backtest.py` roda em <5s usando dados sintéticos (sem hit em APIs externas)
4. Reusa `engine/__init__.py::GridMakerEngine` diretamente — não duplica lógica
5. Custo total infra do backtest: 0 (CoinGecko/Coinbase/dYdX free tier; web3 RPC já configurado)

## Arquivos novos
- `backtest/__init__.py`
- `backtest/__main__.py`
- `backtest/data.py`
- `backtest/cache.py`
- `backtest/exchange_mock.py`
- `backtest/chain_mock.py`
- `backtest/simulator.py`
- `backtest/report.py`
- `tests/test_backtest.py`

## Arquivos modificados (T0 cleanup)
- `config.py`, `state.py`, `app.py`
- `web/routes.py`, `web/templates/partials/settings.html`, `web/static/app.js`
- `tests/test_config.py`, `tests/test_state.py`
- `.env.example`
- `CLAUDE.md` (nota sobre threshold)
