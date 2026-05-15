# Event-driven grid reconciler

**Status:** Design approved 2026-05-15
**Author:** brainstorm session 2026-05-15 with user
**Replaces:** Self-healing reconciliation (commit `b03a8af`, 2026-05-13)

## Problema

O reconciler atual (`engine/__init__.py::_maintain_grid` + `_reconcile_grid`) recomputa a grade desejada a cada iter (~1Hz), faz diff vs `get_open_orders` da Lighter, e cancela/posta as diferenças. Resultado: toda vez que o preço se move 1 tick, o bot cancela 1 stop velha e posta 1 nova — mesmo que nenhum stop tenha sido realmente hit.

Empiricamente em 2026-05-15 com ARB caindo ~0.4% em 30 min:
- 39 `reconcile post sell` em 60s
- 8 `place_stop_market failed: 429` (Lighter rate limit hit)
- Lighter L1Address ratelimit: 40 writes/60s — saturado

A "self-healing reconciliation" foi introduzida em 2026-05-13 como fallback porque o `_fill_callback` não disparava pra ordens SL_MARKET. Mas a implementação ficou aggressive demais, tratando movimento de preço como "drift do estado desejado" em vez de "estado normal sem fills".

## Solução

Reverter pra modelo **event-driven**: a grade só muda quando a posição muda (= um stop foi hit). Movimento de preço sozinho não causa write nenhum.

### Algoritmo central

```
Estado mantido em memória:
  last_known_position: Position | None
  local_grid: dict[cloid, GridStop]   # snapshot dos 16 stops postados
  last_safety_reconcile_at: float

Loop (sleep 100ms entre iters):
  pos_now = get_position()             # 1 read

  # Safety net periódico (every 90s)
  if time.time() - last_safety_reconcile_at > 90:
    full_reconcile()                   # corrige drift entre local_grid e Lighter live
    last_safety_reconcile_at = now

  if pos_now == last_known_position:
    continue                           # ZERO writes. Próximo iter.

  # Fill detectado
  open_orders = get_open_orders()      # 1 read
  filled_cloids = local_grid.keys() - {o.cloid for o in open_orders}

  for cloid in sorted(filled_cloids, key=lambda c: distance_from_market(local_grid[c])):
    stop = local_grid[cloid]
    if stop.side == 'sell':
      # Sell foi hit → posição ficou mais short
      lowest_buy = min(local_grid.values() where side='buy', key=trigger_price)
      cancel(lowest_buy.cloid)                                  # WRITE 1
      post_buy_at(stop.trigger_price)                           # WRITE 2 (preenche slot perto do market)
      post_sell_at(top_sell.trigger_price + step)               # WRITE 3 (estende topo)
      del local_grid[cloid]
      del local_grid[lowest_buy.cloid]
      add new_buy + new_sell to local_grid
    else:  # buy filled
      # Buy foi hit → posição ficou menos short (mais long)
      highest_sell = max(local_grid.values() where side='sell', key=trigger_price)
      cancel(highest_sell.cloid)                                # WRITE 1
      post_sell_at(stop.trigger_price)                          # WRITE 2
      post_buy_at(bottom_buy.trigger_price - step)              # WRITE 3
      # análogo update local_grid

# Note: usar TRIGGER_PRICE do stop filled (não fill_price real) pra postar a contra-parte.
# Trigger é determinístico (a gente sabe), fill_price requer query extra e diferiria por slippage.
# Em SL_MARKET zk-rollup o slippage é minimal e essa simplificação é aceitável.

  last_known_position = pos_now
```

### Custo em chamadas API

| Cenário | Reads/min | Writes/min |
|---|---:|---:|
| 0 fills/min (preço estável) | 600 (1 get_position cada 100ms) | 0 |
| 1 fill/min | 600 + 1 get_open_orders | 3 |
| 10 fills/min (volatilidade alta) | 600 + 10 get_open_orders | 30 |

Lighter limits: writes=40/60s (L1Address), reads=ilimitado (ou alto, sem documentação clara). Confirmado empiricamente que 100% dos 429 hoje vieram de writes.

## Componentes

### 1. Engine state (novo)

Em `GridMakerEngine.__init__`:
```python
self._last_known_position: Position | None = None
self._local_grid: dict[int, GridStop] = {}  # cloid → GridStop(side, trigger_price, size)
self._last_safety_reconcile_at: float = 0.0
```

`GridStop` é um `@dataclass`:
```python
@dataclass
class GridStop:
    cloid: int
    side: Literal['sell', 'buy']
    trigger_price: float
    size: float
```

### 2. Replace `_maintain_grid`

Substituir o body atual por:
- 100ms loop em vez de 1s
- Position-diff trigger pra mutações
- Safety net 90s pra audit completo

Manter o que já existe sem mudança:
- Out-of-range guard (cancela tudo + idle)
- Range change detection (Beefy rebalance → cancel all + DB cleanup + rebuild)
- Initial grid placement (no `start_operation` flow)

### 3. New helper: `_apply_fills_to_grid(filled_cloids)`

Função pura(ish) que recebe os cloids filled e dispara os 3 writes por fill, atualizando `local_grid`. Loop interno trata multi-fill em ordem. Retorna count de writes pra logging/metrics.

### 4. New helper: `_safety_reconcile()`

Audit completo: query `get_open_orders`, comparar com `local_grid`.

**Behavior depende de se `local_grid` está vazio ou populated:**
- **Bootstrap path** (first call após restart, `local_grid` vazio): popular `local_grid` com os cloids encontrados na Lighter (look up trigger_price/side via `grid_orders` table do DB pra cada cloid). NÃO cancelar nada.
- **Steady-state path** (`local_grid` populated): corrigir discrepâncias bidirecionais.
  - Orders na Lighter que NÃO estão no `local_grid` → orphan, cancel (provável legacy de run anterior).
  - Cloids no `local_grid` que NÃO estão na Lighter → assumir filled. Re-trigger fill detection via `_apply_fills_to_grid` (mesmo path do iter normal). Idempotente: se já foi processado, `local_grid` já reflete e nada acontece.

### 5. Drift correction integration

`_aggressive_correct` continua exatamente como hoje. Diferença: depois que dispara um taker e ele confirma, **atualiza `_last_known_position`** com o resultado. Senão a próxima iter do `_maintain_grid` vai ver `pos_now != last_known_position`, achar que foi um stop fill, e disparar lógica de grid update — wrong.

Implementação: `_aggressive_correct` retorna a nova posição esperada após o taker, e o engine atualiza `_last_known_position` antes da próxima iter ler.

### 6. Initial grid placement

No `start_operation`:
1. Compute desired grid (8 sells + 8 buys nos preços calculados pela curva V3)
2. Post all 16 stops
3. Populate `local_grid` com os 16 GridStops (cloid + side + trigger)
4. Set `_last_known_position` com a posição imediatamente após o open
5. Set `_last_safety_reconcile_at` ao now

### 7. Edge cases tratados

- **Race fill+cancel**: se cancel chamar pra cloid que JUST filled (Lighter retorna `order_not_found`) → log info, não erro, segue. Safety net no próximo iter (90s) resolve qualquer inconsistência.
- **Multi-fill em 1 iter** (ARB voou): processa em ordem do mais perto pro mais longe do market. Cada fill = 3 writes. 5 fills = 15 writes. Ainda dentro de 40/60s exceto em movimentos extremos.
- **Position change sem fill**: drift correction é a fonte conhecida; tratada via `_last_known_position` update no próprio `_aggressive_correct`. Outros sources (close manual via UI, liquidação parcial) → safety net detecta no próximo audit (max 90s) e refaz o grid via initial placement path.
- **WS dropped mid=0**: position read pode retornar `None` ou `size=0` espuriamente; já existe guard em `_aggressive_correct` (skip se pos is None ou size==0 com target>0). Aplicar mesmo guard antes do fill detector.
- **Out-of-range**: skip o fill detector, cancela ordens, deixa em idle. Quando voltar pro range, refaz initial placement.

## Tests

Arquivo novo: `tests/test_engine_event_driven_grid.py`

- `test_no_position_change_no_writes` — 100 iters consecutivos sem mudar `pos_now` = 0 calls de `place_stop_market` ou `cancel_order`. Só leituras.
- `test_single_sell_fill_triggers_3_writes` — set `pos_now` mais short por 1 stop_size, mock `get_open_orders` retornando 15 (1 sell sumiu) → assert 1 cancel + 2 posts; valida cloid do cancel = lowest_buy; preço dos posts = `stop.trigger_price` e `top_sell + step`.
- `test_single_buy_fill_triggers_3_writes` — simétrico (cancela highest sell, posta sell + buy).
- `test_two_fills_same_iter_processed_in_order` — `pos_now` mudou em 2× stop_size; 2 sells sumiram do open_orders; assert 6 writes (2 cancels + 4 posts) na ordem correta.
- `test_drift_correction_doesnt_trigger_grid_response` — chamada manual de `_aggressive_correct`, depois iter do `_maintain_grid` vê posição mudada MAS `_last_known_position` já foi atualizado → assert 0 writes do grid.
- `test_safety_net_fires_every_90s` — mock `time.time()`, rodar 1000 iters em 95s simulados → assert exatamente 1 chamada de `_safety_reconcile`.
- `test_safety_net_detects_orphan_in_lighter` — `local_grid` tem 16 cloids, `get_open_orders` retorna 17 (1 orphan) → safety net cancela o orphan.
- `test_initial_placement_populates_local_grid` — após `start_operation`, `_local_grid` tem 16 entries com cloids matching os place calls.
- `test_out_of_range_skips_fill_detector` — preço fora do range V3, `pos_now != last_pos` → assert 0 writes do grid logic (cancela tudo separadamente).

## Migração

Branch: `feature/event-driven-grid`. Substitui `_maintain_grid`, `_reconcile_grid` no `engine/__init__.py`. Não toca `engine/reconciler.py` ainda (usado em fall-throughs antigos; pode ser deletado em PR de cleanup separado).

Compat com op #29 ativa em prod: ao deployar, o restart vai re-inicializar `_local_grid` chamando `_safety_reconcile()` (= equivalente ao initial placement) que vai ler open_orders existentes na Lighter, popular `local_grid` com os cloids encontrados, e seguir. Se grid em prod estiver desfalcado (15 stops vs 16 desired), safety net posta o que tá faltando — mas isso é o flow normal, não migration-specific.

## Métricas

Manter os existentes (`bot_grid_orders_open`, `bot_grid_rebuild_total`). Adicionar:
- `bot_position_polls_total` (counter, incrementa a cada `get_position` call)
- `bot_grid_writes_total{reason="fill"|"safety"|"drift"|"initial"}` (counter)
- `bot_grid_fill_detection_latency_seconds` (histogram, time entre `pos_now` change e write completion)

## Não-objetivos (explicitamente fora do escopo)

- WebSocket fill subscription (descartado: SL_MARKET WS instável; pode ser otimização futura quando Lighter consertar)
- Ajuste fino de step/buffer (continua usando o que `engine/curve.py` já calcula)
- Mudança no out-of-range behavior (continua: cancela tudo, idle, refaz ao voltar)
- Mudança no Beefy rebalance handler (continua: detecta range change, full rebuild)
- DB cleanup de rows antigos (separar em PR de manutenção, não bloqueia este)

## Open questions resolved

| | |
|---|---|
| Iter cadence | **100ms** (escolha do user; sweet spot entre latência e custo) |
| Safety net interval | **90s** |
| Multi-fill handling | **Process in order** (opção a) |
| Step | V3 tick + buffer (existente) |
| Out-of-range | Cancela tudo, idle, refaz ao voltar |
| WS fills | Não usado nesta versão |
