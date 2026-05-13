# Predictive Grid v2 — smoke test runbook

**Versão:** 2026-05-13 — branch `feature/predictive-grid-stops`
**Spec:** [docs/superpowers/specs/2026-05-12-predictive-grid-v2-design.md](superpowers/specs/2026-05-12-predictive-grid-v2-design.md)
**Plan:** [docs/superpowers/plans/2026-05-12-predictive-grid-v2.md](superpowers/plans/2026-05-12-predictive-grid-v2.md)

---

## Por que existe esse runbook

Phase A-C implementam o predictive grid v2 (stop-limit orders pré-colocadas na Lighter alinhadas aos ticks do pool Uniswap V3). A feature flag `PREDICTIVE_GRID_V2` está OFF por default. **Antes de virar default ON em produção (Phase D2 cutover)**, precisamos validar em sandbox por 24h+ sem crashes nem regressões.

Este runbook é a checklist de smoke.

---

## Pré-requisitos

- [ ] DO droplet **SANDBOX** em FRA1 separado do produção (mesma região passa Lighter WAF — `memory/project_lighter_waf_datacenter.md`)
- [ ] `.env` completo com `PREDICTIVE_GRID_V2=true`
- [ ] Wallet com USDC.e Arbitrum + collateral USDC no Lighter (~$200 sandbox)
- [ ] Pair selecionado via pair-picker: **ARB/USDC.e Beefy CLM**
- [ ] Capital LP inicial: **$50-100** (smoke), depois escalar pra $500 (validação)
- [ ] HEAD do branch `feature/predictive-grid-stops` no servidor (HEAD `b715515` no momento desse documento)

---

## Smoke 1 — Boot + range detection (5-10 min)

**Objetivo:** verificar que o bot sobe limpo e o `_maintain_grid` posta a primeira grade.

- [ ] Deposit manual ARB/USDC.e no vault Beefy via UI Beefy
- [ ] `POST /operations/hedge-existing` (engine pega a posição existente sem refazer bootstrap)
- [ ] Logs: ver mensagem `predictive_grid_v2=true` + primeiro `_maintain_grid` call
- [ ] `/metrics`: 
  - `bot_grid_levels_active > 0` 
  - `bot_grid_stops_placed_total > 0`
  - `bot_grid_rebuild_total{reason="initial"} == 1`
- [ ] Lighter UI: ver N stop-limit orders postadas em ARB-USD (N depende de tick_spacing e range)
- [ ] Dashboard `/`: card "Grid Predictive v2" aparece com `levels_active > 0`

**Critério de pass:** todos os ✓ acima. Se algum item falhar → debug antes de prosseguir.

---

## Smoke 2 — Fill event (15-60 min, depende de mercado)

**Objetivo:** confirmar que `_on_grid_fill` dispara quando mark crossa um trigger e que o próximo level é postado.

- [ ] Esperar mark price cruzar pelo menos 1 tick do pool
- [ ] Logs: ver `_on_grid_fill` log com `cloid=...`
- [ ] `/metrics`:
  - `bot_grid_stops_filled_total` incrementou
  - `bot_grid_stops_placed_total` incrementou de novo (próximo tick reposto)
  - `bot_grid_rebuild_total{reason="fill"}` incrementou
- [ ] DB query: `SELECT * FROM grid_orders WHERE cloid = <filled_cloid>` mostra `fill_id IS NOT NULL`
- [ ] DB query: `SELECT * FROM grid_orders WHERE is_stop_order=1 AND fill_id IS NULL AND cancelled_at IS NULL` mostra os stops ativos atualmente
- [ ] Dashboard `/`: `stops_filled_total` no card aumentou

**Critério de pass:** ≥3 fills observados sem crash, fill_latency mediana < 30s.

---

## Smoke 3 — Range change da Beefy (depende de Beefy rebalancear, pode levar horas-dias)

**Objetivo:** validar trigger 3 do spec (range change → cancel-all + rebuild).

- [ ] Monitorar `bot_beefy_range_change_total` ao longo do tempo
- [ ] Quando incrementar:
  - Logs: ver `cancel_all_stops` chamado
  - Logs: ver new `_maintain_grid` rebuild call
  - `/metrics`: `bot_grid_stops_cancelled_total` saltou
  - `/metrics`: `bot_grid_rebuild_total{reason="range_change"}` incrementou
- [ ] Lighter UI: orders antigas canceladas (count cai a 0), novas postadas no novo range
- [ ] Dashboard: `range_changes_total` aumentou; `last_rebuild_reason="range_change"`

**Critério de pass:** range change detected + rebuild completed sem orders fantasma sobrando do range antigo.

**Se Beefy não rebalancear em 24h:** ok, esse smoke fica pendente; passa pros próximos.

---

## Smoke 4 — Sustentação 24h

**Objetivo:** verificar estabilidade e replicação.

- [ ] Bot uptime: `systemctl status automoney.service` mostra `active (running)` desde antes do início do smoke
- [ ] Sem crashes: `grep -i 'traceback\|crashed\|panic' /var/log/automoney.log` retorna 0 matches
- [ ] `bot_grid_replication_error_pct` médio < 2% (verificar via `/metrics` plot ou `curl /metrics` periódico)
- [ ] `bot_grid_fill_latency_ms` p95 < 60_000ms (60s)
- [ ] `/operations/current` → `current_pnl_breakdown.hedge_pnl` cobrindo ≥98% da `il_natural` (relação de replicação OK)
- [ ] Disk + memory + CPU estáveis (`top`, `df`)

**Critério de pass:** TODOS os ✓ acima por 24h consecutivas.

---

## Critérios de promoção (sandbox → produção)

Antes de mergear `feature/predictive-grid-stops` em master e flipar default flag em produção:

- [ ] Smoke 1, 2, 4 passed (Smoke 3 pode estar pendente se Beefy não rebalanceou)
- [ ] Bot rodou 24h+ estável em sandbox
- [ ] `replication_error_pct < 2%` médio sustentado
- [ ] `fill_latency_ms p95 < 60s`
- [ ] Zero crashes
- [ ] Code review final do branch (`superpowers:code-reviewer` agent ou manual)
- [ ] User signoff (visualmente conferiu dashboard + métricas)

---

## Rollback (se algo der errado)

Em qualquer ponto durante o smoke, rollback é simples:

1. **Stop bot:** `systemctl stop automoney.service`
2. **Cancel todas as stops na Lighter:** via UI Lighter (ou via curl direto)
3. **Edit `.env`:** `PREDICTIVE_GRID_V2=false`
4. **Start bot:** `systemctl start automoney.service`
5. Bot volta ao legacy taker chase. Posição short na Lighter continua valid (não foi alterada). Beefy LP continua intocada.

Sem perda de capital se rollback for feito antes do bot fillar ordens em volume.

---

## Phase D2 (cutover) — só depois do smoke aprovar

Quando todos os critérios de promoção forem ✓:

1. PR review do branch `feature/predictive-grid-stops` → merge em master
2. No deploy em produção:
   - `git pull` na droplet de produção
   - Edit `.env`: `PREDICTIVE_GRID_V2=true`
   - `systemctl restart automoney.service`
3. Após 24h+ rodando estável em produção:
   - Open PR removendo legacy `_maybe_rebalance_leg` path do iterate (Task D2 cutover)
   - Flag default `True` em config.py
4. Update `CLAUDE.md` + `WORKING_ON.md` documentando que predictive grid v2 é o design atual

---

## Limitações conhecidas (do code review final)

Documentado aqui pra user ter clareza durante smoke:

- **C-2 (`cancel_all_stops` cancela TODA a conta, não só market):** A SDK Lighter
  `cancel_all_orders` é account-wide. Em produção single-market single-account
  isso é equivalente a "cancel all stops desse market" — sem problema. Mas se
  algum dia o bot rodar multi-market OU mixar com ordens manuais na mesma
  conta, esse rebuild zera tudo. Mitigação futura: implementar market-scoped
  cancel iterando active orders + cancel_stop_order individual.

- **C-3 (sem mapping cloid → order_index):** `place_stop_limit_order` descarta
  o `order_index` retornado pela SDK. Sem ele, `cancel_stop_order` individual
  é inviável — só `cancel_all_stops` funciona. Pra MVP isso é OK porque o
  flow é "rebuild inteiro on range_change" (cancela tudo via cancel_all) e
  "fill repõe próximo tick" (não precisa cancelar nada). Mas se quiser
  cancelar nível específico (ex: out-of-range cleanup parcial), precisa
  capturar order_index — refactor futuro.

- **I-1 (decimals hardcoded ARB-USD: `lighter_price_decimals=5`,
  `lighter_size_decimals=1`):** `_maintain_grid` e `_on_grid_fill` usam esses
  valores fixos. Funciona pra ARB/USDC.e (pair atual escolhido). Se você
  trocar pra outro par via pair-picker, vai precisar atualizar antes da
  próxima op. Fix definitivo: ler de `meta = exchange.get_market_meta(symbol)`.

- **I-6 (replication_error_pct sempre = 0% no dashboard):** A métrica está
  declarada mas nunca computada. Dashboard mostra "0.00%" verde sempre. Não
  use esse critério no Smoke 4 — verifique via PnL breakdown (`hedge_pnl /
  il_natural`) que é a métrica real de replicação.

- **`min_quote_amount=$10` Lighter:** o adapter raise apenas se `base_amount_raw <= 0`.
  Se um nível da grade tiver notional `< $10`, a Lighter pode rejeitar com erro
  da SDK (`place_stop_limit_order failed: ...`). Vai aparecer em log warning,
  mas não bloqueia o resto da grade. Se ver muitos warnings, considerar
  raise size mínimo via diluir L (menos níveis, cada um maior).

---

## Anexo: queries úteis pra debug durante smoke

```bash
# Estado atual da grade (stops ativos sem fill nem cancelamento)
ssh root@<sandbox_ip> '/opt/automoney/venv/bin/python3 -c "
import sqlite3
c = sqlite3.connect(\"/data/automoney.db\")
cur = c.execute(\"SELECT side, target_price, size, trigger_price, placed_at FROM grid_orders WHERE is_stop_order=1 AND fill_id IS NULL AND cancelled_at IS NULL ORDER BY target_price\")
for r in cur.fetchall(): print(r)
"'

# Métricas chave
curl -s http://<sandbox_ip>:8000/metrics | grep -E "bot_grid_|bot_beefy_range"

# Status engine
curl -s http://admin:<pwd>@<sandbox_ip>:8000/operations/current | python -m json.tool
```
