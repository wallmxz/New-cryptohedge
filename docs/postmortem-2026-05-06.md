# Postmortem — Sessão 2026-05-05/06

**Capital final:** $976.89 USDC (do inicial $984.85). **Custo da sessão: $7.96 em gas + slippage** (sem perdas em short/over-hedge porque foram fechadas a tempo).

## Timeline com bugs encontrados

```
$984.85 wallet inicial (USDC + WETH + ARB recovered)
   │
   ▼
[Op #16] Tenta dual-leg bootstrap (USDC→WETH + USDC→ARB + deposit Beefy + 2 shorts)
   │  ✅ 2 swaps OK
   │  ❌ Beefy deposit revertou: wrong contract
   │     ▶ BUG #1: Estávamos enviando deposit() pro `strategy_address`,
   │       mas Beefy CLM v2 usa earn vault como destino. Strategy só tem
   │       view-only state + onlyVault writes.
   │     ▶ Fix: BeefyExecutor recebe agora `earn_address` e manda deposit lá.
   │
   ▼
[Op #17,18,19] Várias retentativas
   │  ❌ Deposit revertou de novo
   │     ▶ BUG #2: gas_limit hardcoded em 500_000. Beefy CLM deposit
   │       consome ~740k (transferFrom × 2 + harvest + V3 increaseLiquidity
   │       + share mint). Out of gas mascara como revert genérico.
   │     ▶ Fix: send_tx usa estimate_gas + 20% buffer.
   │  ❌ Deposit revertou em outro retry
   │     ▶ BUG #3: Beefy NotCalm() — anti-MEV check rejeita deposits
   │       quando current tick desvia muito do TWAP do pool.
   │     ▶ Fix: pre-flight eth_call. Se vault retornar NotCalm(),
   │       espera 30/60/120s e retenta.
   │  ❌ ARB sobrou na wallet
   │     ▶ BUG #4 (descoberta): vault Beefy CLM v2 ignora `amount1` —
   │       só consome amount0 e zappa internamente. Por isso comprar
   │       ARB pre-deposit era waste.
   │     ▶ Fix: refator pra single-swap (USDC→token0 só).
   │
   ▼
[Op #20] Depósito completou ✅
   │  ❌ Lighter short falhou: code=21120 invalid signature
   │     ▶ BUG #5: SDK Lighter `create_order()` não tem o decorator
   │       @process_api_key_and_nonce — defaults sentinel (255, -1)
   │       passam direto pro signer Go binding causando sig inválida.
   │     ▶ Fix parcial: passar api_key_index + nonce explícitos.
   │
   ▼
[Op #21] Tentou de novo
   │  ❌ Mesmo erro 21120
   │     ▶ BUG #6 (causa raiz): a private key do `.env` não corresponde
   │       à pubkey registrada na conta da Lighter. Detectado via
   │       signer.CheckClient() retornando "private key does not match
   │       the one on Lighter".
   │     ▶ Fix: gerar nova API key no app.lighter.xyz, atualizar .env.
   │
   ▼
[User regenera key] ✅ CheckClient passa
   │
   ▼
[Op #22] hedge-existing
   │  ❌ Mudou pra code=21104 invalid nonce
   │     ▶ BUG #7: Lighter docs avisam "wait at least 350ms before using
   │       same api key". Retries em < 350ms causam server-side dedup race.
   │     ▶ Fix: backoff 600ms entre retries de nonce error.
   │
   ▼
[Op #23] Tentou de novo
   │  ❌❌❌ CRÍTICO — over-hedge ETH 0.7 ETH (target era 0.077)
   │     ▶ BUG #8 (CRÍTICO): adapter retentava após `create_order=success`
   │       quando `_verify_fill` retornava 0. Cada retry = nova short
   │       aberta. Acumulou 9× o tamanho intended.
   │     ▶ Fix: NUNCA retentar após server-accept. Se verify_fill=0,
   │       returnar Order com size=0 e confiar que IOC cancelou
   │       (auto-cancel = no fill).
   │
   ▼
[Recovery total] ✅
   │  swap WETH+ARB → USDC, withdraw Beefy
   │  $976.89 final em USDC
   │
   ▼
[Custo total: $7.96]
```

## Bugs encontrados (resumo)

| # | Bug | Local | Severidade | Status |
|---|---|---|---|---|
| 1 | Deposit no strategy em vez do earn vault | `chains/beefy_executor.py` | **Crítica** (revertia tudo) | ✅ Fixed |
| 2 | gas_limit hardcoded 500k pra deposit que precisa ~740k | `chains/beefy_executor.py` | Crítica (out of gas oculto) | ✅ Fixed |
| 3 | NotCalm() sem retry | `chains/beefy_executor.py` | Média (UX ruim) | ✅ Fixed |
| 4 | Refator dual-leg single-swap | `engine/lifecycle.py` | Design | ✅ Fixed |
| 5 | SDK Lighter sem decorator nonce | `exchanges/lighter.py` | Crítica (invalid sig) | ✅ Fixed |
| 6 | Private key errada no `.env` | `.env` (config) | **Bloqueante** | ✅ Fixed (user trocou) |
| 7 | Retry rápido < 350ms causa nonce race | `exchanges/lighter.py` | Média | ✅ Fixed (backoff 600ms) |
| 8 | **Over-hedge por retry após server-accept** | `exchanges/lighter.py` | **CRÍTICA** (perda real) | ✅ Fixed |
| 9 | Default `consolidate` consumia ARB sem permissão | `engine/lifecycle.py` | Alta (capital surpresa) | ✅ Fixed (default `keep`) |
| 10 | UI mostrava "dYdX" em texto | `engine/lifecycle.py` | Cosmético | ✅ Fixed |

## Lições

1. **Sempre validar credenciais antes do bootstrap.** O `CheckClient()` da Lighter SDK detecta key/server mismatch em ~50ms. Deveria rodar no `connect()`.
2. **Nunca retentar transações idempotentes após server-accept.** O fix do retry over-hedge é a regra: se a exchange disse "aceitei", confia. `verify_fill=0` significa "não fillou" — não "tenta de novo".
3. **Vault contracts não têm interface universal.** Mesma assinatura `deposit(amount0, amount1, minShares)` em CLMs diferentes pode ter comportamento diferente (Beefy CLM v2 ignora amount1).
4. **Defaults silenciosos consumindo capital sempre erram.** `consolidate` deveria ser opt-in, não opt-out — qualquer ação que mexa em saldo precisa do user explicitamente clicando.
5. **Estimate gas > hardcoded gas.** Em chains EVM, contratos evoluem. Estimate dá +20% buffer e valor correto pra cada call.
