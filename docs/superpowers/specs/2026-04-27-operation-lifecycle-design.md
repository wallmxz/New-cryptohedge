# Fase 1.2 — Operation Lifecycle + PnL por operação

## Objetivo

Adicionar ciclo de vida explícito de operação ao bot, com PnL por operação detalhado (LP fees, IL natural, hedge PnL, funding, perp fees, slippage), persistência em DB, e UI de controle. Limpar código legacy não usado.

**Fora do escopo desta fase:**
- Auto-deleverage e auto-emergency-close (apenas alerts via webhook, conforme já implementado na 1.1)
- Swap automático e deposit/withdraw on-chain (Fase 1.3)
- Adaptive grid spacing e backtesting (fases posteriores)

## Modelo conceitual

### Operation

Entidade com ciclo de vida explícito:

```
NONE ──[user clicks Iniciar]──> STARTING ──[bootstrap done]──> ACTIVE
                                                                  │
                                                  [user clicks Encerrar]
                                                                  ↓
                                                              STOPPING
                                                                  ↓
                                                              CLOSED
```

- `NONE`: bot rodando mas sem operação ativa. Lê estado on-chain mas **não placea grade** nem mantém short. Modo stand-by.
- `STARTING`: usuário clicou "Iniciar". Bot grava baseline (preço, amounts, collateral) e executa bootstrap (taker pra abrir short inicial = `x(p_now) * hedge_ratio`).
- `ACTIVE`: estado normal. Grade rodando, fills entrando, PnL acumulando.
- `STOPPING`: usuário clicou "Encerrar". Bot cancela grade e fecha short com taker.
- `CLOSED`: encerrada, PnL final gravado, dashboard mostra resumo.

**Concorrência:** uma operação ativa por vez. Tentar iniciar uma nova quando há ativa → erro 409.

### Baseline

No transition `STARTING`, bot grava snapshot:
- `baseline_eth_price` — preço atual ETH/USDC
- `baseline_pool_value_usd` — valor da pool em USDC
- `baseline_amount0` — WETH na pool
- `baseline_amount1` — USDC na pool
- `baseline_collateral` — collateral atual na dYdX subaccount

Baseline serve como referência fixa para calcular deltas durante a operação.

### Acumuladores

Durante `ACTIVE`, atribuímos eventos à operação atual:
- Cada fill na perp → `fills.operation_id = op.id`, `op.perp_fees_paid += fill.fee`
- Cada funding payment → `op.funding_paid += amount`
- Cada `Harvest` da Beefy → `op.lp_fees_earned += harvested_amount`
- Bootstrap taker fee é gravado em `op.bootstrap_slippage`

### PnL breakdown

Computado em runtime para a UI:

```
LP fees earned         = op.lp_fees_earned (do Beefy Harvest)
Beefy perf fee         = -10% × LP fees earned
IL natural             = -(HODL_value − pool_value)  
                         onde HODL_value = baseline_amount0 × current_price + baseline_amount1
Hedge PnL              = realized_pnl + unrealized_pnl - baseline_realized_pnl
Funding                = op.funding_paid (signed)
Perp fees              = -op.perp_fees_paid
Bootstrap slippage     = -op.bootstrap_slippage
─────────────────────
Net operation PnL      = sum of above
```

Em uma operação delta-neutral perfeita, `IL natural + Hedge PnL ≈ 0` (cancelam-se). O resto (LP fees − perf fee − funding − perp fees − slippage) é o ganho líquido real.

## Arquitetura

### Módulos novos

| Módulo | Função |
|---|---|
| `engine/operation.py` | `Operation` dataclass, state machine `OperationState` enum, transition guards |
| `web/templates/partials/operation.html` | Card de operação ativa no dashboard |
| `web/templates/partials/history.html` | Aba histórico de operações |

### Módulos modificados

| Módulo | Mudança |
|---|---|
| `db.py` | nova tabela `operations`; `operation_id` em fills/grid_orders/order_log; helpers de start/stop/get_active/get_history |
| `state.py` | `current_operation_id`, `operation_state`, `operation_pnl_breakdown` |
| `engine/__init__.py` | `_iterate` respeita `operation_state`; `start_operation()`, `stop_operation()`; `_attribute_event(operation_id, event)` |
| `engine/pnl.py` | extender com `compute_operation_pnl(op, current_state)` retornando breakdown |
| `chains/beefy.py` | adicionar listener pra evento `Harvest` (atribui LP fees à operação ativa) |
| `web/routes.py` | `POST /operations/start`, `POST /operations/stop`, `GET /operations` (lista histórico), `GET /operations/current` |
| `web/templates/dashboard.html` | inclui partial operation.html no topo |
| `web/static/app.js` | estado de operação + ações |
| `app.py` | injetar engine.start_operation/stop_operation nos endpoints |

### Módulos a remover (cleanup legacy)

| Módulo | Motivo |
|---|---|
| `engine/hedge.py` | substituído por `engine/curve.py` + `engine/grid.py`; não importado em lugar nenhum após cleanup |
| `tests/test_hedge.py` | testa módulo deletado |
| `chains/evm.py` | substituído por `chains/uniswap.py` + `chains/beefy.py` |
| `tests/test_evm.py` | testa módulo deletado |
| `exchanges/hyperliquid.py` | decisão arquitetural (Fase 1.1): Hyperliquid descontinuado |
| Asserts em `tests/test_exchanges.py` que dependem de Hyperliquid | manter os asserts de `Order`, `Fill`, `Position` (genéricos) |

## DB schema

```sql
CREATE TABLE operations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    ended_at REAL,
    status TEXT NOT NULL,  -- "starting", "active", "stopping", "closed", "failed"
    
    baseline_eth_price REAL,
    baseline_pool_value_usd REAL,
    baseline_amount0 REAL,
    baseline_amount1 REAL,
    baseline_collateral REAL,
    
    perp_fees_paid REAL DEFAULT 0,
    funding_paid REAL DEFAULT 0,
    lp_fees_earned REAL DEFAULT 0,
    bootstrap_slippage REAL DEFAULT 0,
    
    final_net_pnl REAL,
    close_reason TEXT  -- "user", "engine_stop"
);

CREATE INDEX IF NOT EXISTS idx_operations_active ON operations(status)
    WHERE status IN ('starting', 'active', 'stopping');
```

ALTER existing tables to add `operation_id INTEGER` (nullable) FK to `operations(id)`:
- `fills`
- `grid_orders`
- `order_log`

Eventos antes da Fase 1.2 não terão `operation_id` (ficam NULL). Eventos depois são atribuídos à operação ativa no momento.

## Fluxos

### Start operation

```
on POST /operations/start:
    1. Verifica: get_active_operation() == None  (senão 409)
    2. Lê estado atual on-chain: p_now, my_amount0, my_amount1, dydx_collateral
    3. INSERT operations (status=starting, baselines)
    4. hub.current_operation_id = new_id
    5. hub.operation_state = STARTING
    6. Bootstrap: taker buy ou sell pra atingir target_short = x(p_now) * hedge_ratio
       - registra taker fee em op.bootstrap_slippage
       - atribui o fill à operação
    7. UPDATE operations SET status='active'
    8. hub.operation_state = ACTIVE
    9. Engine loop volta a placear grade
    Return: 201 Created with operation id
```

### Stop operation

```
on POST /operations/stop:
    1. op = get_active_operation()
    2. UPDATE op.status = 'stopping'
    3. Cancel all grid orders (batch_cancel via DB list)
    4. Get current short, close via taker
       - registra fee em op.perp_fees_paid
       - atribui o fill à operação
    5. Compute final_net_pnl = compute_operation_pnl(op, current_state)
    6. UPDATE op.status='closed', ended_at=now, final_net_pnl, close_reason='user'
    7. hub.current_operation_id = None
    8. hub.operation_state = NONE
    9. Engine loop volta a stand-by (lê estado, não placea)
    Return: 200 OK with final pnl breakdown
```

### Engine main loop

```
async def _iterate(self):
    # 1. Always read on-chain state (mesmo em NONE)
    beefy_pos = read_position()
    p_now = read_price()
    update hub.* (range, L, pool_value, etc.)
    
    # 2. Only proceed with grid logic if operation is ACTIVE
    if hub.operation_state != ACTIVE:
        return
    
    # 3. Existing logic: out-of-range handling, grid diff, place/cancel
    ...
```

### Beefy Harvest event listener

Beefy CLM strategies emit `Harvest(uint256 token0Collected, uint256 token1Collected)` (ou similar — verificar ABI exata) quando coletam fees do Uniswap V3 e fazem compound.

```
on Harvest event:
    op = get_active_operation()
    if op is None:
        ignore  # bot in standby
    
    # Convert collected to USD value
    usd_value = token0Collected * p_now + token1Collected
    op.lp_fees_earned += usd_value
    UPDATE operations
```

Implementação: na Fase 1.2 fazemos polling dos eventos Harvest a cada N blocos (ou a cada loop principal). Subscription via WebSocket fica para Fase 1.3+.

## API endpoints

```
POST /operations/start
    Returns 201 with {id, status: "active"} if started
    Returns 409 if active operation exists

POST /operations/stop
    Returns 200 with {id, status: "closed", final_net_pnl, breakdown}
    Returns 404 if no active operation

GET /operations/current
    Returns 200 with {id, status, started_at, baseline, current_pnl_breakdown}
    Returns 204 if no active operation

GET /operations?limit=20
    Returns 200 with [{id, started_at, ended_at, final_net_pnl, status}, ...]
```

## UI changes

### Painel — novo card "Operação atual" no topo

Quando `state.operation_state == "none"`:
```
┌────────────────────────────────────────────┐
│  Operação                                  │
│  Nenhuma operação ativa                    │
│                            [Iniciar]       │
└────────────────────────────────────────────┘
```

Quando `state.operation_state == "active"`:
```
┌────────────────────────────────────────────┐
│  Operação ativa  •  iniciada há 2h 14min   │
│                                            │
│  LP fees recebidas      +$2.10             │
│  Beefy perf fee         -$0.21             │
│  IL natural             -$8.05             │
│  Hedge PnL              +$8.10             │
│  Funding ARB            +$0.45             │
│  Perp fees              -$0.18             │
│  Bootstrap slippage     -$0.07             │
│  ──────────────────────────────            │
│  Net PnL                +$2.14             │
│                                            │
│                   [Encerrar operação]      │
└────────────────────────────────────────────┘
```

Botão "Encerrar" abre modal de confirmação ("Tem certeza? Vai cancelar grade e fechar short com taker, custo ~$X").

### Aba "Histórico"

Lista de operações fechadas, mais recente primeiro:

```
#7  12-15 jan  •  3d 4h  •  +$23.40 net  ✓
#6  09-11 jan  •  1d 18h •  -$2.10 net  ✓
#5  05-08 jan  •  2d 22h •  +$15.80 net ✓
```

Click em uma operação expande pra mostrar o breakdown completo + lista de fills da operação.

## Tests

### Unit
- `tests/test_operation.py` — state machine transitions, baseline snapshot, PnL breakdown calc
- Extender `tests/test_db.py` — operations table, FKs

### Integration
- `tests/test_integration_operation.py` — full lifecycle:
  - start_operation com mocks → status=active, bootstrap fired
  - simulate fill durante active → atribuído à operação
  - stop_operation → status=closed, final_pnl computed
  - try start while active → 409
  - try stop without active → 404

## Acceptance criteria

1. Usuário inicia uma operação via UI; bot bootsrapa o short e começa a placear grade
2. Durante operação ativa, todos os fills, funding e LP fees são atribuídos à operação
3. PnL breakdown na UI atualiza em <1s após cada fill
4. Usuário encerra operação via UI; bot cancela grade e fecha short
5. Operação fechada aparece na aba Histórico com PnL final
6. Tentar iniciar operação enquanto outra está ativa retorna 409 (UI mostra erro)
7. Engine não placea ordens quando `operation_state != ACTIVE`
8. Após cleanup: `engine/hedge.py`, `chains/evm.py`, `exchanges/hyperliquid.py` e respectivos testes deletados; suite continua verde

## Riscos

| Risco | Mitigação |
|---|---|
| Beefy não emite evento `Harvest` (ou nome diferente) | Verificar empíricamente no plan; se ausente, calcular LP fees como diff entre `pool_value_usd` e `baseline_amount0 * p_now + baseline_amount1` (delta menos IL menos Hedge PnL) |
| Stop com erro a meio caminho (cancelou grade mas não fechou short) | Operation entra em `failed`; ao reiniciar bot, recovery tenta fechar short remanescente; UI permite retry manual |
| Bot down durante operação ativa | Ao reiniciar, lê última operação `active` ou `starting`/`stopping` → reconciler resolve grade; estado da operação fica `active` (operação não trava) |
| Histórico cresce sem limite | Paginação no GET /operations; índice em `ended_at` |
| FK `operation_id` em fills antigos é NULL | Aceitar NULL como "pré-fase 1.2"; UI ignora esses |
