# WORKING_ON

**Última atualização:** 2026-05-12 (BOT EM PRODUÇÃO no DO Frankfurt — local desligado)

## Foco atual
Bot rodando 24/7 em **DigitalOcean Frankfurt** (`104.248.44.6:8000`, systemd `automoney.service`). Op #28 ATIVA, hedgeando com latência ~200ms steady-state (vs 1400ms local). Master pós-PR #5 inclui /health/engine + lighter-sdk fix em requirements.txt + Fly tooling preservado como histórico.

## Última ação pendente do user
User mudou `hedge_ratio` no dialog de Configurações pra **0.99** mas DB ainda mostra `0.98` — falta clicar **Salvar** no dialog. Dito ao user: ou clica Salvar OU eu faço via `curl -u admin:Wallace1 -X POST http://104.248.44.6:8000/settings -d "hedge_ratio=0.99"`. User pediu compact ANTES de eu rodar. Próxima sessão: confirmar com user e fazer.

## Estado do deploy (operacional)
- **IP:** `104.248.44.6`
- **Dashboard:** http://104.248.44.6:8000 (admin / Wallace1)
- **SSH:** `ssh -i C:\Users\Wallace\.ssh\id_ed25519 root@104.248.44.6`
- **Systemd unit:** `/etc/systemd/system/automoney.service`
- **Code:** `/opt/automoney/` (git clone master)
- **DB:** `/data/automoney.db` (persistente)
- **Logs:** `/var/log/automoney.log`
- **Comandos completos:** `~/.claude/projects/.../memory/reference_do_deploy.md`

## Por que DO Frankfurt e não Fly/Oracle/etc
Lighter retorna `code 20558 "restricted jurisdiction"` (mensagem JSON literal do servidor) pra:
- Fly.io qualquer região (testado fra + iad) — ASN bloqueado
- Qualquer cloud em US/Canada (Oracle Ashburn, DO NYC1 testados)
- DO Frankfurt (ASN 14061) **passa**

Workaround documentado pela Lighter (`?readonly=true`) só serve pra read-only — bot precisa trade. Detalhes em `memory/project_lighter_waf_datacenter.md`.

## Status PRs/branches
- **PR #5** (feat: deploy + /health/engine + lighter-sdk fix) — ✅ MERGEADO em master (`c5713895`)
- **PR #4** (Fly.io deploy) — fechado com postmortem (Fly inviável)
- **PR #3** (funding window) — aberto, BLOQUEADO pelo bug `'PositionFunding' object has no attribute 'get'` em `LighterAdapter.get_funding_total_since`
- Branch `feature/flyio-deploy` — preservada após merge (não deletada)

## Pendências da fila (importância decrescente)
1. **Bug funding `.get()`** (~5 min) — fix `e.get(k, default)` → `getattr(e, k, default)` em `exchanges/lighter.py::get_funding_total_since`. Bloqueia merge do PR #3.
2. **Cross-check on-chain** (~30 min) — script via Alchemy archive comparando ticks Beefy vs fills Lighter, validar hipótese do user sobre fires mal-sincronizados.
3. **Otimizar verify_fill latency** (~1h) — eliminar spikes de 7s/iter quando 2 legs fire simultâneo. Opções: skip `_verify_fill` HTTP (confiar só em position-truth + reconciler), reduzir timeout 3-5s → 1s, ou migrar pra WS push `update/account_all`.
4. **Brainstorm UI/UX** — user pediu desde o compact ("o site é quase inútil"). Pipeline brainstorm/spec/plan/subagent completo. 1-2 sessões.

## Notas pra próxima sessão
- Pipeline brainstorm/spec/plan/subagent obrigatório (`memory/feedback_use_pipelines.md`)
- Subagent-driven default ao executar plans (`memory/feedback_subagent_driven_default.md`)
- Atualizar WORKING_ON e memory a cada mudança de foco (`memory/feedback_keep_state_fresh.md`)
- Compra no ask, vende no bid — sem buffer (`memory/feedback_no_price_buffer.md`)
- Verificar posição via fonte autoritativa antes de re-fire (`memory/feedback_verify_before_fire.md`)
- Não disparar trades por iniciativa — user clica botão (`memory/feedback_no_autonomous_trades.md`)
- start.bat / stop.bat agora são DEPRECATED (bot está em DO, não local)
