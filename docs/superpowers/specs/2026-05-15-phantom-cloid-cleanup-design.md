# Spec — Phantom cloid cleanup (Approach C: hybrid pop-only + post-verify)

**Data:** 2026-05-15 (segunda fase, noite)
**Status:** Approved (pending plan + impl)
**Owner:** wallmxz
**Prereq:** cloid 32-bit truncation fix (`docs/superpowers/specs/2026-05-15-cloid-32bit-truncation-fix-design.md`, master `4f098d9`)

## Contexto

Após o deploy do fix de cloid 32-bit (master `4f098d9` em ~22:14 UTC), o bot rodou ~14 min com a grade visivelmente vazando:

- **Sells congelados** em 0.1238-0.1243 (cloids `seq=4..8` dos 8 sells iniciais; seq=1..3 sumiram nos primeiros segundos).
- **Buys subindo a cada ~5 min** — 0.12560 → 0.12632 → 0.12716 → 0.12776+, cloids crescendo (584 → 610 → 634 → 670).
- **Posição NÃO mudava** (`Position: None` consistente após o taker de 77 ARB de drift_correction no startup).
- Métrica `bot_grid_writes_total{reason="fill"}` crescendo ~5/min com zero fills reais (`bot_grid_stops_filled_total = 0`).

## Root cause

**Phantom cloids viram fake-fills no `_safety_reconcile::missing`.**

Fluxo:

1. `_post_initial_grid` chama `place_stop_market(cloid_int=X)` 16 vezes (8 sells + 8 buys).
2. Para os triggers MAIS PRÓXIMOS DO MERCADO, Lighter **silently rejeita** (gate "trigger past market" ou rate-limit não levanta exception no SDK — retorna `err=None` mesmo sem persistir a ordem).
3. `_post_initial_grid` insere `_local_grid[X] = GridStop(...)` quando `place_stop_market` não levanta. Inclui os silently-rejected → **phantoms em `_local_grid`**.
4. 90s depois, `_safety_reconcile` (steady-state path):
   - `missing = local_cloids - live_cloids` = os phantoms (e quaisquer fills reais não capturados).
   - Chama `_apply_fills_to_grid(filled_cloids=missing, step=_estimate_grid_step(), ...)`.
5. Cada phantom é processado como "sell filled":
   - Cancela `lowest_buy` (do _local_grid).
   - Posta `new_buy` em `phantom.trigger_price` (que é onde o sell phantom estava ≈ próximo de p_now).
   - Posta `new_sell` em `top_sell.trigger_price + step` (extending upward).
6. As novas ordens TAMBÉM podem ser silently-rejected → mais phantoms → próximo ciclo (90s ou via `_grid_event_loop` no caso de WS drop) repete.
7. Net: a cada 90s, alguns buys "movem" para cima (porque o `new_buy` é posted no preço do phantom-sell que estava perto do mercado), os sells ficam intactos (porque o `new_sell` extending-upward é silently-rejected ou não substitui os existentes).

Padrão observado em prod (5 sells + 8 buys, sells fixos, buys subindo) bate **exatamente** com este modelo.

## Decisões de design

### D1. Source-of-truth: Lighter (cache: `_local_grid`)

Aprovado via brainstorming. Toda operação que altera o grid deve ser verificada contra `get_open_orders` ou refletida na posição. `_local_grid` é cache de aceleração, não verdade.

### D2. Fill signal: position-delta autoritativo

Aprovado via brainstorming. Mudança de posição = fill confirmado. Local-vs-live diff é só pra cleanup de staleness, NÃO pra fill processing.

### D3. `_safety_reconcile::missing` → pop-only

`_safety_reconcile` deixa de chamar `_apply_fills_to_grid` no branch missing. Substitui pela simples remoção dos cloids missing de `_local_grid`.

Justificativa:
- Phantoms são limpos sem disparar reposições.
- Fills reais não detectados pelo `_grid_event_loop` (caso WS drop fizer position oscillate) são re-detectáveis na próxima iteração de event_loop quando a posição reportada estabilizar — `_grid_event_loop` já compara contra `_last_known_position` e processa via `local - live` no momento da mudança.
- Trade-off: se um fill real cair entre dois ticks de event_loop E coincidir com WS drop nos próximos 90s+, ele pode ser perdido. Mitigação: position-delta em event_loop continua sendo a fonte primária; verify_fill no place_long_term_order path (já existente) cobre takers.

### D4. `_post_initial_grid` post-verify

Após postar todos os 16 stops, esperar 500ms e chamar `get_open_orders`. Drop cloids do `_local_grid` que não aparecem em live (= silently-rejected). Best-effort: falha de network loga warning e prossegue (próximo `_safety_reconcile` repete a limpeza).

500ms é margem confortável pra Lighter persistir (testes empíricos mostram <100ms tipicamente). Não bloqueia o engine porque `_post_initial_grid` é fase inicial.

### D5. Escopo deliberadamente fora

- **`_apply_fills_to_grid` post-verify** — o mesmo padrão de silent-reject pode ocorrer em fills posteriores. Fix análogo (verify-after-batch) seria útil mas adiciona ~30 linhas. **Follow-up.** Reavaliar comportamento pós-deploy.
- **Position-delta-based fill detection no `_grid_event_loop`** (Approach A) — mais correto mas maior cirurgia. Reavaliar se o Approach C não estabilizar.
- **`cancel_all_stops` no engine.start usa symbol stale** — bug colateral conhecido (WORKING_ON #1 atual). Separado.
- **`exchanges/lighter.py::cancel_all_stops` não symbol-scoped** — separado.

## Componentes alterados

| Arquivo | Função | Mudança |
|---|---|---|
| `engine/__init__.py` | `_safety_reconcile` (linhas ~1789-1793 atual) | branch `missing` deixa de chamar `_apply_fills_to_grid`; substitui por pop loop |
| `engine/__init__.py` | `_post_initial_grid` (final da função, antes de setar `_last_known_position`) | adiciona bloco de verify (sleep 500ms + `get_open_orders` + drop phantoms) |

Sem mudança em `_grid_event_loop`, sem mudança em `_apply_fills_to_grid`, sem migração de DB.

## Data flow após o fix

### Cenário 1 — Startup com silent-rejections

1. `engine.start()` chama `_post_initial_grid` (via primeiro `_iterate`).
2. Loop posta 16 stops. Lighter silently-rejeita 3 (triggers muito próximos do mercado, eg seq=1,2,3 sells).
3. `_local_grid` tem 16 cloids.
4. `await asyncio.sleep(0.5)` + `get_open_orders` retorna 13 cloids.
5. `phantoms = {seq=1,2,3}` → pop de `_local_grid`. Log warning.
6. Engine prossegue com 13 cloids em `_local_grid` = 13 em live. Match perfeito.
7. `_safety_reconcile` 90s depois: `missing = {}`. Branch never fires.

### Cenário 2 — Fill real durante WS drop

1. Grid steady-state: 13 cloids = 13 live.
2. Price moves, cloid X (a sell) fills. Position grows 4.5 ARB short.
3. WS drops simultaneously. `_grid_event_loop` reads `pos_now = None`.
4. `_position_equal(None, Position(prev))` → False → triggers.
5. `live = get_open_orders` returns 12 (X gone). `filled = local - live = {X}`. Calls `_apply_fills_to_grid({X}, ...)`.
6. Processes X as filled. Posts replacements. Updates `_last_known_position = None`.
7. WS reconnects. Next iter: `pos_now = Position(real)`. `_position_equal(real, None)` → False → triggers again.
8. live recompute: includes new replacements. local-live diff = {}. No-op.
9. Eventually position stabilizes, event_loop short-circuits.

### Cenário 3 — Phantom in steady state (existing fills via _apply_fills_to_grid)

1. `_apply_fills_to_grid` posts new_buy + new_sell. Suppose Lighter silent-rejects new_sell.
2. `_local_grid[new_sell_cloid]` is **NOT** added (post-only-on-success was already implemented in T3).
3. Phantom NOT created. ✓

(Apenas o `_post_initial_grid` tinha o vazamento; o `_apply_fills_to_grid` já tem post-only-on-success.)

## Estratégia de testes (TDD)

Três testes novos em `tests/test_engine_event_driven_grid.py`:

1. **`test_safety_reconcile_missing_drops_phantoms_without_calling_apply_fills`** — populate `_local_grid` com 5 cloids, mock `get_open_orders` retornando 2 (3 missing). Mock `_apply_fills_to_grid` como AsyncMock. Call `_safety_reconcile()`. Assert: `_apply_fills_to_grid.assert_not_called()`. Assert: `_local_grid` keys == os 2 que estavam em live.

2. **`test_safety_reconcile_steady_state_logs_phantom_drop`** — same setup, mock logger. Assert log mentions "dropped N missing cloids".

3. **`test_post_initial_grid_drops_phantoms_after_verify`** — fixture mock que posta 16 stops via `_post_initial_grid` mas mock de `get_open_orders` retorna só 10 (6 phantoms). Assert: depois do `_post_initial_grid`, `_local_grid` tem 10 entries (não 16).

O teste preexistente `test_safety_reconcile_steady_state_detects_missing_as_fill` (que **valida o comportamento OLD**) precisa ser **REMOVIDO** (a fixture viola o novo invariante) ou refatorado pra testar `_grid_event_loop` em vez de `_safety_reconcile`.

## Deploy

1. Branch `fix/phantom-cloid-cleanup` (worktree atual).
2. Push direto pra master via `git push origin <branch>:master` (mesmo padrão do fix anterior, fast-forward).
3. SSH prod, git pull, restart automoney.
4. Verificação pós-deploy:
   - Log: `_post_initial_grid dropped N phantom cloids` (esperado N=2-4 baseado no padrão pré-fix). Sucesso.
   - Log: NO `_safety_reconcile cancelled orphan` em modo cascata. Permite uns poucos no startup race (event-driven cancel skipped).
   - Métricas após 5 min:
     - `bot_grid_writes_total{reason="safety"}` cresce ≤ 5 nas primeiras 5 min (bootstrap + uma ou duas iterações de cleanup).
     - `bot_grid_writes_total{reason="fill"}` cresce SÓ se houver fill real (posição mudou). Estável em posição flat.
   - Lighter state: ≥ 10 stops vivos, balanceados ao redor do mercado, **sem drift sistemático over 5+ min**.

## Rollback

`git revert <merge>` em master, restart. Estado anterior: fix-1 (cloid 32-bit) presente, fix-2 (este) revertido. Bot funciona com sangria a 5 writes/min — não fatal, mas degrada grid over hours.

## Riscos & mitigações

| Risco | Probabilidade | Mitigação |
|---|---|---|
| Fix-1 quebrou outra coisa não relacionada que causa cascata. Investigação foi rasa. | Média | Test 3 reproduz exatamente o cenário com 16 posts + N silent-rejected. Se o test passa em local mas comportamento permanece em prod, descobrimos outro caminho de cascata. |
| `_safety_reconcile` deixando de processar real fills causa hedge drift | Baixa | Event_loop é o detector primário; ele já trata fills em qualquer position-change. Drift_correction taker (já existe) compensa qualquer divergência grande (>1$ delta). |
| 500ms wait no startup blocks engine | Sem impacto | startup é one-time; bot demora ~12s pra postar 16 stops já. +500ms é trivial. |
| Verify de `get_open_orders` falha (network) | Baixa | try/except + warn; próximo `_safety_reconcile` (90s) pop phantoms via D3. |

## Aprovação

Brainstorm: D1 (Lighter source-of-truth), D2 (position-delta authoritative), Approach C — todos aprovados via AskUserQuestion 2026-05-15.
