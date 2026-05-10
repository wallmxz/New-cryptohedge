# WORKING_ON

**Última atualização:** 2026-05-10 (predictive v2 implementado, PR #2 aberto)

## Foco atual
**Predictive Hedge Model v2** — implementação completa via pipeline `superpowers:brainstorming` → spec → writing-plans → subagent-driven-development (T1-T9 ✅, 15 commits). PR [#2](https://github.com/wallmxz/New-cryptohedge/pull/2) aberto contra master, **aguardando user validar live + decidir merge**.

**Próximo passo após merge do #2:**
1. Funding window (item 3 do roadmap) — estender datetime picker pra também afetar Funding (~50 LoC)
2. Deploy Fly.io (item 4)
3. **Brainstorm UI/UX** — user pediu antes do compact ("o site é quase inútil"), discutir o que ter no painel

## Estado do bot agora
- **Branch local:** `feature/predictive-grid-v2` — pushada
- **PR aberto:** [#2 feat: predictive hedge model](https://github.com/wallmxz/New-cryptohedge/pull/2) — 15 commits, ~+650/-1100 LoC
- **`master`:** sem o predictive v2 ainda — aguarda merge
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
| 2 | **Predictive Hedge Model v2** | ✅ pushada (PR #2), aguardando validação live + merge |
| 3 | Funding window (estender datetime picker) | pendente, próximo após v2 mergear |
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
