# WORKING_ON

**Última atualização:** 2026-05-15 01:43 UTC — Bot LIVE pós-fix triplo (cloid wraparound + Alchemy→Ankr + Lighter key slot 67). Op #29 hedgeando normal.

## Foco atual

**Master `f5debca` deployed em produção (DO Frankfurt).** Hoje resolvemos 3 problemas concorrentes:
1. **Cloid 256-wraparound** — `_next_cloid_for_leg` mascarava `_cloid_seq & 0xFF` → só 256 cloids únicos por (run, leg). Após 256 stops, todo INSERT colidia com row antiga em `grid_orders.cloid` (UNIQUE). Fix: layout 64-bit run_id (32) | leg (8) | seq (24) = 16M únicos. Commit `1c7c126`, merge `f5debca`. Tests em `tests/test_engine_cloid.py` (4 novos).
2. **Alchemy free tier exausto** — abuse de 18h queimou 30M/30M CUs do mês. Trocamos `ARBITRUM_RPC_URL` pra Ankr Freemium (whitelist IP `104.248.44.6`).
3. **Lighter API key broken** — slot 2 não batia mais com server. User regerou em **slot 67** (`LIGHTER_API_KEY_INDEX=67`).

**Estado live do bot (verificado 2026-05-15 01:43 UTC):**

| | |
|---|---|
| Service | `active` (restart 01:42 UTC) |
| Op #29 | active, baseline pool $199.75 |
| Net PnL | -$0.09 |
| Pool $ | +$0.10 |
| Hedge PnL | -$0.21 (realized +$0.14, unrealized -$0.35) |
| Funding | +$0.017 |
| Stops live na Lighter | **15** (7 sells $0.13040-$0.13119 + 8 buys $0.13135-$0.13227) — desfalcado em 1 sell |
| Errors last 60s | 0 UNIQUE / 0 invalid sig / 0 Lighter 429 / 0 Alchemy 429 |

## Bugs remanescentes (post-fix)

1. ⚠️ **1 sell faltando no grid (15/16)** — reconciler tentando postar @ $0.13127 (entre sells e buys, próximo do market) → safety clamp empurrando trigger past market → Lighter rejeita silencioso. Edge-case do bug #4 do handoff. Não bloqueia.
2. ⚠️ **Hedge model status: warming_up / verify_diverging:100%** (predict mistura RAW V3 com HUMAN p_now)
3. ⚠️ **LP fees attribution = 0** (Beefy Harvest listener — user disse "não precisa")
4. ⚠️ **engine.pair_factory rebuild lifecycle a cada HTTP request** (log spam + possível aiohttp leak)

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
