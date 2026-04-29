# Phase 2.0 — On-chain Execution Design

## Objetivo

Automatizar o lado Arbitrum do lifecycle (swap Uniswap V3 + deposit/withdraw Beefy CLM), eliminando steps manuais e o slippage alto que o usuário paga hoje (~$3 por start/stop = ~31% do retorno anual com APR 27,5% e 1 round-trip/mês).

**Após Phase 2.0:**
- 1 clique "Start" → bot swap → deposit Beefy → snapshot baseline → abre short
- 1 clique "Stop" → bot cancela grade → fecha short → withdraw Beefy → (opcional) swap WETH→USDC
- Custo round-trip steady-state: ~$0,08 (≈0,025% por start/stop). 30× menor que o manual atual.

**Fora de escopo:**
- dYdX collateral management (depositar/retirar USDC do subaccount). Usuário continua pré-funding manualmente.
- Bridge cross-chain (Arbitrum ↔ outras chains).
- MEV protection / Flashbots (não justifica complexidade pra swaps de $150 em pool deep).
- Auto-rebalance de range (Beefy strategy já faz).
- Multi-chain support (só Arbitrum no MVP).

## Decisões de design

### 1. Escopo: só Arbitrum

dYdX margin permanece manual. Usuário mantém ~$130 fixos no subaccount. A parte "criativa" (swap + LP) é o que merece automação; bridge Cosmos↔EVM é uma camada à parte.

### 2. UX: 1-click start/stop

Fluxo único atomicamente orquestrado. State machine no DB rastreia em que step o lifecycle está pra crash recovery. Trade-off vs 2-step: menos friction, complexidade fica no código (state machine), não no usuário.

### 3. Deposit math: same-pool swap + leitura de balance real

**Insight:** swap no MESMO pool Uniswap V3 0,05% que a Beefy CLM usa garante slippage mínimo (~0,055% total = pool fee + impacto desprezível em $150 trade vs ~$200M TVL).

**Algoritmo:**
1. Read `pool.slot0` → `sqrt_price` → `p`
2. Read `beefy_strategy.range()` → `tick_lower`, `tick_upper` → `p_a`, `p_b`
3. Compute split V3:
   ```
   ratio_weth_value = (√p − p/√p_b) / (2√p − √p_a − p/√p_b)
   amount_weth_target = V × ratio_weth_value
   amount_usdc_target = V − (V × ratio_weth_value)
   ```
4. Edge cases:
   - `p ≥ p_b` (preço acima do range): só USDC útil → `swap_amount = 0`, deposita só USDC
   - `p ≤ p_a` (preço abaixo): só WETH útil → swap TUDO pra WETH, deposita só WETH
5. Swap exact-output (`Uniswap V3 SwapRouter.exactOutputSingle`):
   - `tokenIn = USDC`, `tokenOut = WETH`, `fee = 500`
   - `amountOut = amount_weth_target` (exato)
   - `amountInMaximum = V × (1 + slippage_bps/10000)` (proteção)
6. Deposit com **balance real** lido após swap (não cálculo):
   - `balance_weth = WETH.balanceOf(wallet)`
   - `balance_usdc = USDC.balanceOf(wallet)`
   - `beefy_strategy.deposit(balance_weth, balance_usdc, min_shares=expected × 0.99)`

Beefy CLM aceita amounts não-perfeitos (rebalança internamente); precisão exata do swap não é necessária.

### 4. Skip swap no teardown (cost optimization)

Default: teardown NÃO converte WETH residual de volta pra USDC. Wallet fica com `X WETH + Y USDC`. Próximo start usa o que tem na wallet, swap só a *diferença* pra hit o ratio ótimo. Em steady-state (operações consecutivas), swap fica próximo de zero.

Botão "Cash out" disponível pra forçar swap WETH→USDC se usuário quiser sair totalmente da posição.

### 5. Slippage tolerance: 0.3% hardcoded

Folgado pro pool 0,05% mesmo em condições adversas. Configurável via `.env` (`SLIPPAGE_BPS=30` default), mas sem UI exposure no MVP.

### 6. Approve pattern: max approval one-time

Primeiro start aprova `uint256.max` pro Uniswap Router e pro Beefy Strategy (~120k gas economizado por start nos seguintes). Risco residual: smart contract exploits. Mitigação: usar contratos audited e battle-tested (Uniswap V3, Beefy CLM).

## Arquitetura

### Módulos novos

```
chains/
  executor.py          ← base: signing, gas estimation, tx submission, receipt waiting, retry, idempotency
  uniswap_executor.py  ← swap_exact_output, ensure_approval (USDC/WETH → router)
  beefy_executor.py    ← deposit, withdraw (extends existing chains/beefy.py read-only reader)

engine/
  lifecycle.py         ← bootstrap_operation, teardown_operation; state machine
  lp_math.py           ← compute_optimal_split (pure V3 math)

abi/
  erc20.json                     ← approve, balanceOf, allowance
  uniswap_v3_swap_router.json    ← exactOutputSingle, exactInputSingle
  beefy_clm_strategy_write.json  ← deposit, withdraw (estende o existente que tem só leitura)
```

### Componentes detalhados

#### `chains/executor.py` — base ChainExecutor

```python
class ChainExecutor:
    """Abstrai web3.py para signing + tx submission com retry + idempotency.

    Cada subclass (UniswapExecutor, BeefyExecutor) chama self._send_tx(fn_call)
    sem se preocupar com gas, nonce, retry.
    """

    def __init__(self, w3: AsyncWeb3, account: LocalAccount, ...): ...

    async def send_tx(
        self, contract_fn, *,
        gas_limit: int | None = None,
        idempotency_key: str | None = None,  # if set, dedupe via DB lookup
    ) -> str:
        """Submits tx, waits for receipt, returns tx_hash. Raises on revert."""

    async def wait_for_receipt(self, tx_hash: str, timeout: int = 180) -> receipt:
        """Used both for new txs and for resuming pending txs after crash."""

    async def estimate_gas(self, contract_fn) -> int: ...
    async def get_nonce(self) -> int: ...  # tracks pending nonces
```

Idempotência: se `idempotency_key` é passado, lifecycle persiste no DB antes de chamar; se já tem `tx_hash` registrado, executor wait pelo receipt em vez de re-submeter.

#### `chains/uniswap_executor.py` — swap

```python
class UniswapExecutor(ChainExecutor):
    def __init__(self, w3, account, router_address, ...): ...

    async def ensure_approval(self, token: str, amount: int, spender: str) -> str | None:
        """Returns tx_hash se precisou aprovar; None se allowance já é suficiente."""

    async def swap_exact_output(
        self, *,
        token_in: str, token_out: str, fee: int = 500,
        amount_out: int, amount_in_maximum: int,
        recipient: str, deadline: int,
    ) -> str: ...
        """Returns tx_hash. Raises se reverte."""

    async def swap_exact_input(  # used em teardown opcional WETH → USDC
        self, *, ...
    ) -> str: ...
```

#### `chains/beefy_executor.py` — deposit/withdraw

```python
class BeefyExecutor(ChainExecutor):
    def __init__(self, w3, account, strategy_address, ...): ...

    async def ensure_approval(self, token: str, amount: int) -> str | None: ...

    async def deposit(
        self, *, amount0: int, amount1: int, min_shares: int,
    ) -> str:
        """Deposits both tokens. Returns tx_hash. Reverts se min_shares não atingido."""

    async def withdraw(self, *, shares: int) -> str: ...
```

#### `engine/lp_math.py` — V3 split math

```python
def compute_optimal_split(
    *, p: float, p_a: float, p_b: float, total_value_usdc: float,
) -> tuple[float, float]:
    """Returns (amount_weth_target, amount_usdc_target) for total V em USDC.

    Edge cases:
        p >= p_b: returns (0, V)
        p <= p_a: returns (V/p, 0)
    """
```

#### `engine/lifecycle.py` — orchestration

```python
class OperationLifecycle:
    """Orquestra bootstrap (swap+deposit+hedge) e teardown (close+withdraw).

    State machine persistida no DB. Idempotente — safe pra retomar após crash.
    """

    def __init__(self, *, settings, hub, db, exchange, uniswap, beefy, pool_reader, beefy_reader): ...

    async def bootstrap(self, *, usdc_budget: float) -> int:
        """Returns operation_id. Cria operação, executa swap+deposit+hedge.

        Idempotente: se operação já existe in-flight, reume do estado salvo.
        """

    async def teardown(self, *, swap_to_usdc: bool = False) -> dict:
        """Cancela grade, fecha short, withdraw Beefy. Returns final breakdown."""

    async def resume_in_flight(self) -> None:
        """Chamado em startup. Lê operations com bootstrap_state != 'active' e != 'closed'.
        Resume da próxima step pendente."""
```

### State machine

```
operations.bootstrap_state ∈ {
    'pending',
    'approving',
    'swap_pending',     ← tx submitted, waiting receipt
    'swap_confirmed',
    'deposit_pending',
    'deposit_confirmed',
    'snapshot',
    'hedge_pending',
    'hedge_confirmed',
    'active',           ← terminal happy path; engine roda grade
    'teardown_grid_cancel',
    'teardown_short_close',
    'teardown_withdraw_pending',
    'teardown_withdraw_confirmed',
    'teardown_swap_pending',  ← only se swap_to_usdc=True
    'teardown_swap_confirmed',
    'closed',           ← terminal
    'failed',           ← terminal; manual recovery needed
}
```

### Schema DB additions

```sql
ALTER TABLE operations ADD COLUMN usdc_budget REAL;
ALTER TABLE operations ADD COLUMN bootstrap_state TEXT DEFAULT 'pending';
ALTER TABLE operations ADD COLUMN bootstrap_swap_tx_hash TEXT;
ALTER TABLE operations ADD COLUMN bootstrap_deposit_tx_hash TEXT;
ALTER TABLE operations ADD COLUMN teardown_withdraw_tx_hash TEXT;
ALTER TABLE operations ADD COLUMN teardown_swap_tx_hash TEXT;
```

Migration via `try/except ALTER TABLE` no `db.initialize()`, mesmo padrão das phases anteriores.

### Settings additions

```python
# config.py
uniswap_v3_router_address: str  # 0xE592427A0AEce92De3Edee1F18E0157C05861564 (Arbitrum)
uniswap_v3_quoter_v2_address: str  # opcional pra slippage estimation: 0x61fFE014bA17989E743c5F6cB21bF9697530B21e
usdc_token_address: str         # 0xaf88d065e77c8cC2239327C5EDb3A432268e5831 (native USDC Arbitrum)
weth_token_address: str         # 0x82aF49447D8a07e3bd95BD0d56f35241523fBab1
slippage_bps: int = 30          # 0.3% default
```

## Failure modes e mitigações

### F1 — Approve falha (gas insuficiente / RPC error)

- **Estado wallet:** inalterado
- **Mitigação:** retry 3× com backoff exponencial. Se falhar todas, `bootstrap_state='failed'`, alerta usuário, sem state on-chain pra desfazer.

### F2 — Swap reverte (slippage / price impact)

- **Estado wallet:** inalterado (Uniswap atômico). Allowance pode ter sido setado.
- **Mitigação:** retry 1× com slippage 1.5× original. Se falhar, alerta.

### F3 — Swap OK + deposit falha

- **Estado wallet:** WETH + USDC split (não 100% USDC)
- **Mitigação:**
  - Bot persiste `swap_confirmed` antes de tentar deposit
  - Retry deposit 3× com backoff
  - Se persiste, `bootstrap_state='failed'`, alerta com instrução: "Funds in wallet (X WETH, Y USDC). Click 'Retry deposit' or 'Refund' (swap back)."
  - UI expõe ambos os botões

### F4 — Deposit OK + baseline read falha

- **Estado wallet:** LP depositada
- **Mitigação:** retry leitura 5× com backoff (RPC transient). Se persiste >5min, alerta operacional.

### F5 — LP depositada + hedge falha (CRÍTICO)

- **Estado wallet:** LP exposta sem proteção (naked long ETH durante a janela)
- **Mitigação:**
  - Timeout agressivo no `place_long_term_order` (60s)
  - Retry 3× com prioridade alta (sem backoff longo)
  - Se persistir após ~3min, alerta crítico via webhook + auto-teardown opcional (configurável: `auto_teardown_on_hedge_fail=false` default)

### F6 — Crash mid-flow (qualquer step)

- **Mitigação:** `OperationLifecycle.resume_in_flight()` chamado em startup. Lê `operations` com state intermediário e:
  - Se `*_tx_hash` existe e não há receipt: `wait_for_transaction_receipt(timeout=180)`
  - Se receipt encontrado: avança state e continua
  - Se tx expired (mempool drop): re-submete da mesma step
  - Se nenhum tx_hash: re-executa step (idempotente porque on-chain state ainda não mudou)

### F7 — Reorg (Arbitrum reorgs raros mas possíveis)

- **Mitigação:** wait_for_receipt com `confirmations=2` para tx críticas. Aceita atraso de ~2-3s extra.

## Wallet & gas requirements

- Wallet precisa ter ETH para gas. Bot lê `wallet.balance` em startup; alerta se < 0.005 ETH (~$15 ao preço atual).
- Bot expõe `wallet.gas_balance_eth` no `/state` SSE pra UI mostrar.
- Gas estimado por start (5 txs no first start, 2 txs depois): ~600k gas total = ~$0.60 a 30 gwei.

## UI changes

### Modal "Start operation"

```
┌──────────────────────────────────────┐
│ Start new operation                  │
├──────────────────────────────────────┤
│ Capital (USDC):  [   300.00   ] $   │
│                  [Use wallet max]    │
│                                      │
│ Preview at p=$3,000:                 │
│   Swap: $138 USDC → 0.046 WETH       │
│   Deposit: 0.046 WETH + $162 USDC    │
│   Open short: 0.046 ETH @ dYdX       │
│   Estimated gas: ~$0.60              │
│                                      │
│         [Cancel]    [Start]          │
└──────────────────────────────────────┘
```

### Operation card during bootstrap

```
┌─────────────────────────────────────────────┐
│ Operation #42 — Bootstrapping...            │
├─────────────────────────────────────────────┤
│ ✓ Approved USDC (tx: 0xabc... ↗)            │
│ ✓ Swapped (tx: 0xdef... ↗)                  │
│ ⏳ Depositing in Beefy CLM...                │
│   Hedging short on dYdX...                  │
│                                             │
│ [Cancel] (only valid before deposit)        │
└─────────────────────────────────────────────┘
```

Cada `tx_hash` é link clicável para Arbiscan.

### Settings additions

- "Slippage tolerance" input (default 0.3%, range 0.1–1.0%)
- "Cash out remaining WETH" button (manual `swap_exact_input(WETH→USDC, full_balance)`)
- "Wallet ETH balance" display + warning se baixo

### Operation card during teardown

```
┌─────────────────────────────────────────────┐
│ Operation #42 — Closing...                  │
├─────────────────────────────────────────────┤
│ ✓ Cancelled grid                            │
│ ✓ Closed short                              │
│ ⏳ Withdrawing from Beefy CLM...             │
│ Final PnL pending...                        │
└─────────────────────────────────────────────┘
```

## Testing strategy

### Unit tests (puro)

- `tests/test_lp_math.py`: V3 split, edge cases (out of range, exact boundary), regression numérica vs cálculo manual
- `tests/test_chain_executor.py`: signing, gas estimation, retry logic, idempotency com mock w3
- `tests/test_uniswap_executor.py`: build correto de tx (params), revert handling
- `tests/test_beefy_executor.py`: deposit/withdraw construction, allowance check

### Integration tests (mocked)

- `tests/test_lifecycle.py`: end-to-end bootstrap + teardown com chain mock
- `tests/test_lifecycle_recovery.py`: simula crash em cada step, valida resume

### Optional fork tests (Anvil/Hardhat)

- Run local Anvil fork de Arbitrum mainnet
- Bot conecta no fork, executa bootstrap real contra contratos reais (Uniswap V3, Beefy CLM)
- Custo: zero (Anvil local), tempo: ~30s por test
- Valida que ABIs e calldata estão corretos sem queimar gas real

## Tasks estimadas

```
T0  - Schema DB migration: ALTER TABLE operations + 5 colunas novas + bootstrap_state default
T1  - engine/lp_math.py: compute_optimal_split + tests numéricos (~10 cases)
T2  - chains/executor.py: ChainExecutor base com signing/gas/retry/idempotency
T3  - abi/erc20.json + abi/uniswap_v3_swap_router.json + chains/uniswap_executor.py
T4  - abi/beefy_clm_strategy_write.json + chains/beefy_executor.py
T5  - engine/lifecycle.py: bootstrap state machine (approve → swap → deposit → snapshot → hedge → active)
T6  - engine/lifecycle.py: teardown state machine (cancel grid → close short → withdraw → optional swap)
T7  - engine/lifecycle.py: resume_in_flight (recovery em startup)
T8  - Refactor engine/__init__.py: GridMakerEngine.start_operation/stop_operation routem via lifecycle
T9  - REST API: POST /operations/start aceita {usdc_budget}; POST /operations/cashout
T10 - UI: modal start com input + preview; operation card progress + tx hashes Arbiscan links
T11 - UI: settings (slippage_bps display); botão "Cash out remaining"; wallet gas balance display
T12 - tests/test_lifecycle.py: integration end-to-end com chain mock
T13 - tests/test_lifecycle_recovery.py: crash recovery scenarios
T14 - (opcional) Anvil fork test
T15 - Tag fase-2.0-completa + CLAUDE.md update
```

15 tasks total (14 obrigatórias + 1 opcional).

## Riscos

| Risco | Mitigação |
|---|---|
| ABI da Beefy CLM strategy diferente do esperado | T4 começa explorando ABI on-chain via Etherscan; documenta formato real |
| Wallet sem ETH pra gas no momento do start | Validação em `lifecycle.bootstrap()` — verifica `wallet.balance > 0.005 ETH`, recusa start com alerta |
| Reorg na Arbitrum afeta tx_hash idempotency | `confirmations=2` em wait_for_receipt; raro |
| TWAP check da Beefy reverte deposit | Swap pequeno na pool deep não move TWAP; tested via Anvil fork no T14 |
| `wallet_private_key` exposta acidentalmente | Continua só via `.env`, nunca via UI; nenhum endpoint REST expõe; nunca logada |
| Bug no state machine deixa operação travada | Botão "Force close to FAILED" no UI permite recovery manual; alerta em alertas críticos |
| Gas spike (Arbitrum normalmente baixo mas...) | Bot usa `eth_gasPrice + 10%` máximo; recusa tx se gas > $5 (configurável) |

## Critérios de aceitação

1. `POST /operations/start {usdc_budget: 300}` executa swap + deposit + hedge atomicamente, marca operação `ACTIVE`
2. `POST /operations/stop` cancela grade + fecha short + withdraw Beefy, marca operação `CLOSED`
3. Crash entre steps + restart: `resume_in_flight()` continua de onde parou (validado por testes)
4. Custo round-trip steady-state ≤ 0,1% do capital ($0,30 em $300)
5. UI mostra progress steps + Arbiscan tx links durante bootstrap/teardown
6. Settings expõe slippage_bps; botão "Cash out remaining"
7. Wallet ETH < 0.005 → start recusa com alerta operacional
8. Tests: ≥10 numéricos pra `lp_math`; ≥5 para cada executor; ≥3 cenários crash recovery
9. (opcional) Anvil fork test passa com contratos reais

## Arquivos novos

- `chains/executor.py`
- `chains/uniswap_executor.py`
- `chains/beefy_executor.py`
- `engine/lifecycle.py`
- `engine/lp_math.py`
- `abi/erc20.json`
- `abi/uniswap_v3_swap_router.json`
- `abi/beefy_clm_strategy_write.json`
- `tests/test_lp_math.py`
- `tests/test_chain_executor.py`
- `tests/test_uniswap_executor.py`
- `tests/test_beefy_executor.py`
- `tests/test_lifecycle.py`
- `tests/test_lifecycle_recovery.py`

## Arquivos modificados

- `config.py` — settings novos (router, quoter, USDC, WETH, slippage_bps)
- `db.py` — migration ALTER TABLE operations
- `engine/__init__.py` — start_operation/stop_operation route via lifecycle
- `engine/operation.py` — adicionar bootstrap_state ao Operation dataclass
- `state.py` — adicionar `wallet_eth_balance`, `bootstrap_progress` (string descritivo)
- `app.py` — instanciar lifecycle + executors no startup; chamar `resume_in_flight()` em lifespan
- `web/routes.py` — `start_operation` aceita JSON body com `usdc_budget`; novo `cashout` endpoint
- `web/templates/dashboard.html` + `partials/operation.html` — progress view com tx hashes
- `web/templates/partials/settings.html` — slippage display, cash out button
- `web/static/app.js` — modal start input, progress display
- `.env.example` — vars novas
- `requirements.txt` — eth_account já presente; verificar dependências
- `CLAUDE.md` — note sobre Phase 2.0 completa

## Convenções (mantidas)

- TDD: failing test → impl → green → commit
- `feat(task-N):`, `fix(task-N):`, `test(task-N):`, `docs(task-N):`, `chore:`
- Branch: `feature/onchain-execution`
- Tag: `fase-2.0-completa`
