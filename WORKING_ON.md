# WORKING_ON

**Última atualização:** 2026-05-10 (PR #1 mergeado, abrindo predictive v2)

## Foco atual
**Predictive curve-grid v2** — brainstorm em andamento. Virada arquitetural: grid serve só pra TIMING (detectar cruzamento de level), target absoluto sempre vem de `my_amount × hedge_ratio` direto da Beefy. Reconciler $0.50 toda iter. Engine não pode "engasgar" (RPC timeout sem fallback, await infinito, etc). Pipeline `superpowers:brainstorming` → spec → plan → subagent.

## Estado do bot agora
- **Branch atual:** `feature/predictive-grid-v2` (fast-forward de `master` pós-merge do PR #1)
- **`master`:** sincronizado com `origin/master`, contém todo o trabalho cross-pair + position-truth + baseline + funding poller + datetime picker (merge commit `ea479b9`)
- **Op ativa no DB:** #28 (cross-pair WETH/ARB, baseline manual $50.03)
- **Predictive grid v1:** DESLIGADO desde `ed8923d` (bug `positionAlt` — ver `memory/project_predictive_status.md`)
- **Reactive rebalance:** ativo, floor `$0,50` USD/leg — user confirmou que está OK em 2026-05-10
- **Hedge ratio:** `0.98` (98%)
- **Uvicorn:** rodando na :8000 (PID 24200 visto no último brief; pode ter mudado)
- **Working tree:** limpo exceto `tmp_out/` untracked

## Status da fila de trabalho
| # | Item | Status |
|---|---|---|
| 1 | Fix do over-hedge ARB | ✅ user confirmou OK |
| 2 | **Predictive v2** | 🔄 em andamento agora |
| 3 | Funding window (estender datetime picker) | pendente |
| 4 | Deploy Fly.io | pendente |

## Decisões em aberto
Brainstorm do predictive v2 ainda não começou — perguntas chave a cobrir:
1. Como detectar level crossing sem confiar em `compute_l_from_value` (que estava inflado por positionAlt)?
2. Quando o grid precisa ser reconstruído (Beefy rebalanceia ticks)?
3. Como o engine garante que NÃO trava (timeout, fallback, circuit breaker)?
4. Coexistência com reactive (que já funciona) — primary/fallback ou substituir totalmente?

## Notas pra próxima sessão
- Pipeline obrigatório: brainstorm → spec → plan → subagent (`memory/feedback_use_pipelines.md`)
- Compra no ask, vende no bid — sem buffer (`memory/feedback_no_price_buffer.md`)
- Verificar posição via fonte autoritativa antes de re-fire (`memory/feedback_verify_before_fire.md`)
- Não disparar trades por iniciativa — user clica botão (`memory/feedback_no_autonomous_trades.md`)
- Próximas prioridades depois deste item: 3 (funding window) → 4 (fly.io)
