# WORKING_ON

**Última atualização:** 2026-05-10 (reabertura pós-compact)

## Foco atual
Sistema de continuidade entre sessões (SessionStart hook + memory + WORKING_ON.md) acabou de ser instalado.
Próximo passo técnico: decidir qual frente do roadmap atacar primeiro (ver fila abaixo).

## Estado do bot agora
- **Branch:** `feature/cross-pair-dual-hedge` (em sincronia com `origin`, 0 commits ahead)
- **PR aberto:** [New-cryptohedge#1](https://github.com/wallmxz/New-cryptohedge/pull/1) — recebeu todos os commits novos da branch sem precisar de novo PR
- **Op ativa no DB:** #28 (cross-pair WETH/ARB, baseline manual $50.03)
- **Predictive grid:** **DESLIGADO** desde commit `ed8923d` (bug positionAlt — ver `memory/project_predictive_status.md`)
- **Reactive rebalance:** ativo com floor `$0,50` USD/leg
- **Hedge ratio:** `0.98` (98%)
- **Uvicorn:** estado atual desconhecido pelo assistant; user usa `start.bat`/`stop.bat`
- **Working tree:** limpo exceto `tmp_out/` untracked (não comitar)

## Último blocker reportado pelo user
**ARB over-hedged** apesar do floor $0.50 em reactive. Reconciliação não está convergindo. User pediu investigar antes de qualquer feature nova.

## Fila de trabalho aprovada (ordem do user)
1. **Fix do over-hedge ARB persistente** ⬅ próximo
2. **Predictive v2** — grid pra timing + `my_amount × hedge_ratio` pra target + reconciler $0,50 toda iter
3. **Funding window** — estender datetime picker pra também afetar Funding (~50 LoC)
4. **Deploy Fly.io** — sem perda de DB/.env, login mantido, smoke pós-deploy

Detalhes em `~/.claude/projects/C--Users-Wallace-Desktop-NewHedgeBot/memory/project_pending_work.md`.

## Decisões em aberto
Nenhuma — user aprovou os 4 itens da fila. Falta só decidir qual atacar primeiro na próxima sessão.

## Notas pra próxima sessão
- Toda mudança em engine/exchanges/lifecycle é dinheiro real. Pipeline brainstorm/spec/plan/subagent obrigatório (ver `memory/feedback_use_pipelines.md`).
- Compra no ask, vende no bid — sem buffer (ver `memory/feedback_no_price_buffer.md`).
- Verificar posição via fonte autoritativa antes de re-fire (ver `memory/feedback_verify_before_fire.md`).
- Não disparar trades por iniciativa — user clica botão (ver `memory/feedback_no_autonomous_trades.md`).
