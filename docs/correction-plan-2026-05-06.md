# Plano de correção — pós-postmortem 2026-05-06

> Reviews completos: **hedge logic** + **curve math**. Cleanup audit ainda rodando.

## 🔥 Bugs CRÍTICOS encontrados (aplicar PRIMEIRO)

### #C1 — `engine/__init__.py:961` — sizing errado do short token1 em dual-leg
```python
targets[symbols[1]] = compute_y(L_user, p_now, p_a) * self._hub.hedge_ratio  # ❌
```
`compute_y(L, p, p_a) == my_amount1` apenas quando `share=1.0` (single user no vault). Em qualquer multi-user vault Beefy a fórmula está errada — usa o L da posição inteira do strategy quando deveria usar nossa fatia. **Resultado:** under-hedge ou over-hedge persistente em ARB.

**Fix:** `targets[symbols[1]] = my_amount1 * self._hub.hedge_ratio`

### #C2 — `exchanges/lighter.py:344-356` — bug latente de over-hedge ainda existe
Após o fix do retry-after-server-accept (que resolveu o stack de 0.9 ETH), ainda restou: quando `verify_fill=0` retorna, o adapter retorna `Order(size=0, status="cancelled")` sem confirmar pela posição real se a ordem fillou ou não. Se o lookup `account_inactive_orders` for lento (>2s timeout), pode ter filled mas o adapter "miente". Engine main loop então dispara **outra** ordem por drift detection (sem cooldown no path do bootstrap).

**Fix:** antes de retornar `cancelled`, fazer `get_position(symbol)`. Comparar com snapshot pre-call. Se `delta > 0`, retornar `Order(size=delta, status="filled")`.

### #C3 — UI da curva LP **mente** sobre o que o bot faz
`compute_curve_preview` monta grid via `compute_target_grid` (modelo maker). Mas runtime do engine (`_iterate`) usa **takers level-triggered** quando drift > min_notional. **A UI mostra 199 pontos no chart e zero ordens reais existem na exchange.**

**Fix opção A** (menos invasivo): renomear no frontend "Buys/Sells" para "Triggers de correção (taker)". Documentar.
**Fix opção B**: restaurar caminho maker (postar limit orders nos preços de cada GridLevel). Mais trabalho.

Vou pela A — match com a realidade atual do engine.

### #C4 — Pre-flight `SignerClient.CheckClient()` no `connect()` da Lighter
A causa-raiz do invalid-signature de hoje (key errada) seria pega em ~50ms se rodássemos `CheckClient` no startup. Sem isso, qualquer config errada vira 5 retries por ordem + alerta tardio.

**Fix:** chamar `CheckClient` em `LighterAdapter.connect()`. Se falhar, log warning e marcar `connected_exchange=False` (sistema sobe sem exchange, igual já fazemos pra WAF).

---

## 🟠 Bugs ALTOS

### #A1 — Cloid mascarado a 32 bits sem run_id
`exchanges/lighter.py:267` faz `int(cloid_int) & 0xFFFFFFFF`. Em dual-leg paralelo (asyncio.gather de 2 ordens), risco de colisão entre legs após restart (`time.time() & 0xFFFF` cíclico ~18h).

**Fix:** incorporar `_run_id` (16 bits altos), igual o engine faz em `_next_cloid_for_leg`.

### #A2 — `compute_curve_preview` fallback errado em cross-pair
`engine/__init__.py:891`: `p0_usd = oracle_prices.get(symbols[0], p_now)`. Pra cross-pair (ARB/WETH), `p_now` é ratio do pool (não USD). Cai como fallback → `pool_value_usd` factor ~ETH price errado.

**Fix:** retornar erro explícito quando oracle falha em cross-pair.

---

## 🟡 Bugs MÉDIOS

### #M1 — `compute_target_grid` produz ordens abaixo de min_notional
`step_x = min_notional_usd / p_now` mas níveis perto de p_b ficam abaixo. Engine pode rejeitar.

**Fix:** `step_x = min_notional_usd / p_b` (worst-case).

### #M2 — Re-escala `max_orders` ignora distância de p_now aos bounds
Quando p_now colado num bound, lado dominante explode levels.

**Fix:** `step_x = max(step_x, max(x_now, x_at_a − x_now) / (max_orders / 2))`.

### #M3 — `open_shorts_for_existing_position` marca op `ACTIVE` antes de abrir shorts
Engine main loop pode reagir antes do gather. Op fica `ACTIVE` no DB mesmo se shorts falham (raise propaga depois).

**Fix:** inserir como `STARTING`, transicionar pra `ACTIVE` só após `gather` dos shorts ter retornado sem erro.

### #M4 — `_swap_residuals_to_usdc` single-leg usa `p_now` do pool
Se preço fora de range, valor garbage.

**Fix:** usar oracle ou recusar se p_now fora do range conhecido.

### #M5 — `_bootstrap_dual_leg` chama `compute_optimal_split` mas resultado é descartado
Dead code — `baseline_amount0/1` é sobrescrito por `update_baseline_amounts` post-deposit.

**Fix:** simplificar; remover compute do dual-leg single-swap. Manter no preview com label "estimativa pós-zap".

---

## 🟢 Bugs BAIXOS

### #B1 — `_level_key` ignora `target_short` (`engine/grid.py:14`)
Benigno hoje (engine não usa GridLevel em runtime). Risco se o caminho maker voltar.

### #B2 — `cancel_long_term_order` swallow exception (`exchanges/lighter.py:364`)

### #B3 — `_FILL_VERIFY_TIMEOUT_S=2.0` muito apertado
**Fix:** subir pra 4-5s OU sair do loop assim que `inactive_orders` retornar nosso cloid em qualquer estado terminal.

### #B4 — Hard refresh nonce só em "invalid nonce/signature"
**Fix:** sempre `hard_refresh_nonce` antes de retry.

---

## Ordem de execução

1. **#C1** (token1 sizing — engine/__init__.py:961). 30 linhas.
2. **#C2** (over-hedge guard via get_position). 40 linhas.
3. **#A1** (cloid run_id). 5 linhas.
4. **#C4** (pre-flight CheckClient). 30 linhas.
5. **#C3** (UI label rename). 10 linhas.
6. **#A2 + #M1 + #M2 + #M3 + #M4 + #M5** — fixes pontuais.
7. Cleanup do agent #3 (quando completar).
8. **Tests** — adicionar regression tests por bug crítico (no-retry-after-accept, single-vs-multi-user vault).
9. Commit + smoke test.
