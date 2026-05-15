# WORKING_ON

**Última atualização:** 2026-05-15 22:18 UTC — Bot LIVE com cloid 32-bit fix em master `435f02c`. Op #29 hedgeando. Grade preservada (não há mais cascata de orphan-cancels a cada 90s).

## Foco atual

**Master `435f02c` deployed em produção (DO Frankfurt).** Sessão 2026-05-15 noite entregou hot-fix do bug crítico que destruía a grade.

### Noite — Cloid 32-bit truncation fix (ROOT CAUSE do grid imbalance)

Spec: `docs/superpowers/specs/2026-05-15-cloid-32bit-truncation-fix-design.md`. Plan: `docs/superpowers/plans/2026-05-15-cloid-32bit-truncation-fix.md`.

**Root cause:** `_next_cloid` / `_next_cloid_for_leg` geravam cloids 64-bit (`run_id<<32 | leg<<24 | seq`). Mas `LighterAdapter.place_stop_market` faz `client_order_index = cloid_int & 0xFFFFFFFF` — **truncado pra 32 bits no wire**. `_local_grid` guardava 64-bit, `get_open_orders` retornava 32-bit, e a intersecção de sets sempre dava vazio:

- `orphans = live - local` = TODAS ordens vivas → canceladas cada 90s pelo `_safety_reconcile`
- `missing = local - live` = TODOS cloids locais → fake-fills processados, cancelando opposite extremes

Sintoma em prod (~02:30 UTC): grade ficava com 5 sells / 0 buys, 16 sells / 0 buys, cloids 32-bit do reconciler nuke (cloids como `2684354xxx`). `bot_grid_writes_total{reason="fill"} = 62` enquanto `bot_grid_stops_filled_total = 0`.

**Fix (5 commits em master):**
- `5d3de95` fix(engine): truncate `_next_cloid_for_leg` to 32 bits — layout `leg_byte(8) | seq(24)` = 32 bits
- `db95800` fix(engine): truncate `_next_cloid` to 32 bits — mesma transformação
- `96d8739` test(engine): regression guard for cloid set intersection
- `501b754` feat(engine): `cancel_all_stops` on engine.start when op active (gate em connected_exchange, try/except)
- `435f02c` chore(engine): remove dead `_run_id` field

**Pipeline:** brainstorm → spec → plan → 5 commits via subagent-driven (implementer + spec reviewer + code quality reviewer per block) + final cross-cutting review. Tests: 411 passed + 1 pre-existing pollution (test_settings_defaults). Branch fast-forwarded para master via `git push origin <branch>:master` porque main worktree estava locked.

**Estado live pós-deploy (verificado 2026-05-15 22:17 UTC):**

| | |
|---|---|
| Service | `active` |
| Op #29 | active, baseline pool $199.75 |
| Lighter live | 14 stops (5 sells + 9 buys, balanceado around p_now), todos cloids 32-bit `0xA000XXXX` |
| Position polls | ~10/sec ✅ |
| `bot_grid_stops_cancelled_total` | **0** (pré-fix crescia ~20/min) ✅ |
| `bot_grid_writes_total{reason="safety"}` | **3 em 5min** (pré-fix: 21 em ~30s) ✅ |
| `bot_grid_writes_total{reason="initial"}` | 16 ✅ |
| `bot_grid_writes_total{reason="fill"}` | 24 (alguns durante startup race entre `_post_initial_grid` lento + `_grid_event_loop` rápido — comportamento benigno) |
| `bot_grid_writes_total{reason="drift"}` | 1 (taker grande de 77 ARB no boot pra reabrir short do hedge — esperado dado posição flat) |

### Tarde — Event-driven grid reconciler (substituição completa do "self-healing")

Spec: `docs/superpowers/specs/2026-05-15-event-driven-grid-design.md`. Plan: `docs/superpowers/plans/2026-05-15-event-driven-grid.md`.

### Tarde — Event-driven grid reconciler (substituição completa do "self-healing")

Spec: `docs/superpowers/specs/2026-05-15-event-driven-grid-design.md`. Plan: `docs/superpowers/plans/2026-05-15-event-driven-grid.md`.

- **`_grid_event_loop` (100ms cadence)**: get_position; if changed, query open_orders, identify filled cloids via diff vs `_local_grid`, cancel opposite extreme + post 2 replacements (3 writes/fill)
- **`_safety_reconcile` (90s cadence)**: bootstrap path popula `_local_grid` pós-restart; steady-state cancela orphans + re-trigger fill detection pra missing cloids
- **`_post_initial_grid`**: placement inicial 8+8 populando `_local_grid` + `_last_known_position`
- **`_apply_fills_to_grid`**: algoritmo central, post-only-on-success (sem phantom cloids), `step<=0` guard
- **Drift correction** atualiza `_last_known_position` pós-taker pra grid loop não interpretar como fill
- **Métricas novas**: `bot_position_polls_total`, `bot_grid_writes_total{reason=fill|safety|drift|initial}`

Pipeline: brainstorm → spec → plan → 14 subagent-driven tasks (TDD + 2-stage review). Reviews pegaram 3 bugs: phantom cloid corruption (T3), `cancel_stop_order` signature mismatch (cloid_int vs order_index), step=0 edge case. Tests: 408 passing.

### Manhã — 3 fixes emergenciais

1. **Cloid 256-wraparound** — `_cloid_seq & 0xFF` só dava 256 únicos. Fix: 64-bit layout (32 run | 8 leg | 24 seq). Commit `1c7c126`.
2. **Alchemy free tier exausto** — 30M/30M CUs queimados. Trocamos pra Ankr Freemium.
3. **Lighter API key broken** — slot 2 quebrou. User regerou em slot 67.
**Estado live do bot (verificado 2026-05-15 05:14 UTC):**

| | |
|---|---|
| Service | `active` |
| Op #29 | active, baseline pool $199.75 |
| Errors críticos last 5min | **0/0/0/0** (UNIQUE / Lighter ratelimit / invalid sig / Alchemy) ✅ |
| Position polls | ~8.6/sec (target 10) ✅ |
| Lighter live | 16 stops (mas desequilibrado: 16 sells / 0 buys após cascata de buy fills com ARB caindo 2%) |
| Net PnL | -$0.09 (≈ neutro) |
| Pool $ | -$0.86 |
| Hedge PnL | +$0.74 |
| Funding | +$0.02 |

## Bugs remanescentes (post-fix)

1. ⚠️ **`cancel_all_stops` no engine.start usa symbol stale** — `self._settings.dydx_symbol_token0` é "ETH-USD" (placeholder do .env). O `pair_factory.refresh_vault_readers` muda pra "ARB-USD" SÓ depois de o engine.start completar. Sintoma: log mostra `cleared pre-existing stops for ETH-USD` quando deveria ser ARB-USD. Não destrói nada (não há ordens em ETH-USD), mas anula a feature em runs onde haja resíduo em ARB. **Fix:** mover cancel_all_stops pra DEPOIS do primeiro refresh do pair_factory, OU query lifecycle.active_symbol antes. Follow-up de baixa prioridade.
2. ⚠️ **`exchanges/lighter.py::cancel_all_stops` não é symbol-scoped** — cancela ALL account orders. Single-pair seguro hoje; cross-pair (Phase 3.x) vai precisar fix. Flag pra documentação.
3. ⚠️ **`engine/lifecycle.py::_next_cloid`** é função SEPARADA (não a do engine) com algoritmo diferente. Out-of-scope do hot-fix mas confunde. Consolidar quando convier.
4. ⚠️ **Cascading fill imbalance** — comportamento original observado pré-cloid-fix; agora resolved como sintoma do bug 64-bit. Reavaliar se ainda aparece em prod ao longo de horas.
5. ⚠️ **Race condition `_post_initial_grid` lento (1 ord/seg) vs `_grid_event_loop` rápido (100ms)** — durante o startup, alguns "event-driven cancel skipped" / "skip fill step=0" logs aparecem. Resultado em prod: 14/16 ordens vivas (2 "orphans" criadas via apply_fills com post falhando silenciosamente). Self-correcting via safety_reconcile mas log noisy.
6. ⚠️ **Hedge model status: warming_up / verify_diverging:100%** (predict mistura RAW V3 com HUMAN p_now)
7. ⚠️ **LP fees attribution = 0** (Beefy Harvest listener — user disse "não precisa")
8. ⚠️ **engine.pair_factory rebuild lifecycle a cada HTTP request** (log spam + possível aiohttp leak)
9. ⚠️ **Loop latency 2-3k ms total** (`Saúde do loop` no UI). Suspeita: `_grid_event_loop` em I/O concorrente roubando event loop dos outros tasks. Investigar.
10. ⚠️ **`bot_grid_orders_open` gauge não wired** ao `_local_grid` count (sempre 0). Não bloqueia.

## PRs / commits da sessão 2026-05-13/14 (todos em master)

```
208cbe0 feat(pnl): decompose hedge_pnl into realized + unrealized
d29352c fix(curve): uniform level sizes (anchor to aligned tick)
ee7d214 fix(pnl): pool_dollar uses baseline_pool_value_usd fallback
fa69d7a fix(pnl): single-leg breakdown reads funding_paid_token0
ac2ef0a fix(engine): clamp grid trigger to safety margin from market
567cd9b Merge: self-healing grid reconciliation
b03a8af feat(engine): self-healing grid reconciliation (replaces fill-callback trailing)
5db4919 Merge: skip reconciler under v2 + drift guards
9add729 fix(engine): skip reconciler under predictive_grid_v2 + drift guard pos=0
ca93a2b Merge feat/trailing-grid-and-drift
2bfcb5f feat(engine): trailing grid + 8+8 + drift correction + out-of-range
```

## Bugs resolvidos na sessão anterior (handoff.md tem detalhes)

1. Async fills SL_MARKET não disparavam `_fill_callback` → trocou trailing event-driven por self-healing reconciliation
2. Reconciler destrói grade ao tratá-la como orphans → skip sob v2
3. Drift correction shortava cego durante WS drop → skip se `pos is None`
4. Buffer empurra trigger past market → safety clamp (≤ p_now × 0.9999 sell, ≥ × 1.0001 buy)
5. Reconcile cap orphan-cancels precisa cloid namespace
6. Funding tracking single-leg lia `funding_paid` legacy → agora lê `funding_paid_token0`
7. Pool $ usava HODL (= IL natural) → agora prioriza baseline_deposit_usd > baseline_pool_value_usd > HODL
8. Uniform level sizes: prev_x ancora em tick aligned, não x_at_tick_now
9. Hedge PnL decomposto em realized + unrealized

## Bugs remanescentes (do handoff)

1. ⚠️ **NOVO — Reconciler UNIQUE constraint loop** (ver acima)
2. ⚠️ `hedge_model` status `warming_up` / `verify_diverging:100%` — predict mistura RAW V3 com HUMAN p_now
3. ⚠️ LP fees attribution = 0 (gap conhecido — Beefy Harvest listener; user disse "não precisa")
4. ⚠️ `engine.pair_factory` rebuild lifecycle a cada HTTP request — log spam + possível aiohttp session leak

## Próximo passo concreto

1. **Investigar e fixar reconciler UNIQUE constraint loop** (prioridade alta — spam grave + grade desfalcada)
   - Ler `engine/__init__.py::_maintain_grid` branch de reconcile post
   - Reproduzir local se possível, ou anexar mais log na prod
   - Fix: provavelmente `INSERT OR REPLACE` ou cleanup de cloid stale antes de re-post
2. Se quiser polir tracking: bug #2 hedge_model unit fix
3. LP fees real-time: bug #3 Beefy Harvest listener

## Deploy info (operacional)
- **IP produção:** `104.248.44.6`
- **Dashboard:** http://104.248.44.6:8000 (admin / Wallace1)
- **SSH:** `ssh -i C:\Users\Wallace\.ssh\id_ed25519 root@104.248.44.6`
- **Systemd unit:** `/etc/systemd/system/automoney.service`
- **Code:** `/opt/automoney/` (master `208cbe0`)
- **DB:** `/data/automoney.db` — op #29 ativa
- **Logs:** `/var/log/automoney.log`
- **Lighter account:** `724201`
- **Bot wallet:** `0x7cb0e1c2C9699E7023Ce13205A0C3E0E4320873c`
- **Lighter WAF:** só FRA1 passa (ASN 14061)

## Comandos úteis

```bash
# Estado completo
ssh -i ~/.ssh/id_ed25519 root@104.248.44.6 'curl -s -u admin:Wallace1 http://127.0.0.1:8000/operations/current | python3 -m json.tool'

# Lighter live orders
ssh -i ~/.ssh/id_ed25519 root@104.248.44.6 '/opt/automoney/venv/bin/python /tmp/sg.py'

# Reconciler errors count
ssh -i ~/.ssh/id_ed25519 root@104.248.44.6 'grep -c "UNIQUE constraint failed: grid_orders.cloid" /var/log/automoney.log'

# Metrics
ssh -i ~/.ssh/id_ed25519 root@104.248.44.6 'curl -s http://127.0.0.1:8000/metrics | grep "^bot_grid_"'
```

## Notas pra próxima sessão
- Pipeline brainstorm/spec/plan/subagent obrigatório (`memory/feedback_use_pipelines.md`)
- Subagent-driven default ao executar plans (`memory/feedback_subagent_driven_default.md`)
- Atualizar WORKING_ON e memory a cada mudança de foco (`memory/feedback_keep_state_fresh.md`)
- Compra no ask, vende no bid — sem buffer (`memory/feedback_no_price_buffer.md`)
- Verificar posição via fonte autoritativa antes de re-fire (`memory/feedback_verify_before_fire.md`)
- Não disparar trades por iniciativa — user clica botão (`memory/feedback_no_autonomous_trades.md`)
