# WORKING_ON

**Última atualização:** 2026-05-11 (predictive v2 ✅ master + funding window PR #3 + Fly.io PR #4 abertos)

## Foco atual
**Fly.io deploy** implementado (item 4). PR [#4](https://github.com/wallmxz/New-cryptohedge/pull/4) aberto. Aguarda **operação manual do user** (push secrets, criar volume, deploy, migrar DB, smoke compare) — runbook em `docs/flyio-runbook.md`.

**Status PRs:**
- PR #3 funding window — aguarda validação + merge
- PR #4 Fly.io deploy — aguarda operação cutover (~25 min do user)

**Próximos passos:**
1. Mergear PR #3 (funding window) — quando validar live
2. Operação Fly.io: secrets + volume + deploy + sftp DB + smoke compare (runbook step-a-step)
3. Pós-deploy estável: encerrar `start.bat` local permanentemente
4. **Próxima feature:** brainstorm UI/UX (item 5) — user pediu desde o compact

## Pendente (próximas sessões)
- **Cross-check on-chain:** script analisando fills Lighter vs ticks Beefy históricos via Alchemy archive (user pediu "primeiro Fly, depois script")

## Estado do bot agora
- **Branch atual:** `master` (fast-forwardada após merge do PR #2)
- **`master`:** contém predictive v2 + tudo anterior (merge commit `c68f1ae`, +2367/-1110 LoC)
- **Branch `feature/predictive-grid-v2`:** preservada (caso queira referência)
- **Op ativa no DB:** #28 (cross-pair WETH/ARB, baseline manual $50.03)
- **Hedge model:** novo módulo `engine/hedge_model.py` + `chains/v3_position.py` — lê L_main + L_alt direto da Uniswap V3 pool, computa target via fórmula V3, verify vs Beefy actual a cada iter
- **Invariante estrutural:** target sempre vem de `actual × hedge_ratio` (Beefy), predicted é só pra verify+status
- **`hedge_model_status` field:** novo (states: `warming_up | active | verify_diverging:X% | unavailable`); UI surfacing em operation card
- **Reactive `_maybe_rebalance_leg`:** ÚNICO fire path — nada mudou no comportamento das ordens
- **Hedge ratio:** `0.98`; **Floor rebalance:** `$0.50` USD/leg
- **Anti-engasgo:** 5s `asyncio.wait_for` em RPC reads + try/except outer + position-truth stamping mantida
- **Uvicorn:** estado live desconhecido — user precisa restart pra carregar v2

## Status da fila de trabalho
| # | Item | Status |
|---|---|---|
| 1 | Fix do over-hedge ARB | ✅ user confirmou OK em 2026-05-10 |
| 2 | **Predictive Hedge Model v2** | ✅ MERGEADO em master (PR #2, c68f1ae) — aguarda validação live |
| 3 | Funding window (estender datetime picker) | próximo (após validação live) |
| 4 | Deploy Fly.io | pendente |
| 5 | Brainstorm UI/UX (novo) | pendente, user explicitamente pediu antes do compact |

## Final review do PR #2 — verdict: SHIP WITH FOLLOWUP
- ✅ Spec coverage 100%
- ✅ Arquitetura sound, invariante "actual wins" enforced
- ✅ Live deploy risk **LOW** (target sempre vem de Beefy, idêntico ao reactive que já funciona)
- ✅ Op #28 restart-safe (sem mudança de schema)

Follow-ups (não bloqueantes, em qualquer sessão futura):
- Reforçar `test_iterate_uses_actual_target_for_fire_even_when_predicted_diverges` pra invocar engine real (atualmente é puramente arithmetic)
- Estreitar o `except Exception` em `read_position_alt` pra exceptions específicas do web3
- Monitorar `hedge_model_status` na primeira hora pós-deploy — se oscila entre `active` e `verify_diverging`, threshold 1% pode precisar tuning

## Verificação live (próximo passo do user)
1. `stop.bat` → `start.bat`
2. Watch `uvicorn.log` → `HedgeModel.refresh_cache: L_main=<int>, L_alt=<int|None>` + `hedge_model_status: warming_up → active`
3. Operation card mostra "Hedge model: <status>" — confirmar
4. Drift fires (se necessário) acontecem via `_maybe_rebalance_leg` (reactive path)
5. Se OK → merge PR #2 pra master

## Notas pra próxima sessão
- Pipeline obrigatório (`memory/feedback_use_pipelines.md`)
- Compra no ask, vende no bid — sem buffer (`memory/feedback_no_price_buffer.md`)
- Verificar posição via fonte autoritativa (`memory/feedback_verify_before_fire.md`)
- Não disparar trades por iniciativa (`memory/feedback_no_autonomous_trades.md`)
- Subagent-driven default ao executar plans (`memory/feedback_subagent_driven_default.md`)
- Atualizar WORKING_ON e memory a cada mudança de foco (`memory/feedback_keep_state_fresh.md`)
