# Fase 1.1 — Grid Maker Engine

## Objetivo

Substituir o "rebalance reativo" do engine atual por **market-making sintético do hedge**:
mantemos uma grade de ordens maker no perpétuo (Hyperliquid) que reflete a curva de
exposição da LP concentrada (Beefy CLM em cima de Uniswap V3). Conforme o preço se move,
ordens da grade fillam — e cada fill nos aproxima do hedge alvo, **com fee positivo de maker**
em vez de pagar taker.

Escopo desta sub-fase: o usuário deposita na Beefy manualmente. O bot só:
1. Lê a pool on-chain
2. Calcula a curva alvo de hedge
3. Mantém uma grade de ordens maker no Hyperliquid alinhada com a curva
4. Escala para taker se exposição > 5%
5. Recolocala a grade quando o Beefy rebalanceia o range

Operação lifecycle (botões iniciar/encerrar, PnL por operação) é a sub-fase 1.2.
Execução on-chain de swap+deposit é a sub-fase 1.3.

## Pré-requisitos

- Wallet Arbitrum: endereço + chave privada no `.env` (UI nunca toca na chave privada)
- Mnemonic dYdX v4: 24 palavras no `.env` (necessário para assinar txs Cosmos)
- Par escolhido (ex.: ARB/USDC) com:
  - Vault Beefy CLM ativo na Arbitrum
  - Pool Uniswap V3 underlying
  - Perpétuo correspondente listado em **dYdX v4** (ex.: `ARB-USD`)
- dYdX v4 signing implementado (via `dydx-v4-client` SDK)

### Por que dYdX v4 e não Hyperliquid

Decisão do usuário: dYdX v4 tem `min_notional` ~$1 (vs Hyperliquid $10). Grade fica 10× mais
densa, erro de discretização proporcionalmente menor.

**Sobre custo de placement:** dYdX v4 é Cosmos chain própria. Validadores mantêm orderbook
off-chain. Não há "gas por ordem" estilo Ethereum. Custo real = trading fees no fill.

Schedule oficial (verificado em docs.dydx.xyz, abr/2026):

| Tier | Volume 30d | Maker | Taker |
|---|---|---|---|
| 1 | < $1M | 1.0 bps | 5.0 bps |
| 2 | ≥ $1M | 1.0 bps | 4.5 bps |
| 3 | ≥ $5M | 0.5 bps | 4.0 bps |
| 4 | ≥ $25M | 0 bps | 3.5 bps |
| 5 | ≥ $50M | 0 bps | 3.0 bps |
| 6 | ≥ $100M | -0.7 bps (rebate) | 2.5 bps |
| 7 | ≥ $200M | -1.1 bps (rebate) | 2.5 bps |

Comparação relevante com Hyperliquid (tier 0): maker **1.0 bps dYdX vs 1.5 bps Hyperliquid**
(dYdX 33% mais barato no lado que mais usaremos), taker **5.0 bps dYdX vs 4.5 bps Hyperliquid**
(dYdX +0.5 bp, irrelevante porque taker é circuit-breaker raro).

**Modos de ordem disponíveis:**
- `SHORT_TERM`: in-memory, ~20 blocos (~30s) de validade, custo zero, exige re-submit periódico
- `LONG_TERM`: gravada on-chain, até 95 dias de validade, sem gas explícito documentado, place-and-forget

**Decisão:** Fase 1.1 usa `LONG_TERM` pelo modelo mais simples. Se placement lento ou congestão
afetar tracking, migrar pra `SHORT_TERM` com loop de re-submit em fase posterior.

Trade-offs vs Hyperliquid (a verificar empiricamente no plan):
- Latência maior (~1-2s/bloco Cosmos vs <100ms REST Hyperliquid) — afeta escalada agressiva, não a grade resting
- Spread potencialmente maior em pares menos líquidos (a comparar)
- SDK `dydx-v4-client` mais pesado que o da Hyperliquid

Hyperliquid fica reservado como **fallback opcional** — adapter mantido na codebase, escolhível
em settings, mas validação e tunings de Fase 1.x focam em dYdX.

## Modelo conceitual

### Curva de exposição

A LP concentrada do Beefy é, sob o capô, uma posição Uniswap V3 num range `[p_a, p_b]`. Para
qualquer preço `p` dentro do range, sua posição em token0 (ex.: ARB) é determinística:

```
x(p) = L · (√p_b − √p) / (√p · √p_b)
```

onde `L` é a liquidez da sua share no vault. `L` muda só quando você deposita/saca, ou quando
o Beefy rebalanceia o range. Lemos `p_a`, `p_b`, `L` on-chain a cada poll.

### Grade alvo (densidade máxima)

Cada ordem tem o **mínimo de notional aceito pelo perp** (ex.: $10 na Hyperliquid →
size mínimo = `$10 / preço_atual`, recomputado na construção da grade).

A grade cobre todo o range `[p_a, p_b]` com níveis em preços `p_0, p_1, ..., p_K` calculados
de forma **fechada** pela inversa de `x(p)`:

```
Δx = min_notional / preço_atual                # incremento fixo de token0 por ordem
x_i = x(preço_atual) − i · Δx                  # decrescente para níveis abaixo
                                                # crescente para níveis acima
p_i = 1 / (x_i / L + 1/√p_b)²                  # inversa exata
```

Ordens:
- Níveis **abaixo** do preço atual (preço caindo → mais token0 na pool → preciso shortear mais): **sell**, size `Δx · hedge_ratio`
- Níveis **acima** do preço atual (preço subindo → menos token0 na pool → cobrir parte): **buy**, size `Δx · hedge_ratio`

O resultado é uma rede densa que replica `x(p) · hedge_ratio` com erro de discretização ≤
`min_notional/2`. Para uma posição de $1000 com min_notional $10, a grade tem ~100 ordens —
praticamente contínua.

**Cap de segurança**: número total de ordens limitado por `max_open_orders_exchange` (verificar
limite real no SDK; default conservador 200). Se a grade ideal exceder o cap, o bot agrupa
níveis adjacentes (size = `2·Δx`, etc.) até caber.

**Reanchoring**: a grade é estática para um dado `(p_a, p_b, L)`. Movimento de preço NÃO
recalcula a grade — apenas dispara fills nos níveis apropriados. Recálculo só acontece em:
- Mudança de range (Beefy rebalance)
- Mudança de L (deposit/withdraw/compound)
- Drift de reconciliação > tolerância

### Escalada por exposição

A cada poll do estado:
```
exposure_pct = |hedge_atual − target_no_preco_atual| / token0_na_pool
```
- `exposure_pct ≤ 5%`: trabalha só com a grade maker, não emite ordem agressiva
- `exposure_pct > 5%`: define `side_to_correct` = `"sell"` se `current_short < expected_short`, senão `"buy"`. Cancela ordens da grade no lado `side_to_correct`. Manda agressiva (taker) com size = delta no BBO até voltar a `exposure_pct ≤ 2%`. Reconstrói grade só no próximo loop em que a condição estiver respeitada.

Os limites (5%, 2%) são configuráveis em `settings.html` (Trading tab).

### Triggers de reset da grade

A grade inteira é cancelada e recolocada quando:
1. Beefy rebalanceia o range (detecta via mudança de `p_a` ou `p_b` on-chain)
2. `L` muda (depósito/saque do usuário, ou compound do Beefy)
3. Preço fura o range (sai de `[p_a, p_b]`) — neste caso, a grade fica vazia até o range voltar
4. Configuração mudou (`hedge_ratio`, número de níveis, etc.)
5. Reconciliação detecta drift da exchange

## Arquitetura

```
┌──────────────────────────────────────────────────────────────┐
│                    GridMakerEngine                            │
├──────────────────────────────────────────────────────────────┤
│  PoolWatcher          ←  on-chain poll (1s)                  │
│  CurveCalculator      ←  x(p) e grade-alvo                    │
│  GridManager          ←  diff entre grade-atual e grade-alvo  │
│  ExchangeAdapter      ←  Hyperliquid (assinado, via SDK)     │
│  Reconciler           ←  cross-check periódico                │
└──────────────────────────────────────────────────────────────┘
```

### Componentes (módulos novos / refatorações)

| Módulo | Status | Função |
|---|---|---|
| `chains/beefy.py` (novo) | Substitui leitura genérica em `chains/evm.py` | Lê `range()`, `position()`, `balances()`, `totalSupply()` específicos do Beefy CLM Strategy |
| `chains/uniswap.py` (novo) | — | Lê `slot0()` (sqrt price + tick atual) do pool V3 |
| `engine/curve.py` (novo) | — | `compute_target_grid(L, p_a, p_b, p_now, hedge_ratio, min_notional, max_orders) → list[GridLevel]` (usa inversa fechada de x(p)) |
| `engine/grid.py` (novo) | — | `GridManager`: estado da grade + `diff(current_grid, target_grid) → [cancel, place]`. Place/cancel em batch quando suportado |
| `engine/__init__.py` | Refatorado | Vira `GridMakerEngine`: orquestra poll → curva → diff → ordens. Substitui o `_hedge_cycle` atual |
| `exchanges/dydx.py` | Reescrito do zero | Usa `dydx-v4-client` para `place/cancel/modify` assinados via mnemonic Cosmos. Suporte a long-term orders + WS de orderbook/fills |
| `exchanges/hyperliquid.py` | Mantido como fallback | Implementação ficará pronta mas opcional; testes apenas como sanity-check |
| `engine/hedge.py` | Mantido + extendido | `compute_aggressive_action(exposure_pct, threshold_aggressive, threshold_recover)` para o gate de escalada |
| `engine/orderbook.py` | Mantido | `calc_maker_price` usado em cada nível da grade pra ajustar pro tick correto |

### Estado em memória adicional (`StateHub`)

```python
@dataclass
class GridLevel:
    target_price: float
    target_size: float
    side: str  # "buy" or "sell"
    order_id: str | None  # cloid no Hyperliquid
    placed_at: float

current_grid: list[GridLevel]
range_lower: float  # p_a
range_upper: float  # p_b
liquidity_L: float
```

### Schema DB (alterações em `db.py`)

Tabela nova:
```sql
CREATE TABLE grid_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cloid TEXT UNIQUE NOT NULL,
    side TEXT NOT NULL,
    target_price REAL NOT NULL,
    size REAL NOT NULL,
    placed_at REAL NOT NULL,
    cancelled_at REAL,
    fill_id INTEGER REFERENCES fills(id)
);
```
Cloid (client order id) garante idempotência: bot cair e reiniciar → relê grid_orders, sincroniza com exchange via `info(open_orders)` da Hyperliquid.

## Fluxos

### Loop principal (1Hz)

```
1. PoolWatcher: read p_a, p_b, L, current_price
2. Se (p_a, p_b, L) mudou desde última construção → marcar GRID_DIRTY
3. Reconciler:
   a. fetch_position(perp) → current_short
   b. expected_short = x(current_price) · hedge_ratio
   c. exposure_pct = |current_short − expected_short| / x(current_price)
4. Se exposure_pct > 5%:
   GridManager.cancel_side(side_to_correct)
   ExchangeAdapter.aggressive(side_to_correct, delta, BBO)
   marcar GRID_DIRTY (vai recolocar quando exposure < 2%)
5. Senão (modo maker):
   Se GRID_DIRTY:
     target_grid = compute_target_grid(L, p_a, p_b, current_price, hedge_ratio, min_notional, max_orders)
     diff = GridManager.diff(current_grid, target_grid)
     ExchangeAdapter.batch_cancel(diff.to_cancel)
     ExchangeAdapter.batch_place(diff.to_place)  # cloid único por ordem
     limpar GRID_DIRTY
   # caso contrário, grade segue válida — só observamos
6. Atualizar StateHub
```

Loop não é 1Hz fixo — `asyncio.sleep(1)` entre iterações, com poll on-chain podendo levar
~200-500ms via RPC público. Em mercado parado, recomputo zero (steady state) — a grade fica
intacta por horas. Tráfego REST ao Hyperliquid só quando há fill (WS), drift detectado, ou
range muda.

### Tratamento de fill

WS `userFills`:
1. Match `oid` da exchange ↔ `cloid` na nossa tabela `grid_orders`
2. Marcar order como filled, gravar fill em `fills`
3. `StateHub.hedge_position` atualizado com novo size
4. Próximo loop principal vai naturalmente "preencher o buraco" daquele nível com nova ordem se ainda for parte da grade alvo

### Recovery após restart

```
on engine.start():
  1. Lê DB: grid_orders ativos (cancelled_at IS NULL, fill_id IS NULL)
  2. Lê Hyperliquid: open_orders por wallet
  3. Reconcilia:
     - cloid em DB e na exchange → manter
     - cloid em DB sem match → marcar cancelled (provavelmente foi cancelado por timeout do exchange)
     - ordem na exchange sem cloid em DB → cancelar (lixo)
  4. Continua loop principal — próximo tick recalcula grade alvo e diffa
```

## dYdX v4 signing

Decisão: **usar `dydx-v4-client` SDK oficial**. Cosmos signing via mnemonic — endereço derivado
não é o mesmo que o EVM; cada exchange tem sua wallet própria, financiada separadamente.

```python
from dydxv4client import NodeClient, IndexerClient, Wallet, Order
from dydxv4client.network import make_mainnet

network = make_mainnet()
wallet = await Wallet.from_mnemonic(node_client, settings.dydx_mnemonic, settings.dydx_address)
node_client = await NodeClient.connect(network.node)
indexer = IndexerClient(network.rest_indexer)

# Place long-term order (vive até preencher ou TTL)
order = Order(
    market="ARB-USD", side=Order.Side.SELL, type=Order.Type.LIMIT,
    size=Decimal("10"), price=Decimal("1.05"),
    time_in_force=Order.TimeInForce.GTT, good_til_block_time=now + 86400,
    client_id=cloid,
    order_flags=Order.Flags.LONG_TERM,
)
tx = await node_client.place_order(wallet, order)
```

Cancel + replace via `cancel_order(client_id)`, batch via `broadcast_messages([msg1, msg2, ...])`
(múltiplas ações no mesmo tx, se o módulo dYdX permitir).

WS de orderbook + fills via `IndexerSocket` da SDK (subscribir `v4_orderbook.<market>` e
`v4_subaccounts.<address>/<subaccount>`).

**Validações pendentes pro plan** (Context7 / docs oficial):
- Confirmar SDK suporta `LONG_TERM` orders e a API exata
- Custo de gas por ordem long-term na mainnet atual
- Limite de open orders por subaccount
- Suporte a multi-msg tx pra batch place/cancel

## UI

Mínimo viável (sub-fase 1.1):
- **Painel atual**: mantém PnL, pool, hedge, mas adiciona seção "Grade ativa" mostrando os N níveis com preço/size/status (open/filled/cancelled)
- **Configurações > Trading**: novos campos
  - `Niveis da grade` (default 10)
  - `Threshold aggressive` (default 0.05 = 5%)
  - `Threshold recovery` (default 0.02 = 2%)

## Testes

Unit:
- `engine/curve.py`: dado L, p_a, p_b, n → grade matemática correta (versus referência calculada à mão)
- `engine/grid.py`: dado current_grid e target_grid, diff produz cancel/place mínimos
- `engine/hedge.py`: gate de escalada (5% / 2%)

Integração (mock):
- Mock RPC + mock Hyperliquid SDK: simula movimento de preço e verifica que ordens são canceladas/placadas conforme esperado
- Reconciliação: discrepância entre DB e exchange → corretamente reconcilia

## Expected Performance Profile (Beefy CLM 0,05% fee tier)

### IL com hedge perfeito = 0

V3 LP em range tem IL fechado:
```
IL_v3(p) = L · (√p − √p_0)² / √p_0
```
Hedge dinâmico (short trackando x(p) continuamente) cancela esse termo no P&L total. Bot
recebe **só fees líquidas**, sem componente direcional.

Erro de discretização da grade ($1 min_notional): residual < 0.001% APR pra posições > $500.
Negligível.

### Estimativa de APR líquido

Para Beefy CLM 0,05% WETH/USDC em Arbitrum:

| Componente | Range estimado | Notas |
|---|---|---|
| LP fees gross APR | **+15% a +40%** | Depende de volume da pool e concentração do range |
| Beefy performance fee | **−10% das fees** | Tipicamente 9.5% sobre fees coletadas |
| **LP yield líquido** | **+13.5% a +36% APR** | Pré-hedge |
| Perp maker fees (dYdX, 1 bp) | **−0.05% a −0.5% APR** | Negligível, depende de turnover |
| Funding ARB perp (short) | **−5% a +15% APR** | Historicamente positivo. ETH: maior variância |
| Discretização hedge | **< 0.001% APR** | Negligível |
| **Net APR hedgeado (esperado)** | **+10% a +50%** | Banda ampla, depende muito do par/funding |

### Sensibilidade

Não vale a pena se:
- LP gross APR < 12% (sobra muito pouco depois de Beefy fee + funding ruim)
- Funding médio anual < −10% (perp short consome o yield)
- Pool 0,05% sem volume (sem fee gerada)

Vale revisar se há vault 0,3% no mesmo par com volume — taxa maior pode dar APR líquido melhor.

### Hedge ratio recomendação

- **100%** se funding histórico do perp ≥ 0 (caso comum em ARB short)
- **95%** como default conservador (buffer pra erros operacionais)
- Configurável via `hedge_ratio` em settings

### Frequência de rebalance

Bot decide automaticamente:
- **Micro-rebalance** (cada fill): 50-500/dia conforme volatilidade. Custo: 1 bp em maker fee
- **Macro-rebalance** (grade inteira): 1-10/dia em rebalance da Beefy. Custo: zero (placement free)
- **Total de custos de rebalance**: < $5/dia pra capital de $1k

### Riscos não-IL (não cobertos pela estratégia hedge)

| Risco | Impacto típico | Mitigação |
|---|---|---|
| Smart contract bug (Beefy/Uniswap) | Total loss | Diversificar capital, escolher vaults auditados |
| dYdX/exchange downtime | IL real durante janela | Bot vai pra safe mode, alerta usuário |
| Funding rate spike (>30% APR) | Yield zerado ou negativo temporário | Alerta + opção de fechar operação manualmente |
| Range gap (preço pula fora do range) | Pequena IL real (segundos de drift) | Grid recalcula assim que detecta |
| Beefy rebalance frequente | Slippage interno do swap | Métrica observável; vault troca se for excessivo |

## Margin sizing na exchange (dYdX v4)

Parâmetros típicos para ETH-USD:
- Initial Margin Fraction: 5%
- Maintenance Margin Fraction: 3%

**Para sobreviver `s` de stress adverse (price up para short):**
```
collateral ≥ N × (s + MM × (1+s))
```

Tabela para $300 de LP em WETH/USDC range ±10%:

| Stress | Collateral / Notional | $ na dYdX (peak short $278) |
|---|---|---|
| 10% | 13,3% | $37 |
| 20% | 23,6% | $66 |
| **27,5%** | **31,3%** | **$87** |
| 35% | 39,1% | $109 |
| 50% | 51,5% | $143 |

**Recomendação default:** `dYdX_collateral = 1,3 × N_peak × 31,3%` ≈ $113 por $300 de LP. Cobre 27,5% de stress + 30% buffer.

**Total capital alocado:** ~$300 (Beefy) + ~$130 (dYdX) = **~$430** para 35% stress confortavelmente coberto.

### Defesa em camadas durante operação não-supervisionada

A `margin_ratio` (collateral atual / collateral pra 27,5% stress) é monitorada a cada loop:

| Threshold | Ação | Implementação |
|---|---|---|
| < 80% | Alert info (webhook) | Fase 1.1 |
| < 60% | Alert WARNING (sugestão de ação) | Fase 1.1 |
| < 40% | Alert URGENT, prepara deleverage | Fase 1.1 |
| < 40% por > 5min | **Auto-deleverage**: close 50% do short via taker (~$0,50 fee no peak) | Fase 1.2 |
| < 20% | **Auto-emergency-close**: close 100% do short, cancela grade, vira flat. Custo ~$2 em fees | Fase 1.2 |

Auto top-up cross-platform (withdraw Beefy → bridge → deposit dYdX): **explicitamente fora da Fase 1.x**. Vai pra Fase 2 ou 3.

**Comportamento se você dorme e algo dá errado:**
- Cenário 1 (drift normal): alerts no webhook, você reage de manhã
- Cenário 2 (margem cai): bot deleverage 50%, manda alerta, você reage com tempo
- Cenário 3 (extremo): bot fecha tudo, vira flat (LP exposta, mas sem risco de liquidação na dYdX). Você acorda com short = 0 e pool sozinha. Custo ~$5-20 de IL durante janela "flat"

## Comportamento out-of-range (ambos os lados)

### Upper band (preço > p_b)

```
t=0   : preço cruza p_b. Grade já cobriu short até 0
        Pool: 100% USDC, 0 WETH
        Bot: short = 0
t=>0  : preço continua subindo
        Pool: continua 100% USDC (não há mais WETH pra vender)
        Bot: short = 0, sem mudanças
        PnL: zero (não há exposição)
```

### Lower band (preço < p_a)

```
t=0   : preço cruza p_a. Grade já adicionou short até max
        Pool: 100% WETH (~0,103 WETH no exemplo), 0 USDC
        Bot: short = x(p_a) WETH (boundary)
t=>0  : preço continua caindo
        Pool: continua 100% WETH (não há USDC pra trocar)
        Bot: short permanece no boundary
        PnL: zero net (pool perde valor + short ganha PnL = se cancelam)
```

### Algoritmo único pros dois casos

```python
on poll():
    p = current_price
    if p > p_b:
        target_short = 0
    elif p < p_a:
        target_short = x(p_a)  # boundary
    else:
        target_short = x(p) * hedge_ratio  # normal in-range

    if target_short != current_short:
        # closing or opening difference via aggressive (taker) — não é caso de grade
        adjust_short_aggressive(target_short - current_short)

    # cancel grid orders fora do range válido
    if p > p_b or p < p_a:
        cancel_all_grid_orders()
        wait_for_beefy_rebalance()
    else:
        rebuild_grid_if_dirty()
```

### Tempo até Beefy rebalancear

Depende do strategy do vault específico. Padrões observados:
- "Out-of-range trigger": rebalanceia logo que price < p_a ou > p_b. Tipicamente 1-30 min de janela
- "Drift-from-center trigger": rebalanceia quando price drifta X% do centro. Pode tardar
- "Time-based trigger": rebalanceia em intervalos fixos (cada 4h, 24h)

**Bot detecta on-chain via poll de `range()` ou `position()` na strategy a cada 1s.** Não precisa prever quando — só observa e reage.

## Stress test: 21 round-trips em 7 dias

Cenário: ETH oscila $3000 → $3300 → $2700 → $3000 três vezes por dia, sete dias.

### Mecânica por RT (V3 integrado)

| Leg | Δx | Avg price | Pool USDC flow |
|---|---|---|---|
| $3000→$3300 | -0,0476 WETH | $3146 | +$149,71 |
| $3300→$2700 | +0,1028 WETH | $2982 | −$306,75 |
| $2700→$3000 | -0,0552 WETH | $2841 | +$157,03 |

### Hedge PnL por RT

```
Bootstrap @ $3000: short 0,0476
L1 close @ avg $3146: realized = (3000 − 3146) × 0,0476 = −$6,95
L2 open @ avg $2982: position 0,1028 @ $2982
L3 close @ avg $2841: realized = (2982 − 2841) × 0,0552 = +$7,79
Position final: 0,0476 @ $2982. Mark @ $3000 unrealized = −$0,86
Total: realized +$0,84 + unrealized −$0,86 = $0  (delta-neutral ✓)
```

### 7 dias acumulados

| Item | 7 dias |
|---|---|
| Hedge (oscilação delta-neutral) | $0 |
| Maker fees (4.368 fills × $3 × 1bp) | −$1,31 |
| LP fees (60% APR × $300 × 7/365) | +$3,45 |
| Beefy perf fee (10% das LP fees) | −$0,35 |
| Funding (ETH ~0%) | $0 |
| **Net** | **+$1,79** |

**Por dia: +$0,26**. Estratégia **sobrevive a oscilação extrema** assumindo volume externo gerando LP fees.

### Margem durante stress test

Pico de short: 0,1028 WETH a $2700 = notional $278.

Pior caso (gap fora do range para $3300+ no peak da posição):
- Mark loss: (2982 - 3300) × 0,1028 = −$32,67
- MM: 3% × $339 = $10,17
- Collateral necessário: **$42,84**

Recomendado pra 27,5% stress: $113. **Sobra 2,6× durante o stress test.** ✓

### Risco identificado: velocidade

4.368 fills/7 dias = 0,007/s média. Sem problema.

Se um RT colapsa em 1h (flash event), 208 fills/h ≈ 3,5/min. Bot loop 1Hz pode atrasar 1-2s em replacement orders após fill. Em flash crash + bounce em <30s, perde sincronização da grade.

**Mitigações** (decisão de implementação fica pro plan):
- Loop 5Hz durante volatilidade alta detectada
- Pré-coloca orders dos dois lados (BUY e SELL) em cada nível, sempre mantém ambos resting
- Event-driven via WS de fills (substitui poll)

## IL residual da discretização

Mesmo com hedge dinâmico, o grid de $3 (min notional ETH) introduz erro residual:
- Drift máximo entre fills: ~1% do hedge alvo
- Em movimentos rápidos, pequeno gap entre fill e ajuste
- **Estimado: 0,01% APR de IL residual**. Negligível para capital ≥ $300.

Comparação prática (movimento $3000 → $2700):
- Pool: $277,79
- HODL referencia: $285,88 (IL natural = $8,09)
- Hedge PnL: +$15,45 (short 0,103 WETH, avg entry $2850)
- Net: $293,24 (faltando $6,76 vs HODL)
- **Esse $6,76 = IL natural + discretização + fees**. ~2,3% de drift no movimento +/- 10%

Para movimentos menores, drift << 1%.

## Server requirements

Bot precisa rodar 24/7 com baixa latência ao dYdX e Arbitrum RPC.

**Setup escolhido para Fase 1.1:**
- **Fly.io shared-cpu-1x** (US East): $1.94/mês
- **Alchemy free tier** para Arbitrum RPC: $0
- Total: ~$2/mês

**Specs:** 1 vCPU shared, 256MB RAM, 1MB/mês de disco SQLite. Bot consome ~10% CPU sustained, ~150-300MB RAM, ~2,6M chamadas RPC/mês (cabe no free tier de qualquer provider).

**Drag de infra no yield:**
- $300 capital → $2/mês = 8% drag → APY líquido ~47%
- $1.000 capital → 2% drag
- $3.000+ capital → <1% drag

`fly.toml` já existe na raiz, configuração mínima necessária.

## Não-objetivos desta sub-fase

- Botões iniciar/encerrar operação (1.2)
- PnL por operação (1.2)
- Swap automático na Uniswap (1.3)
- Deposit/withdraw automático na Beefy (1.3)
- Multi-pair (uma operação ativa por vez na 1.x)
- Reorgs de Arbitrum (improvável em 250ms)
- Webhook/alertas (1.3 ou Fase 3)

## Critérios de aceitação

1. Com depósito manual numa pool Beefy CLM ARB/USDC + saldo USDC na Hyperliquid:
   - Bot lê `p_a`, `p_b`, `L` da pool — valores batem com `position()` lido manualmente via cast/etherscan, tolerância 0.1% (arredondamento de uint256 → float)
   - Coloca grade de min_notional ($10) por nível cobrindo todo o range, respeitando max_open_orders
   - Erro de discretização da grade vs curva ideal: ≤ `min_notional / hedge_size` em qualquer preço
   - Quando preço move, ordens fillam progressivamente, hedge mantém-se com `exposure_pct < 1%` em **pelo menos 99% das medições do loop** durante 24h em movimento normal de mercado (mais apertado que o anterior porque a grade é densa)
   - Quando `exposure_pct` estoura 5% (gap rápido / WS down), agressiva é enviada e bot volta a `exposure_pct ≤ 2%` em < 5s
2. Reset da grade ao detectar rebalance do Beefy: teste com mock que altera `p_a`/`p_b` confirma cancelamento + re-place
3. Bot pode ser killed (SIGKILL) e reiniciado: ao subir, lê DB + open_orders, não dobra ordens nem deixa cloid órfão
4. 100% dos testes passando
5. Latência mediana do loop principal (poll → diff → place/cancel): < 500ms p50, < 1.5s p99 (dominado pela latência do RPC + Hyperliquid REST)

## Riscos

| Risco | Mitigação |
|---|---|
| dYdX v4 SDK incompatível com Python 3.14 | Validar no plan; fallback é Hyperliquid (adapter já feito) |
| TTL da long-term order esgotando (95 dias) durante operação muito longa | Renovar ordens automaticamente quando TTL < 24h |
| Beefy CLM Strategy ABI varia entre vaults | Suportar 2-3 ABIs comuns + log claro se vault não for reconhecido |
| Range muda muito rápido (rebalance frequente) → grade fica recolocando | Limitar frequência de reset (debounce 30s) |
| RPC Arbitrum lento → bot perde fills | Usar fallback RPC + `eth_subscribe newHeads` em vez de poll quando possível |
| Cloid colide entre runs | Cloid = `f"hb-{run_id}-{level_idx}-{seq}"`, sequência só sobe |
| Grade ideal excede max_open_orders | Algoritmo de agrupamento progressivo: começa com Δx = min_size, dobra Δx até caber |
| dYdX rate limit / multi-msg tx tamanho | Slice batch em chunks; medir empiricamente no plan |
| Fill parcial num nível | Exchange devolve qty residual no resting order; bot trata `fill` como decremento, deixa restante resting até preencher ou ser cancelado |
| Spread maior em dYdX vs Hyperliquid pra ARB | Comparar spreads no plan via WS de ambos antes de comprometer; escolher exchange por par no settings |

## Configuração que vai pro `.env` / UI

`.env` (não muda em runtime):
```
WALLET_ADDRESS=0x...
WALLET_PRIVATE_KEY=0x...
ARBITRUM_RPC_URL=...
ARBITRUM_RPC_FALLBACK=...
DYDX_MNEMONIC="word1 word2 ... word24"
DYDX_ADDRESS=dydx1...
DYDX_NETWORK=mainnet              # ou testnet
DYDX_SUBACCOUNT=0
```

UI (editável, persiste em DB):
- `active_exchange` (`dydx` default | `hyperliquid`)
- `clm_vault_address`
- `pool_token0_symbol` (ex.: ARB)
- `pool_token1_symbol` (ex.: USDC)
- `perp_symbol` (ex.: ARB-USD pra dYdX, ARB pra Hyperliquid)
- `hedge_ratio` (0–1)
- `max_open_orders` (cap, default 200; verifica limite real do exchange no setup)
- `threshold_aggressive` (0.01–0.20, default 0.05)
- `threshold_recovery` (0.005–0.10, default 0.02)
- `max_slippage_swap` (deixado pra 1.3)

Não-editáveis (lidos do exchange via SDK):
- `min_notional` por par (dYdX expõe via `IndexerClient.markets()`, Hyperliquid via `info(meta)`)
- `tick_size` por par
