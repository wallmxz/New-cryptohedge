# Predictive Grid Hedge v2 — design

**Data:** 2026-05-12
**Status:** aprovado, pronto pra writing-plans

## 1. Contexto

### 1.1 O que tem hoje

Engine atual (`engine/__init__.py::_iterate`, linhas ~1133-1206) implementa **taker chase reativo**:

1. Lê chain → `my_amount0`, `my_amount1` (composição da posição Beefy)
2. `target = my_amount * hedge_ratio`
3. Lê posição efetiva na Lighter (WS-fused via `get_effective_position`)
4. `drift = target - current`
5. Se `|drift| × ref_price ≥ $0,50` → fire taker cross-spread (`place_long_term_order`)

`compute_target_grid` (curva V3) existe em `engine/curve.py` mas é usado **só pra preview no UI** (linha 216 do engine), não pra placement de ordens.

### 1.2 Por que é problema

Análise empírica da op #28 (cross-pair ARB/WETH, ~115h de operação) mostrou:

| Métrica | Valor |
|---|---|
| Fills totais | ~4.700 (ETH-USD + ARB-USD) |
| Notional negociado | ~$5.300 |
| Turnover vs capital ($50 LP) | 53× |
| Avg sell ARB | $0,14005 |
| Avg buy ARB | $0,14027 |
| **PnL realizado matched ARB** | **−$2,00** (vende baixo, compra alto) |
| IL natural | −$13,37 |
| Hedge cover | +$11,82 |
| Gap não coberto | −$1,55 |

O bot é estruturalmente um **chase taker**: quando preço cai, LP compra ARB, bot vende ARB no preço baixo. Quando preço sobe, LP vende ARB, bot compra ARB no preço alto. Cada fire paga half-spread (zero fee mas input-lag tax na Lighter), acumulando em milhares de fires.

### 1.3 O que muda

Substituir o chase taker por **grade de stop-limit orders pré-colocadas na Lighter**, alinhadas exatamente aos ticks do pool Uniswap V3 que o Beefy CLM espelha.

**Tese:** cada fill na Lighter passa a corresponder 1:1 a um tick-cross no pool. Não há mais chase — ordens ficam dormentes até o trigger ativar.

## 2. Decisões já tomadas

| Decisão | Valor | Razão |
|---|---|---|
| Pool | ARB/USDC.e Arbitrum | Single-leg (USDC.e stable), perp ARB-USD existe na Lighter com tick fino |
| Capital | $500 LP + collateral Lighter separado | User-defined |
| Bootstrap | Manual (user deposita) + `POST /operations/hedge-existing` | Evita risco de bootstrap automático com pool nova; rota já existe |
| Hedge perp | ARB-USD (market_id=50 na Lighter) | Tick=$0,00001, step_size=0,1 ARB, zero fee |
| Order type | `STOP_LOSS_LIMIT` com `limit_price = trigger_price` | Sem slippage, sem markup; trade-off é timing-gap em flash moves (absorvível) |
| Trigger reference | Mark price (8min EMA premium + CEX blend) — único disponível na Lighter | Manipulação-resistente; rastreia preço global em real-time |
| Densidade | 1 ordem por tick ativo do pool V3 (fee tier define spacing) | Réplica matemática exata da curva |
| `$0,50 min` | É **exposição máxima desejada**, não threshold | Grade dense, sem buffer |

## 3. Arquitetura

### 3.1 Data flow

```
Beefy CLM (Arbitrum on-chain)
  │
  ├── strategy.balances() ─────► my_amount0, my_amount1, tick_lower, tick_upper
  ├── pool.slot0() ────────────► sqrtPriceX96, tick_now
  └── (eventos: Harvest, Rebalance, Swap)
       ↓
   compute_grid_from_pool_ticks (engine/curve.py NEW)
       ↓
   target_grid: list[GridLevel]
       ↓
   GridManager.diff(current_open_orders, target_grid)
       ↓
   to_place / to_cancel
       ↓
   Lighter API:
     - create_sl_limit_order (place)
     - cancel_order (cancel)
       ↓
   Lighter matching engine
     - trigger: markPrice crosses trigger_price
     - convert to limit at price = trigger_price
     - fill exact OR rest in book
       ↓
   Bot WS: fill event ──► update local state, replace level
```

### 3.2 Componentes principais

| Componente | Arquivo | Responsabilidade |
|---|---|---|
| Grid math | `engine/curve.py` | `compute_grid_from_pool_ticks` (NEW) |
| Diff | `engine/grid.py` | `GridManager.diff` (already exists, minor changes) |
| Lighter wrapper | `exchanges/lighter.py` | `place_stop_limit_order`, `cancel_stop_order` (NEW) |
| Engine loop | `engine/__init__.py` | Replace `_maybe_rebalance_leg` with `_maintain_grid` (event-driven) |
| Lifecycle | `engine/lifecycle.py` | Teardown reusa atual (cancel-all + close-short + withdraw) |
| Persistence | `db.py` | `grid_orders` ganha `trigger_price`, `is_stop_order` cols |

## 4. Construção da grade

### 4.1 Algoritmo: `compute_grid_from_pool_ticks`

Assinatura:
```python
def compute_grid_from_pool_ticks(
    *,
    L: float,                    # liquidity da posição V3
    tick_lower: int,             # tick boundary inferior (do CLM)
    tick_upper: int,             # tick boundary superior (do CLM)
    tick_spacing: int,           # spacing do fee tier (10/60/200)
    tick_now: int,               # tick atual do pool
    decimals0: int,              # decimais do token0 (ARB = 18)
    decimals1: int,              # decimais do token1 (USDC.e = 6)
    hedge_ratio: float,          # ex: 0.99
    lighter_price_decimals: int, # tick price decimals do perp Lighter (5 pra ARB-USD)
) -> list[GridLevel]
```

Algoritmo (pseudocódigo, implementação fica no plan):
```
1. price_upper = tick_to_human_price(tick_upper, decimals0, decimals1)
2. ticks_in_range = [t for t in range(tick_lower, tick_upper + 1, tick_spacing) if t != tick_now]
3. prev_x = compute_x(L, tick_to_human_price(tick_lower, ...), price_upper)
4. para cada t em ticks_in_range (sorted ascending):
     price_human = tick_to_human_price(t, decimals0, decimals1)
     price_rounded = round(price_human, lighter_price_decimals)
     x_at_t = compute_x(L, price_human, price_upper)
     delta = abs(prev_x - x_at_t)
     size = round(delta * hedge_ratio, lighter_size_decimals)
     if size > 0 and price_rounded > 0:
         side = "buy" if t > tick_now else "sell"
         levels.append(GridLevel(price=price_rounded, size=size, side=side, target_short=x_at_t * hedge_ratio))
     prev_x = x_at_t
5. return levels
```

Helper `tick_to_human_price`:
```
raw = 1.0001 ** tick
# Token0 é o de address menor (ARB para ARB/USDC.e: ARB < USDC.e)
# raw é "token1 per token0" em unidades raw. Human-readable USDC.e per ARB:
human_price = raw * 10**(decimals_token0 - decimals_token1)
return human_price
```

Detalhes:
- Ajuste de decimais (`decimals_token0 - decimals_token1`) traduz a razão raw V3 pra USDC.e por ARB. Sinal correto depende de qual token é token0 (address menor). Implementação deve seguir a convenção de `chains/uniswap_executor.py` / pool reader existentes.
- `tick_spacing` vem do fee tier do pool: 500 (0,05%) → 10; 3000 (0,30%) → 60; 10000 (1,00%) → 200. Fixo por pool, não muda.
- Cada level tem size derivado **exato** do delta V3 entre ticks adjacentes — não há médias nem aproximações
- `compute_x` reusa função existente em `engine/curve.py`; espera price em unidade human-readable consistente com `price_upper`

### 4.2 Range — sempre derivado do estado live da Beefy

**Range NÃO é hardcoded nem estimado.** `tick_lower` e `tick_upper` vêm direto de `BeefyClmReader.read_position()` (`chains/beefy.py`), que chama `strategy.positionMain()` on-chain e retorna o range ativo daquele bloco.

Número de ordens na grade = `(tick_upper - tick_lower) / tick_spacing`. Calculado em runtime a partir do que a Beefy expõe — **nunca assumir um número fixo**.

**A Beefy reposiciona o range periodicamente** (CLM manager rebalanceia quando preço se afasta do centro, em harvest events, ou conforme política do strategy contract). Quando o range muda:
- `tick_lower` / `tick_upper` mudam (visível na próxima leitura de `positionMain()`)
- Níveis antigos da grade ficam fora do novo range ou faltam níveis no novo range
- **Bot DEVE detectar e rebuildar grade inteira** — esse é trigger 4 da Seção 6.1

Implicação operacional: chain read precisa não só ler composição (`amount0`, `amount1`) mas comparar `(tick_lower, tick_upper)` posted vs current. Diferente = rebuild imediato.

### 4.2 Onde fica o tick atual

A grade **não posta ordem no tick atual** (skip). É a posição "no mid"; não tem sentido um trigger ali.

Existe questão de design: e se preço fica numa banda muito perto de `tick_now`? Resposta: o próximo tick acima/abaixo é só `tick_spacing` ticks de distância (0,1% pra fee tier 0,05%). Ordens lá são suficientemente próximas pra cobrir.

## 5. Colocação na Lighter

### 5.1 Mapeamento `GridLevel → Lighter order`

Usar `SignerClient.create_sl_limit_order`:
```python
await client.create_sl_limit_order(
    market_index=50,              # ARB perp
    client_order_index=<cloid>,   # idempotency
    base_amount=<size_int>,       # size em base units × 10^size_decimals
    trigger_price=<price_int>,    # trigger × 10^price_decimals
    price=<price_int>,            # IGUAL ao trigger_price (exato)
    is_ask=(side == "sell"),      # True=sell, False=buy
    reduce_only=False,
)
```

Triggers da Lighter:
- BUY: `markPrice ≥ trigger`
- SELL: `markPrice ≤ trigger`

`base_amount` em raw units: `int(round(size * 10**size_decimals))`. Para ARB perp: `size_decimals=1`, então `1.5 ARB → 15`.

`trigger_price` e `price` em raw units: `int(round(price * 10**price_decimals))`. Para ARB perp: `price_decimals=5`, então `0.14582 → 14582`.

### 5.2 TIF e expiry

- TIF: implícito GTT (good-till-time) via `DEFAULT_28_DAY_ORDER_EXPIRY`
- Não usar IOC: ordens missed precisam ficar resting no book

### 5.3 Cancelamento

Usar `SignerClient.cancel_order(market_index, order_index)`. Bot mantém map `cloid → order_index` retornado da Lighter quando ordem foi criada.

## 6. Lifecycle (event-driven)

### 6.1 Trigger events pra rebuild

A grade é rebuildada (cancel + replace) quando **qualquer um destes acontece**:

1. **Ordem fillha**
   - WS push: fill event → bot remove level fillado, posta o próximo tick adjacente
   - Não rebuilda grade inteira; só inserir um novo level

2. **LP composição diverge da grade posted (com range constante)**
   - Bot lê chain a cada N segundos (5-30s, configurável)
   - Compara `current_my_amount0 × hedge_ratio` vs `sum(posted_grid.target_short)` na mesma geometria de range
   - Se `|diff| > tolerance` (1 tick de delta): rebuild grade inteira
   - Causa típica: Beefy harvest (shares aumentaram), drift composto

3. **Range da Beefy mudou** ⚠ **CRÍTICO — sempre cancel-all + rebuild-all**
   - Bot compara `(tick_lower, tick_upper)` retornado por `positionMain()` agora vs o que estava posted
   - **Qualquer mudança detectada → CANCEL TODAS as stop orders ativas no Lighter + REBUILD a grade inteira do zero com o novo range**
   - **NÃO fazer rebuild parcial.** Mesmo ticks que existem nos dois ranges (antigo e novo) precisam ser recolocados porque:
     - O `L` (liquidity) da posição V3 mudou quando Beefy reposicionou (Beefy reabre uma nova posição V3 com nova L; raramente a L é a mesma)
     - A `size` de cada level = `delta_token0(tick_n → tick_n+1) × hedge_ratio` e depende de L
     - Logo, **todos os sizes existentes estão errados após mudança de range**, mesmo nos ticks que coincidem
     - `tick_now` também tipicamente mudou → direção (buy/sell) em ticks de borda pode ter virado
   - Sequência operacional:
     1. Detectar diferença em `(tick_lower, tick_upper)` ou `L` 
     2. Chamar `cancel_all_orders` (Lighter SDK suporta) — ou batch cancel por cloid
     3. Aguardar confirmação (curto, deve ser sub-segundo no Lighter)
     4. Computar nova grade com novos parâmetros
     5. Postar todas as ordens novas
   - Causa típica: CLM manager rebalanceou (preço se afastou do centro, política do strategy, harvest event que reabre posição)
   - Detecção: na mesma chain read do trigger 2; comparar tick boundaries E `L` antes de comparar amounts
   - **Sem detecção desse evento, a grade fica fantasma:** níveis fora do novo range nunca fillham (mark não chega lá porque pool não opera lá), níveis dentro do novo range estão faltando ou com size errado → hedge sub-cobertura sustentada

4. **Preço sai de [tick_lower, tick_upper]**
   - CLM tem range bounded; quando preço sai, posição vira 100% um token
   - Bot cancela toda a grade (não há delta hedgeável fora do range)
   - Re-posta quando preço voltar pro range OU quando Beefy rebalancear pro novo range (trigger 3 cobre esse caso)

### 6.2 Frequência de chain read

Hoje: 1Hz fixo. No novo design:
- **Frequência baixa (5-30s) é suficiente** porque a grade fica dormente entre triggers
- Reduz pressão no RPC + custo

### 6.3 Bot offline

Native stops ficam no matching engine. Se bot cai, **ordens continuam disparando autonomamente**. Bot ao reiniciar:
- Lê ordens ativas via `OrderApi.account_active_orders`
- Reconcilia com state local (cloids, etc)
- Continua de onde parou

## 7. Mudanças de código (resumo)

| Arquivo | Mudança | Tamanho aprox |
|---|---|---|
| `engine/curve.py` | Adicionar `compute_grid_from_pool_ticks` | ~80 linhas |
| `engine/grid.py` | `GridManager.diff` aceita stop orders; `_level_key` inclui trigger_price | ~10 linhas |
| `exchanges/lighter.py` | Métodos `place_stop_limit_order`, `cancel_stop_order` | ~60 linhas |
| `engine/__init__.py` | Substituir `_maybe_rebalance_leg` por `_maintain_grid`; remover taker chase do iterate | ~150 linhas removidas, ~120 adicionadas |
| `db.py` | Migrations: `grid_orders` ganha `trigger_price REAL`, `is_stop_order INTEGER DEFAULT 0` | ~30 linhas |
| `engine/lifecycle.py` | Sem mudança grande; `teardown` cancel-all funciona pra stop orders também | (no change) |
| `web/static/app.js` + templates | Display: levels com trigger price, miss-rate, fill-latency | ~50 linhas |
| `state.py` | `StateHub` ganha `grid_health_metrics` (miss_rate, fill_latency) | ~20 linhas |
| `chains/beefy_clm.py` (ou similar) | Expor `tick_lower`, `tick_upper`, `tick_spacing` | ~30 linhas (provavelmente já parcialmente exposto) |

## 8. Telemetria

Adicionar contadores Prometheus + campos no `StateHub`:

| Métrica | Tipo | O que mede |
|---|---|---|
| `grid_stops_placed_total` | counter | Total de stop-limit orders postadas |
| `grid_stops_filled_total` | counter | Total de fills da grade |
| `grid_stops_cancelled_total` | counter | Cancelamentos por rebuild |
| `grid_fill_latency_ms` | histogram | Tempo entre trigger e fill (cenário B = miss-temporário) |
| `grid_replication_error_pct` | gauge | `\|sum(posted) - target\| / target` |
| `grid_rebuild_total{reason}` | counter | Rebuilds por motivo (fill, drift, range_change, range_exit) |
| `beefy_range_change_total` | counter | Quantas vezes a Beefy reposicionou o CLM (tick_lower/upper mudou) |
| `grid_levels_active` | gauge | Quantidade atual de stops ativos no Lighter (varia conforme range Beefy) |
| `mark_vs_pool_drift_bps` | gauge | `\|markPrice - poolPrice\|` em bps (informativo) |

**Critério de sucesso pra dia 1:**
- `grid_replication_error_pct < 2%` na maior parte do tempo
- `grid_fill_latency_ms p95 < 60s`
- Hedge PnL cobrindo ≥98% da IL natural na primeira semana

**Critério de "tem problema, revisitar":**
- `grid_replication_error_pct > 5%` sustentado
- `grid_fill_latency_ms p50 > 60s` (miss frequente, não temporário)
- LP fees < custo operacional → tese estrutural não está se pagando

## 9. Riscos & mitigações

| Risco | Probabilidade | Impacto | Mitigação |
|---|---|---|---|
| Lighter cap de open orders < ticks na grade | Média | Alto (grade incompleta) | Começar com 50-100 níveis, escalar; descobrir cap empiricamente |
| Stop-limit não aceita size mínima ($0,50) | Baixa | Médio | Testar com 1 ordem no início; se rejeitar, aumentar floor |
| Mark diverge muito do pool em momentos específicos | Média | Médio | Telemetria `mark_vs_pool_drift_bps` mede; se >50bps frequente, considerar hybrid bot-side |
| Lighter WS reconnect drop perde fill events | Média | Baixo | Reconciler já existe (`engine/reconciler.py`); roda periodicamente |
| Beefy CLM rebalanceia range silenciosamente | **Alta** | **Alto** | Trigger 3 da Seção 6.1: comparar `(tick_lower, tick_upper)` posted vs current em toda chain read; rebuild inteira ao detectar qualquer mudança. Adicionar telemetria `beefy_range_change_total` pra visibilidade. Sem isso, grade vira fantasma e hedge falha sustentadamente. |
| Stop orders convertidas em limits ficam pendentes indefinidamente | Baixa | Baixo | TIF 28 dias absorve; bot cancela manualmente se nível obsoleto |

## 10. Out of scope (não fazer agora)

- OCO/fallback stop-market (Approach 2 do brainstorm)
- Sliding window dinâmica (Approach 3)
- Bot-side trigger no bid/ask (alternativa pesada; depois se mark mostrar problema)
- Multi-chain abstraction (só Arbitrum por enquanto)
- Backtesting do novo design (validação será live; backtest engine não modela mark price)
- LP fees attribution (Beefy harvest listener) — gap pré-existente, outra fase
- UI/UX redesign — gap pré-existente, outra fase
- Multi-pool concurrent (uma op por vez ainda)

## 11. Test plan

### 11.1 Unit tests (TDD ao implementar)

| Módulo | Teste |
|---|---|
| `engine/curve.py::compute_grid_from_pool_ticks` | Range simétrico → grade simétrica; spacing respeitado; total delta == LP token0 |
| `engine/curve.py::compute_grid_from_pool_ticks` | Decimal adjustment correto pra ARB(18)/USDC.e(6) |
| `engine/grid.py::GridManager.diff` | Stop orders triggered diff'am corretamente vs regular limits |
| `exchanges/lighter.py::place_stop_limit_order` | Conversão price/size pra raw units com decimais corretos |
| `engine/__init__.py::_maintain_grid` | Rebuild on fill event; rebuild on composition drift; cancel on range exit |

### 11.2 Integration test (com mock Lighter)

- Mock Lighter responde a place_stop, simula trigger via mark price simulada
- Grade construída, ordens postadas, fills simuladas, rebuild executado
- Verificar: total fills × size == LP composition delta (replicação correta)

### 11.3 Live validation (depois do merge)

- Branch isolada, deploy num DO sandbox separado (não na produção atual)
- Op de teste com $50-100 capital primeiro
- 24h de smoke: verificar telemetria, replication error, latency
- Se ok, scale up pra $500 capital, op de produção

## 12. Rollout

1. **PR 1 (curva + grid math + lighter wrapper)**: branch `feature/predictive-grid-v2`, testes verdes, sem mexer no engine loop. Mergeable independentemente.
2. **PR 2 (engine loop replacement)**: substitui `_maybe_rebalance_leg` por `_maintain_grid`; mantém path legacy via feature flag (`PREDICTIVE_GRID_V2=true`).
3. **PR 3 (telemetria + UI)**: adiciona métricas Prometheus + display no dashboard.
4. **Smoke test em sandbox** (não merge ainda no master de produção).
5. **PR 4 (cutover)**: remove path legacy, ativa v2 default. Só depois de smoke ok.

## 13. Critérios de aceitação do design

- [ ] Função `compute_grid_from_pool_ticks` implementada + testada
- [ ] Lighter wrapper `place_stop_limit_order` postando stops reais (smoke)
- [ ] `_maintain_grid` substitui `_maybe_rebalance_leg` no iterate
- [ ] Telemetria nova exposta em `/metrics` e dashboard
- [ ] Op de 24h em sandbox sem crash, replication_error_pct < 2% médio
- [ ] Documentação atualizada (CLAUDE.md, WORKING_ON.md)

---

## Apêndice A: referências

- Brainstorming session 2026-05-12 (PT-BR) — esta conversa
- Lighter SDK source: `/opt/automoney/venv/lib/python3.12/site-packages/lighter/`
- Lighter docs: `https://docs.lighter.xyz/llms-full.txt` (mark price trigger semantics)
- Phase 1.1 design (original grid maker, agora reativado em novo contexto): `docs/superpowers/specs/2026-04-27-grid-maker-engine-design.md`
- Curva V3 math (existente): `engine/curve.py`
- Lighter market params (probed live): ARB-USD market_id=50, price_decimals=5, size_decimals=1
