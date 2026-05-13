# HANDOFF — sessão 2026-05-12

## Goal
Brainstorm + spec + implementação do **snapshot atômico (c.1)** pra eliminar o jitter visual de Net PnL no dashboard (saltos de +$0,32 → −$0,10 em ~1s causados por Hedge PnL atualizar via Lighter WS antes do Pool $ via chain RPC na mesma iter do engine).

User aprovou a abordagem (c.1) explicitamente:
> "Acho que isso aqui é legal implementar"

## Estado atual do código
- Branch: `master` (clean, 0 ahead/behind)
- Bot rodando 24/7 em DO Frankfurt (`104.248.44.6:8000`, systemd `automoney.service`, op #28 ativa)
- Nenhuma mudança de código pendente nessa linha de trabalho — estávamos no estágio de **brainstorm**, ainda sem escrever spec
- **Mudança operacional aplicada nessa sessão**: instalado `python-multipart` no servidor DO via `pip` (não no `requirements.txt` ainda) — precisa ser persistido pra sobreviver redeploy

## Arquivos ativos (que serão tocados quando a implementação começar)
- `engine/__init__.py` — método `_iterate` (~linhas 980-1115). Lê chain via `_read_position`/`_read_price`, depois calcula `pool_value_usd` e PnL. **Aqui entra o snapshot atômico.**
- `engine/pnl.py::compute_operation_pnl` — recebe preços; precisa garantir que os preços vêm do snapshot, não de re-fetch
- `state/hub.py` (ou onde estiver `StateHub`) — campos `pool_value_usd`, `pool_tokens`, `last_iter_timings` populados juntos
- `tests/test_engine_grid.py` ou novo `tests/test_atomic_snapshot.py`
- `requirements.txt` — adicionar `python-multipart>=0.0.20,<1.0` (não relacionado ao snapshot, mas patchear na mesma leva)

## Decisões já tomadas no brainstorm (não revisitar)
- **Diagnóstico confirmado on-chain**: bot bate `previewWithdraw` centavo-a-centavo no instante T (verificado bloco 461933177). NÃO É bug de leitura, NÃO É lucro perdido. É apenas dessincronia temporal entre `chain RPC (Pool $)` e `Lighter WS (Hedge PnL)` dentro do MESMO iter do engine.
- `strategy.balances()` JÁ inclui `positionMain + positionAlt`. Não tem bug do alt.
- O gap de ~$3 que o user observou (Beefy=$442,5 vs bot=$439) era display lag (oracle Lighter momentaneamente atrás de CoinGecko + composição stale). Real value preservado no `final_net_pnl` no fechamento da operação.
- Escopo escolhido: **(c.1) snapshot atômico pra display SOMENTE**. Foi entre 3 opções:
  - (A=c.1) só display
  - (B) display + decisão de fire (rejeitado: adiciona até 1s de latência no fire, over-engineering pra um bug visual)
  - (C) híbrido (rejeitado: dobra complexidade)

## Próxima pergunta pendente do brainstorm
Eu estava na **Pergunta 1 do brainstorm formal** quando a sessão foi interrompida. User respondeu informalmente que quer (c.1), mas o spec ainda precisa nailar:

1. **O que exatamente compõe o "snapshot"?** — só `mid_snapshot = ws_book_top.copy()` (preços) ou também a composição (`my_amount0`, `my_amount1`)?
2. **Onde o snapshot é tirado?** — começo da iter? Após chain read?
3. **Comportamento se WS cache vazio** (startup) — usar último mid conhecido? skip iter? warning?

## O que tentei e falhou nessa sessão
- **`curl -X POST /settings hedge_ratio=0.99` deu 500** — diagnóstico: `python-multipart` faltava no servidor. **Resolvido**: `pip install python-multipart` no venv do DO + restart systemd. Agora dashboard "Salvar" funciona e curl funciona. **Pendente**: adicionar ao `requirements.txt`.
- **Tentei `python3` em Git Bash local** pra processar JSON da Beefy — não tem Python no PATH local (descobri que o user usa Python embeddable em `C:\Users\Wallace\Python313\`). Workaround: rodar tudo via SSH no servidor DO que tem venv configurado.

## Pendências paralelas (NÃO pertencem ao snapshot atômico, só pra contexto)
1. **Bug `.get()` no funding** (`exchanges/lighter.py::get_funding_total_since`) — log spammando `'PositionFunding' object has no attribute 'get'` ~1×/seg. Bloqueia merge do PR #3. Fix: trocar `e.get(k, default)` por `getattr(e, k, default)`. ~5 min.
2. **`requirements.txt`** — adicionar `python-multipart` (já instalado manualmente no DO).
3. Cross-check on-chain (~30 min)
4. Otimizar `verify_fill` latency (~1h)
5. Brainstorm UI/UX (1-2 sessões)

## Próximo passo concreto
Retomar o brainstorm respondendo a Pergunta 1 acima (escopo do snapshot — só preços ou preços+composição). Sugestão: snapshot deve incluir **(a)** `oracle_prices` (dict de token→USD do Lighter WS), **(b)** composição `(amount0, amount1)` da chain, **(c)** timestamp único. Tudo gravado no `StateHub` numa única atribuição via dict-replace pra publicar atômico no SSE. Depois disso, escrever spec em `docs/superpowers/specs/2026-05-12-atomic-snapshot-design.md`, user revisa, invoca `superpowers:writing-plans`, executa via `superpowers:subagent-driven-development`.

## Estado do hedge_ratio
Aplicado `hedge_ratio=0.99` no DB via curl + engine reiniciado (carregou via `python-multipart` recém instalado). Verificado: `[('hedge_ratio', '0.99')]` no `config` table. Default permanece **0.98** no `memory/project_hedge_ratio.md` (intencional — esse é o default do projeto, não o valor live).
