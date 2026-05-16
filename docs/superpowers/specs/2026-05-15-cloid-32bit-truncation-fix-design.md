# Spec — Cloid 32-bit truncation fix

**Data:** 2026-05-15
**Status:** Approved (pending plan + impl)
**Owner:** wallmxz
**Prereq:** event-driven grid (master `c8edc64`), 256-wraparound fix (`1c7c126`)

## Contexto

Em produção (2026-05-15 ~02:30 UTC) a grade event-driven começou a apresentar:

- Cascata de cancelamentos `_safety_reconcile cancelled orphan cloid=2684354xxx` a cada 90s.
- Grade ficando desbalanceada (16 sells / 0 buys e similares) após reinícios.
- Métrica `bot_grid_stops_filled_total = 0` enquanto `bot_grid_writes_total{reason="fill"} = 62` — 62 ciclos de cancel+repost por "fills" fantasmas.
- Log `_safety_reconcile bootstrap: populated local_grid with 0 stops` mesmo com stops vivas na Lighter.
- User obs (screenshots): Lighter mostrando 3-5 ordens sobreviventes, todas do mesmo lado, agrupadas, com triggers irrealistas.

## Root cause

Discrepância de largura de cloid entre engine local e Lighter SDK.

| Camada | Valor de cloid |
|---|---|
| `_next_cloid_for_leg` (engine) | **64-bit**: `(run_id<<32) \| (leg_byte<<24) \| seq` (~7×10¹⁸) |
| `_local_grid[cloid]` keys | 64-bit (mesmo valor) |
| `place_stop_market` envia ao SDK | `cloid_int & 0xFFFFFFFF` — **32-bit truncado** |
| Lighter armazena `client_order_index` | 32-bit (o que recebeu) |
| `get_open_orders` retorna `cloid` | 32-bit `str(o.client_order_index)` |

Em `_safety_reconcile`:
```python
live_by_cloid = {int(o["cloid"]): o for o in live}   # 32-bit ints
local_cloids = set(self._local_grid.keys())          # 64-bit ints
orphans = live_cloids - local_cloids                  # = TODOS os lives
missing = local_cloids - live_cloids                  # = TODOS os locals
```

Os dois sets nunca intersectam. Toda iteração:
1. Cancela todas as ordens vivas (vê como órfãs).
2. Roda `_apply_fills_to_grid` em todas as locais (vê como filled).

`_grid_event_loop` sofre o mesmo problema sempre que `pos_now != _last_known_position`.

O bug foi introduzido pelo fix do 256-wraparound (manhã de 2026-05-15, commit `1c7c126`) que migrou `_cloid_seq` de 8-bit pra 24-bit e empacotou junto com `run_id` no high half de 64 bits — sem ajustar o site de comparação no reconciler.

## Decisões de design

### D1. Cloid uniforme em 32 bits

`_next_cloid` e `_next_cloid_for_leg` passam a retornar `valor & 0xFFFFFFFF`.

Layout final: `leg_byte (8) | seq (24)` = 32 bits. `run_id` é descartado (era inerte: SDK já truncava). Diagnosticidade preservada pelo leg_byte (`0xA0`/`0xA1`) no high byte.

### D2. Cancel-all no engine.start()

`engine.start()`, depois da fase de wiring mas antes de criar `_main_loop` e `_grid_event_loop` tasks, chama `await self._exchange.cancel_all_stops(symbol=self._settings.dydx_symbol_token0)` em try/except. É o mesmo símbolo usado por todo o resto do engine (out-of-range, range-change, reconciler) — atualmente single-pair, atualizado pelo pair_factory antes do engine subir.

Falha silenciosa (network) é logada como warning e o startup prossegue: `_safety_reconcile` bootstrap 90s depois adota qualquer remanescente — e agora os cloids batem.

### D3. Sem migration no DB

`grid_orders` no sqlite armazena `cloid` como string. Engine NÃO lê esse campo pra fazer matching contra Lighter (matching é via `_local_grid` em memória). Rows antigos ficam como histórico. Inserts novos terão cloids 32-bit-em-string.

### D4. Defensive guards permanecem

Os `& 0xFFFFFFFF` espalhados em `exchanges/lighter.py` (4 sites: `place_limit_order`, `cancel_order`, `place_stop_limit_order`, `place_stop_market`, e o lookup em `cancel_order_by_cloid` linha 1638) ficam como no-ops defensivos. Mantém o invariante "Lighter sempre recebe ≤ 32 bits" independente de quem chamar.

### D5. Escopo deliberadamente fora

- **Cascading fill imbalance** — esperado virar não-issue depois que o reconciler para de fake-fillar; reavaliar pós-deploy.
- **`initial=15` em vez de 16** — clamp de `safety_frac=0.0001` descarta níveis que colidem com o mercado; cosmético.
- **`hedge_model warming_up`** — escopo separado.
- **`engine.pair_factory` rebuild a cada HTTP** — escopo separado.

## Componentes alterados

| Arquivo | Função | Mudança |
|---|---|---|
| `engine/__init__.py` | `_next_cloid(domain)` (~825-832) | retornar `valor & 0xFFFFFFFF` |
| `engine/__init__.py` | `_next_cloid_for_leg(symbol)` (2058-2072) | retornar `valor & 0xFFFFFFFF` |
| `engine/__init__.py` | `start()` (~760) | cancel_all_stops antes dos loops |

Sem mudança de schema, sem mudança em `exchanges/lighter.py`, sem mudança no DB.

## Data flow após o fix

1. **engine.start():** cancela qualquer stop preexistente para o symbol ativo.
2. **`_main_loop` (1Hz)** detecta `posted_sig is None` → chama `_post_initial_grid`.
3. **`_post_initial_grid`** gera 16 cloids via `_next_cloid_for_leg` → cada cloid é 32-bit `(leg_byte<<24) | seq` → posta na Lighter → Lighter armazena exatamente o mesmo valor → `_local_grid[cloid]` populado com o mesmo valor.
4. **`_grid_event_loop` (100ms):** lê `pos_now`. Se mudou, query `get_open_orders` → `live_cloids` = 32-bit set. `filled = local_cloids - live_cloids` = só os que realmente sumiram = fills reais.
5. **`_safety_reconcile` (90s):** mesmo princípio. `orphans = live - local` = ordens que o engine não conhece (não deveria acontecer em estado saudável). `missing = local - live` = fills perdidos pelo event_loop.

## Estratégia de testes (TDD)

Quatro testes novos em `tests/test_engine_grid.py` (ou arquivo dedicado se preferir scope-isolation):

1. **`test_next_cloid_returns_32_bit`** — `_next_cloid(domain)` e `_next_cloid_for_leg(symbol)` retornam valores em `[0, 2**32)` para 100 chamadas consecutivas.

2. **`test_next_cloid_for_leg_encodes_leg_byte`** — para `symbol == settings.dydx_symbol_token0`, bits `[31:24]` do cloid == `0xA0`. Para outros symbols, == `0xA1`.

3. **`test_local_grid_cloid_matches_live_after_post`** — fixture: engine com mock exchange. `_post_initial_grid` posta 16 stops. Mock `get_open_orders` retorna os cloids exatos que o engine passou via `cloid_int`. Asserção: `set(engine._local_grid.keys()) == {int(o["cloid"]) for o in live}` (intersecção total).

4. **`test_engine_start_cancels_existing_stops`** — engine criado, `await engine.start()` chamado. Verifica que `exchange.cancel_all_stops(symbol=<correct>)` foi chamado exatamente 1x antes de tasks serem agendadas. Cobertura para o caminho startup-com-orfãs.

Tests rodam local em Windows (stub `dydx_v4_client`). Sem dependência de prod.

## Deploy

1. Branch `fix/cloid-32bit`. Push.
2. SSH prod: `cd /opt/automoney && git fetch && git checkout master && git pull` (após merge).
3. `systemctl restart automoney`.
4. Verificação pós-deploy:
   - `journalctl -u automoney --since "1 min ago" | grep -E "(populated|orphan|initial post)"` — esperar log `initial post sell ...` 8x + `initial post buy ...` 8x; **sem** "cancelled orphan" nos primeiros 90s.
   - `curl -s http://127.0.0.1:8000/metrics | grep bot_grid_writes_total` — `reason="initial"` deve subir pra ≥15-16; `reason="safety"` deve permanecer estável (sem cascata).
   - Lighter live orders via `/tmp/sg.py` — esperar 16 stops balanceadas (8 buy + 8 sell ao redor de p_now).

## Rollback

Se o fix tiver regressão, `git revert <merge-commit>` no master, `systemctl restart`. Estado anterior: cloids 64-bit + reconciler quebrado (pior, mas conhecido). Operação manual no Lighter via /tmp/sg.py disponível.

## Riscos & mitigações

| Risco | Probabilidade | Mitigação |
|---|---|---|
| `_cloid_seq` colide com 32-bit cloid de stop ainda viva de run anterior | Baixa após D2 (cancel-all garante zero ordens vivas no startup) | D2; reconciler bootstrap 90s pega o que escapou |
| `cancel_all_stops` falha por rate-limit no startup | Baixa (Lighter raramente rate-limita cancels) | try/except + warning; reconciler bootstrap recupera |
| Wraparound do `_cloid_seq` (24-bit = 16M ops) num único run | Quase zero (16M ≈ meses ao ritmo atual ~62 writes/h) | Restart resolve; observabilidade via métrica `bot_grid_writes_total` |
| Op #29 perder hedge entre stop atual e deploy | Já aconteceu (Lighter está flat); user ciente | User aprovou manter op #29; grid é re-posto no restart |

## Aprovação

- Brainstorm: aprovado pelo user (2026-05-15).
- Scope, approach A (truncar no source + cancel-all startup), DB sem migration, op #29 mantida: confirmados via AskUserQuestion.
