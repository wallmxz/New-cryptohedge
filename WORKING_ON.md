# WORKING_ON

**Última atualização:** 2026-05-13 — Predictive Grid v2 implementado, aguardando smoke em sandbox

## Foco atual

**Branch `feature/predictive-grid-stops`** — Predictive Grid Hedge v2 implementado completamente em Phases A+B+C (16 commits + 2 cleanup). Próximo passo: smoke 24h em **sandbox** seguindo runbook (`docs/predictive-grid-v2-smoke-runbook.md`), depois cutover Phase D2 (flip default + remove legacy).

**Bot atual produção:** continua rodando em DO Frankfurt (`104.248.44.6:8000`, op #28 já foi fechada manualmente pelo user durante a sessão de design). Master segue intacto — predictive v2 está na branch isolada com feature flag `PREDICTIVE_GRID_V2=false` default.

## Spec + Plan

- Spec: `docs/superpowers/specs/2026-05-12-predictive-grid-v2-design.md`
- Plan: `docs/superpowers/plans/2026-05-12-predictive-grid-v2.md`
- Smoke runbook: `docs/predictive-grid-v2-smoke-runbook.md`

## O que foi feito na branch (resumo)

**Phase A — Math & Foundations:**
- `engine/curve.py::tick_to_human_price` (V3 tick → human price)
- `engine/curve.py::compute_grid_from_pool_ticks` (grade alinhada aos ticks V3 do pool, two-loop pattern)
- `engine/curve.py::GridLevel.trigger_price` field
- `engine/grid.py::_level_key` 4-tuple (distingue limit vs stop)
- `db.py` migration: `grid_orders.trigger_price`, `grid_orders.is_stop_order`
- `db.py::get_grid_order(cloid)` lookup
- `exchanges/lighter.py::place_stop_limit_order` (SDK `create_sl_limit_order`; limit=trigger, zero slip)
- `exchanges/lighter.py::cancel_stop_order` + `cancel_all_stops`

**Phase B — Engine Integration:**
- `config.py::Settings.predictive_grid_v2 = False` (default)
- `engine/__init__.py::_maintain_grid` event-driven rebuild (HedgeModel.cache source)
- `engine/__init__.py::_on_grid_fill` reposta próximo tick após fill (agora wired no `_on_fill` callback)
- Wire no `_iterate` atrás da flag

**Phase C — Telemetry + UI:**
- 9 métricas Prometheus em `engine/metrics.py`
- `state.py::StateHub.grid_health_metrics` dict
- `web/templates/partials/grid_health.html` dashboard card

**Phase D1 — Smoke runbook:**
- `docs/predictive-grid-v2-smoke-runbook.md` — 4 smokes, rollback, promotion criteria
- Documenta limitações conhecidas (C-2, C-3, I-1 do code review)

**Phase D2 — Cutover:** PENDENTE até smoke aprovar

## Próximo passo concreto

1. Provisionar droplet **SANDBOX** em FRA1 (separado do produção)
2. Deploy do branch `feature/predictive-grid-stops` na sandbox com `PREDICTIVE_GRID_V2=true`
3. Seguir Smokes 1-4 do runbook
4. Se passar: PR + merge em master, deploy produção com flag=true, observar 24h, então D2 cutover (flag default=true + remove legacy)

## Estado dos branches

- **`feature/predictive-grid-stops`** — 18 commits, 29 tests novos (364 passing total, 1 pré-existente unrelated)
- **`master`** — limpo (spec + plan já mergeados como `7a0118c` e `ea5d3d7`)

## Fixes aplicados no code review final

3 críticos foram corrigidos no commit `5104694`:
- **C-1:** `_on_grid_fill` wirei no `_on_fill` WS subscriber (era dead code)
- **I-2:** `int(log())` → `math.floor(log())` em 6 call sites (ticks negativos da ARB-USDC.e)
- **I-7:** instrument `grid_fill_latency_ms.observe()`
- **M-3:** runbook SQL `filled_at` → `fill_id IS NOT NULL`

Documentadas no runbook como limitações conhecidas (não bloqueiam smoke):
- **C-2:** `cancel_all_stops` é account-wide, não market-scoped
- **C-3:** `place_stop_limit_order` descarta `order_index`; cancel individual indisponível
- **I-1:** `lighter_price_decimals=5`, `lighter_size_decimals=1` hardcoded pra ARB-USD
- **I-6:** `grid_replication_error_pct` Gauge declarada mas nunca computada (dashboard sempre 0%)

Esses 4 ficam pra fix antes do cutover D2.

## Deploy info (operacional, continua válido)
- **IP produção:** `104.248.44.6`
- **Dashboard:** http://104.248.44.6:8000 (admin / Wallace1)
- **SSH:** `ssh -i C:\Users\Wallace\.ssh\id_ed25519 root@104.248.44.6`
- **Systemd unit:** `/etc/systemd/system/automoney.service`
- **Code:** `/opt/automoney/` (master)
- **DB:** `/data/automoney.db`
- **Logs:** `/var/log/automoney.log`
- **Lighter WAF:** só FRA1 passa (ASN 14061). Sandbox tem que ser FRA1 também.

## Notas pra próxima sessão
- Pipeline brainstorm/spec/plan/subagent obrigatório (`memory/feedback_use_pipelines.md`)
- Subagent-driven default ao executar plans (`memory/feedback_subagent_driven_default.md`)
- Atualizar WORKING_ON e memory a cada mudança de foco (`memory/feedback_keep_state_fresh.md`)
- Compra no ask, vende no bid — sem buffer (`memory/feedback_no_price_buffer.md`)
- Verificar posição via fonte autoritativa antes de re-fire (`memory/feedback_verify_before_fire.md`)
- Não disparar trades por iniciativa — user clica botão (`memory/feedback_no_autonomous_trades.md`)
