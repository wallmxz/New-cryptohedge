# Cross-pair Dual-Leg Hedge — Design Spec

**Date:** 2026-05-04
**Status:** Draft (pending user review)
**Branch:** `feature/cross-pair-dual-hedge`
**Depends on:** `cleanup/code-review-pass` merged into master (or rebased onto it)

## Goal

Estender o bot pra suportar **LP cross-pair** (token0 e token1 ambos voláteis, ex: ARB/WETH em pool Uniswap V3 0,30% via Beefy CLM) com **hedge dual-leg** — short em duas perps simultâneas na dYdX, uma pra cada token do par.

A estratégia de execução é **level-triggered taker** (sem grid pré-postada, sem threshold artificial): bot faz polling do `p` (razão ARB/WETH) do pool a cada 1Hz, calcula `target_short` por perna via curva V3, dispara market order taker quando `|drift| × preço ≥ min_notional` da exchange.

## Decisões-chave

| Decisão | Escolha | Motivo |
|---|---|---|
| Par alvo inicial | ARB/WETH 0,30% Beefy CLM (vault `0x8bf7D47f...322968`) | Volátil → captura LP fees alta; ambos perps líquidos na dYdX; native do Arbitrum |
| Estratégia per-leg | Level-triggered taker (sem threshold artificial; min_notional é o filtro natural) | Drift bounded a 1 step entre disparos; código simples; OK pra volume pequeno (sem preocupação de slippage) |
| Tipo de ordem | Market order (taker) | Mesmo fee em todos os caminhos pra cross-pair (geometria força taker); simples e robusto |
| Pre-place orders no livro? | **Não** | Pré-postar viraria taker imediato (cruzaria spread). Reactivo é equivalente em fee, mais limpo |
| Skip uptime hardening | Sim, fazer cross-pair primeiro | Decisão explícita do usuário; uptime vira fase futura |
| Capital wrapper | Beefy CLM continua sendo o wrapper (não LP V3 NFT direto) | Reusa toda a infra `chains/beefy.py`; auto-rebalance gratuito |
| Backtest dual-leg antes de mainnet | Sim | Validar PnL do par específico antes de soltar capital |

## 1. Arquitetura

```
┌─ engine/__init__.py (GridMakerEngine)
│  └─ _iterate() itera por leg ativa:
│     ├─ ARB leg: target = x(p), short ARB-USD
│     └─ ETH leg: target = y(p), short ETH-USD (cross-pair only)
│
├─ engine/curve.py — compute_x() e compute_y() já existem; usadas POR PERNA
├─ engine/pnl.py — breakdown ganha campos _token0 / _token1
├─ engine/lifecycle.py — bootstrap/teardown abrem/fecham 2 shorts
├─ chains/uniswap.py — read pool p (sem mudança)
├─ chains/beefy.py — read CLM position (sem mudança)
├─ exchanges/dydx.py — get_oracle_prices() novo método
├─ config.py — Settings ganha dydx_symbol_token0/token1
└─ engine/pair_factory.py — aceita cross-pair quando ambos perps ativos
```

**Princípio:** o motor permanece single-loop a 1Hz. Cada iteração faz polling de duas posições + dois oracle prices + um pool ratio. Em mode single-leg (`dydx_symbol_token1 == ""`) o comportamento é idêntico ao Phase 1.2 — backwards compat é preservada.

## 2. Settings + Pair Picker

### 2.1. Settings

Hoje (single-leg):
```python
dydx_symbol: str         # "ETH-USD"
```

Proposta (suporta ambos):
```python
dydx_symbol_token0: str         # "ARB-USD" — perp pra hedgear token0
dydx_symbol_token1: str         # "ETH-USD" — perp pra hedgear token1; "" = single-leg
```

Migration backwards-compat: `Settings.from_env` continua aceitando `DYDX_SYMBOL` como alias de `DYDX_SYMBOL_TOKEN0`, com `DYDX_SYMBOL_TOKEN1` defaultando vazio (= single-leg).

### 2.2. Pair Factory

`engine/pair_factory.py` hoje rejeita explícitamente cross-pairs:
```python
if not pair.get("is_usd_pair"):
    raise ValueError("requires Phase 3.x dual-leg hedge.")
```

Vira:
```python
if not pair.get("is_usd_pair"):
    perp1 = dydx_perp_for(pair["token1_symbol"])
    if perp1 is None or perp1 not in active_dydx_tickers:
        raise ValueError(
            f"Cross-pair {pair['vault_id']}: token1 {pair['token1_symbol']} "
            f"sem perp dYdX ativo, não suporta dual-leg."
        )
    pair_settings = dataclasses.replace(
        settings,
        dydx_symbol_token0=pair["dydx_perp"],
        dydx_symbol_token1=perp1,
        # ... outros campos do par
    )
```

Allowlist de decimals: hoje `{(18, 6)}`. Adicionar `(18, 18)` (ARB/WETH ambos 18 decimals). WBTC/WETH `(8, 18)` fica fora do MVP por enquanto.

### 2.3. DB cache (`beefy_pairs_cache`)

Adicionar coluna:
```sql
ALTER TABLE beefy_pairs_cache ADD COLUMN dydx_perp_token1 TEXT;
```

`chains/beefy_api.py::_extract_pair` popula `dydx_perp_token1` quando token1 tem perp ativa (cross-pair); fica null pra USD-pairs.

### 2.4. UI (`web/templates/partials/pair_picker.html`)

Cross-pairs hoje renderizam grayed-out com mensagem "Phase 3.x". Vira selectable quando o cache marca a pair com `dydx_perp_token1` populado e o decimals combo está na allowlist.

## 3. Engine flow (`_iterate` dual-leg)

Pseudocódigo do loop principal:

```python
async def _iterate(self):
    await self._maybe_reconcile()

    beefy_pos, p_now = await asyncio.gather(
        self._beefy_reader.read_position(),
        self._pool_reader.read_price(),
    )

    is_dual_leg = bool(self._settings.dydx_symbol_token1)
    symbols = [self._settings.dydx_symbol_token0]
    if is_dual_leg:
        symbols.append(self._settings.dydx_symbol_token1)

    positions, oracle_prices, collateral = await asyncio.gather(
        asyncio.gather(*[self._safe_get_position(s) for s in symbols]),
        self._exchange.get_oracle_prices(symbols),
        self._safe_get_collateral(),
    )

    p_a = tick_to_price(beefy_pos.tick_lower, dec0, dec1)
    p_b = tick_to_price(beefy_pos.tick_upper, dec0, dec1)

    if not (p_a < p_now < p_b):
        self._hub.out_of_range = True
        return  # idle: taker-only não tem grid pra cancelar
    self._hub.out_of_range = False

    if self._hub.operation_state != OperationState.ACTIVE.value:
        return

    L_user = compute_l_from_value(my_value, p_a, p_b, p_now)

    targets = {
        symbols[0]: compute_x(L_user, p_now, p_b) * self._hub.hedge_ratio,
    }
    if is_dual_leg:
        targets[symbols[1]] = compute_y(L_user, p_now, p_a) * self._hub.hedge_ratio

    await self._refresh_pnl_breakdown(...)
    await self._check_margin_and_alert(positions, oracle_prices)

    for symbol, target in targets.items():
        meta = await self._exchange.get_market_meta(symbol)
        idx = symbols.index(symbol)
        current = abs(positions[idx].size) if positions[idx] else 0.0
        await self._maybe_rebalance_leg(
            symbol=symbol, target=target, current=current,
            min_notional=meta.min_notional, ref_price=oracle_prices[symbol],
        )


async def _maybe_rebalance_leg(self, *, symbol, target, current, min_notional, ref_price):
    drift = target - current
    notional_drift_usd = abs(drift) * ref_price
    if notional_drift_usd < min_notional:
        return  # sub-level, idle

    side = "sell" if drift > 0 else "buy"
    size = abs(drift)
    cross_price = ref_price * (0.999 if side == "sell" else 1.001)
    cloid = self._next_cloid_for_leg(symbol)
    try:
        await self._exchange.place_long_term_order(
            symbol=symbol, side=side, size=size, price=cross_price,
            cloid_int=cloid, ttl_seconds=60,
        )
        op_id = self._hub.current_operation_id
        if op_id is not None:
            slippage_usd = 0.0005 * size * ref_price
            field = "perp_fees_paid_token0" if symbol == self._settings.dydx_symbol_token0 else "perp_fees_paid_token1"
            await self._db.add_to_operation_accumulator(op_id, field, slippage_usd)
    except Exception as e:
        logger.exception(f"Rebalance fire failed [{symbol}]: {e}")
        # Sem retry imediato — próximo iter (1s depois) tenta de novo
```

### 3.1. Polling architecture

Bot faz **polling**, não usa triggers nativos da dYdX. Razão: triggers da dYdX disparam em oracle USD price (ARB-USD ou ETH-USD), enquanto a condição que importa é "pool's `p` cruzou nível da curva V3" — que depende de ambos os preços simultaneamente. Apenas o bot, lendo o `slot0` do pool Uniswap, sabe disso.

Implicações:
- Bot precisa estar online (queda = hedge parado)
- Latência ≤ 1s entre evento e ação
- Recovery automática: se um fire falha, próximo iter detecta drift restante e tenta de novo

### 3.2. `get_oracle_prices()`

Novo método em `ExchangeAdapter`:
```python
async def get_oracle_prices(self, symbols: list[str]) -> dict[str, float]:
    """Returns {symbol: usd_price} for the given symbols."""
```

Implementação na `DydxAdapter`: read `/v4/perpetualMarkets` (já cached no `_market_metas`?) e extrair `oraclePrice` por market.

Implementação no `MockExchangeAdapter`: retorna prices controlados pelo simulador.

## 4. Lifecycle

### 4.1. Bootstrap dual-leg

```
[1] PRE-FLIGHT
    - Confere wallet ETH balance ≥ 0,005 (gas reserve)
    - Confere USDC balance ≥ usdc_budget
    - Lê pool: p_now, p_a, p_b

[2] CALCULA SPLIT (V3)
    - amount_token0_target = compute_x(L, p_now, p_b)
    - amount_token1_target = compute_y(L, p_now, p_a)
    - L dimensionado pra valor total = usdc_budget

[3] APPROVALS
    - USDC.approve(uniswap_router, MAX) — uma vez
    - token0.approve(beefy_strategy, MAX) — uma vez
    - token1.approve(beefy_strategy, MAX) — uma vez

[4] DOIS SWAPS (SEQUENCIAIS — mesma wallet, nonces diferentes)
    - swap_exact_output: USDC → token0   ← aguarda receipt antes do próximo
    - swap_exact_output: USDC → token1
    - NÃO podem ser paralelos: mesma wallet → nonces conflitantes na assinatura
    - Em cross-pair são 2 swaps separados (vs 1 do single-leg WETH/USDC)

[5] DEPOSIT BEEFY
    - beefy.deposit(amount0_real, amount1_real, min_shares=0)
    - Usa balance real pós-swap (slippage pode ter dado dust)

[6] SNAPSHOT BASELINE
    - Lê posição Beefy real
    - baseline_token0_usd_price = ARB-USD oracle
    - baseline_token1_usd_price = ETH-USD oracle
    - baseline_pool_value_usd = my_amount0 × ARB-USD + my_amount1 × ETH-USD

[7] ABRE OS DOIS SHORTS (PARALELO via asyncio.gather)
    - dydx.place_long_term_order(ARB-USD, sell, my_amount0 × hedge_ratio)
    - dydx.place_long_term_order(ETH-USD, sell, my_amount1 × hedge_ratio)
    - PARALELO funciona: dYdX usa client_id (cloid) único por subaccount, sem
      colisão de nonce; ambas as ordens são submetidas em paralelo e o exchange
      ordena via blockchain
    - Se UM falha: marca operação failed; o short que abriu fica aberto e
      precisa ser fechado manualmente via teardown ou via UI

[8] MARK ACTIVE
    - bootstrap_state = "active"
    - Engine começa a fazer level-triggered rebalance
```

### 4.2. Teardown dual-leg

```
[1] bootstrap_state = "stopping"
[2] Fecha os DOIS shorts (paralelo)
[3] beefy.withdraw(all_shares)
[4] (opcional) swap token0 → USDC e token1 → USDC
[5] Compute final PnL breakdown (com 2 hedge legs)
[6] bootstrap_state = "closed"
```

### 4.3. State machine

`bootstrap_state` enum estendido. Estados intermediários `_token0`/`_token1`
existem APENAS em cross-pair; single-leg pula direto pra `_done`.

```
pending
approving
swap_token0_pending      ← swap USDC→token0 em curso (sequencial)
swap_token0_done         ← swap1 OK, vai pro swap2
swap_token1_pending      ← swap USDC→token1 em curso
swaps_done               ← ambos OK (cross-pair) OU swap único OK (single-leg)
deposit_pending
deposit_done
snapshot
hedge_pending            ← abrindo shorts em paralelo (asyncio.gather)
hedge_done               ← ambos confirmados
active
stopping
teardown_close_pending   ← fechando shorts (paralelo)
teardown_close_done
teardown_withdraw_pending
teardown_withdraw_confirmed
teardown_swap_token0_pending  ← opcional: swap residual token0 → USDC
teardown_swap_token0_done
teardown_swap_token1_pending  ← opcional: swap residual token1 → USDC
teardown_swap_done
closed
failed
```

Para single-leg, os estados `swap_token1_*` e os `teardown_swap_token1_*` são
puláveis (não há token1 a ser swappado, USDC ou USDC já volta direto).

### 4.4. Recovery

`resume_in_flight()` atual: marca operações em estado intermediário como `failed` e exige intervenção manual (UI). Em cross-pair, mesmo MVP: estados intermediários novos (`swap_token0_done`, `hedge_token0_done`) também marcados como failed pra revisão. Implementação completa de retry vira fase futura.

## 5. PnL breakdown + state

### 5.1. Compute_operation_pnl extensão

Hoje retorna:
```
{lp_fees_earned, beefy_perf_fee, il_natural, hedge_pnl, funding,
 perp_fees_paid, bootstrap_slippage, net_pnl}
```

Vira (cross-pair):
```python
{
    "lp_fees_earned":            float,
    "beefy_perf_fee":            float,
    "il_natural":                float,    # USD LP - USD HODL com 2 oracle prices

    "hedge_pnl_token0":          float,
    "hedge_pnl_token1":          float,
    "hedge_pnl":                 float,    # soma (mantido pra compat)

    "funding_token0":            float,
    "funding_token1":            float,
    "funding":                   float,    # soma

    "perp_fees_paid_token0":     float,
    "perp_fees_paid_token1":     float,
    "perp_fees_paid":            float,    # soma

    "bootstrap_slippage":        float,
    "net_pnl":                   float,
}
```

Single-leg: campos `_token1` ausentes ou zerados; aggregates iguais ao Phase 1.2 (compat preservada).

### 5.2. IL natural

Single-leg formula:
```python
hodl_value = baseline_amount0 * current_eth_price + baseline_amount1
```

Dual-leg formula:
```python
hodl_value = (
    baseline_amount0 * current_token0_usd_price +
    baseline_amount1 * current_token1_usd_price
)
```

`il_natural = current_pool_value_usd - hodl_value` (mesmo sinal: positivo = ganho vs HODL).

### 5.3. State (`StateHub`)

Hoje single hedge:
```python
hedge_position: dict | None
hedge_unrealized_pnl: float
hedge_realized_pnl: float
funding_total: float
```

Vira dict por symbol + properties agregadas + UI legacy compat:
```python
hedge_positions: dict[str, dict] = {}        # por symbol; keys = symbols ativos
hedge_unrealized_pnls: dict[str, float] = {}
hedge_realized_pnls: dict[str, float] = {}
funding_totals: dict[str, float] = {}

@property
def hedge_position(self) -> dict | None:
    """Compat: retorna o primeiro hedge (single-leg) ou agregado simbólico
    pra UI legacy. Dual-leg-aware UI deve ler hedge_positions diretamente."""
    if not self.hedge_positions:
        return None
    return next(iter(self.hedge_positions.values()))

@property
def hedge_unrealized_pnl(self) -> float:
    return sum(self.hedge_unrealized_pnls.values())
@property
def hedge_realized_pnl(self) -> float:
    return sum(self.hedge_realized_pnls.values())
@property
def funding_total(self) -> float:
    return sum(self.funding_totals.values())
```

UI atual (`hedge_position`, `hedge_unrealized_pnl`, etc.) continua funcionando
via properties. Dashboard pode evoluir pra mostrar duas posições, mas isso
é opcional pro MVP — funciona renderizando só a primeira via legacy property.

### 5.4. DB schema

```sql
ALTER TABLE operations ADD COLUMN baseline_token0_usd_price REAL;
ALTER TABLE operations ADD COLUMN baseline_token1_usd_price REAL;
ALTER TABLE operations ADD COLUMN perp_fees_paid_token0 REAL DEFAULT 0;
ALTER TABLE operations ADD COLUMN perp_fees_paid_token1 REAL DEFAULT 0;
ALTER TABLE operations ADD COLUMN funding_paid_token0 REAL DEFAULT 0;
ALTER TABLE operations ADD COLUMN funding_paid_token1 REAL DEFAULT 0;
```

`baseline_eth_price` (existente) renomeado conceitualmente pra `baseline_token1_usd_price`; pra single-leg WETH/USDC é a mesma coisa.

`add_to_operation_accumulator` allowlist ganha as novas keys.

## 6. Backtest simulator dual-leg

### 6.1. Data layer (`backtest/data.py`)

Generalizar fetch:
```python
async def fetch_token_prices(
    self, *, symbol: str, start: float, end: float, interval: int = 300,
) -> list[tuple[float, float]]:
    """Coinbase candles pra symbol-USD. Reusa paginação atual; só parametriza product_id."""
```

ARB-USD, LDO-USD, GMX-USD, etc. estão todos disponíveis no Coinbase Exchange API.

Funding pra ambos os legs:
```python
funding_token0 = await fetcher.fetch_dydx_funding(symbol="ARB-USD", ...)
funding_token1 = await fetcher.fetch_dydx_funding(symbol="ETH-USD", ...)
```

### 6.2. SimConfig

```python
@dataclass
class SimConfig:
    # ... campos existentes ...
    dydx_symbol_token0: str
    dydx_symbol_token1: str   # vazio = single-leg
```

### 6.3. Simulator main loop

```python
for ts in time_axis:
    E = price_at(ts, token1_prices)        # ETH-USD
    P0 = price_at(ts, token0_prices)        # ARB-USD
    p_now = P0 / E                          # razão pool implícita

    await mock_pool.set_price(p_now)
    await mock_beefy.set_p(p_now)           # rebalance dinâmico via curva V3

    await mock_exchange.advance_to_prices({
        "ARB-USD": P0, "ETH-USD": E,
    }, ts=ts)

    apply_funding_if_due("ARB-USD", funding_token0, ts)
    apply_funding_if_due("ETH-USD", funding_token1, ts)

    await engine._iterate()
```

### 6.4. MockExchangeAdapter multi-symbol

Hoje single-symbol. Refator pra dict por symbol:
```python
def __init__(self, *, symbols: list[str], ...):
    self._positions: dict[str, _Position] = {s: _Position() for s in symbols}
    self._last_prices: dict[str, float] = {}
    self._open_orders: dict[str, dict[int, _OpenOrder]] = {}

async def get_position(self, symbol: str) -> Position | None: ...
async def place_long_term_order(self, *, symbol, ...): ...
async def get_oracle_prices(self, symbols: list[str]) -> dict[str, float]: ...
async def advance_to_prices(self, prices: dict[str, float], *, ts: float): ...
def stats(self) -> dict[str, dict]:  # por symbol + agregadas
```

Margin gate (5x collateral) considera notional somado das duas posições.

### 6.5. MockBeefyReader rebalance dinâmico

```python
class MockBeefyReader:
    def __init__(self, *, p_a, p_b, L, share):
        self._p_a, self._p_b, self._L, self._share = p_a, p_b, L, share
        self._p_now = (p_a + p_b) / 2

    def set_p(self, p_now: float):
        self._p_now = p_now

    async def read_position(self) -> _BeefyPosition:
        from engine.curve import compute_x, compute_y
        amount0 = compute_x(self._L, self._p_now, self._p_b)
        amount1 = compute_y(self._L, self._p_now, self._p_a)
        return _BeefyPosition(
            tick_lower=..., tick_upper=...,
            amount0=amount0, amount1=amount1,
            share=self._share, raw_balance=...,
        )
```

Resolve a inconsistência atual onde a posição da LP não rebalanceava. Beneficia também o single-leg.

### 6.6. Sweep cross-pair

`scripts/sweep_strategies.py` ganha `--cross-pair` flag. Roda 4 estratégias (taker, maker, none, topbook) em modo dual-leg. Tabela de output ganha colunas split por leg.

Tempo de execução: ~12-20 min total (4 configs × 3-5 min cada em 6 meses de dados).

## 7. Testing

### 7.1. Unit tests (~40-60 novos)

**Engine dual-leg core:**
- `_maybe_rebalance_leg`: dispara taker quando |drift|×preço ≥ min_notional, idle senão
- `_iterate` em modo dual-leg: 2 chamadas a `_maybe_rebalance_leg`
- `_iterate` em modo single-leg: comportamento idêntico ao Phase 1.2

**Settings + Pair Factory:**
- `Settings.from_env` com `DYDX_SYMBOL_TOKEN1` definido vs vazio
- `pair_factory.build_lifecycle` aceita cross-pair quando ambos perps ativos
- `pair_factory` rejeita cross-pair se token1 sem perp dYdX
- Backwards compat: USD-pair continua funcionando

**Lifecycle:**
- `bootstrap` cross-pair: 2 swaps + 2 short opens em paralelo
- `bootstrap` single-leg: comportamento atual preservado
- `teardown` cross-pair: 2 short closes + 1 withdraw + opcional 2 swaps
- Falhas parciais (swap2 falha após swap1 OK) marcam operação failed

**PnL:**
- Campos `_token0`/`_token1` em cross-pair
- Aggregates somam corretamente
- IL natural com dois oracle prices

**MockExchangeAdapter multi-symbol + MockBeefyReader rebalance dinâmico**

### 7.2. Integration tests (~5-10)

- Full bootstrap → run iterations → teardown
- Recovery de cada bootstrap_state intermediário
- Margin breach handling com 2 legs
- p atravessa N níveis: N×2 disparos
- Single-leg drift (ARB oscila, ETH constante): só ARB leg fires

### 7.3. Backtest integration

- ARB/WETH 6-month run completes sem exceções
- Single-leg WETH/USDC regression: mesmos números do iter 3 anterior
- PnL breakdown reproducible

### 7.4. Coverage target

- engine/* novo código: ≥90%
- backtest/* novos paths cross-pair: ≥80%

## Files affected

**Modificados:**
- `config.py` — novos campos `dydx_symbol_token0`/`token1`, com `DYDX_SYMBOL` alias
- `state.py` — `hedge_positions`/`hedge_unrealized_pnls` dicts + properties
- `db.py` — schema migration: 6 novas colunas em `operations`; `add_to_operation_accumulator` allowlist; `dydx_perp_token1` em `beefy_pairs_cache`
- `engine/__init__.py` — `_iterate` dual-leg; `_maybe_rebalance_leg` substitui `_aggressive_correct`; `_grid_strategy` removido (taker-only)
- `engine/lifecycle.py` — `bootstrap`/`teardown` dual-leg; estados novos
- `engine/pnl.py` — breakdown extendido com fields `_token0`/`_token1`
- `engine/pair_factory.py` — aceita cross-pair com perps validados
- `engine/curve.py` — sem mudança (math já generaliza)
- `chains/beefy_api.py` — popular `dydx_perp_token1` no cache
- `exchanges/base.py` — `get_oracle_prices()` na ABC
- `exchanges/dydx.py` — `get_oracle_prices()` impl
- `web/templates/partials/pair_picker.html` — cross-pairs selectable
- `backtest/exchange_mock.py` — multi-symbol, get_oracle_prices, advance_to_prices
- `backtest/chain_mock.py` — `MockBeefyReader.set_p` + rebalance dinâmico
- `backtest/data.py` — `fetch_token_prices(symbol)` generalizado
- `backtest/simulator.py` — dual-feed, p derivado de P0/E
- `backtest/__main__.py` — flags `--symbol-token0`/`--symbol-token1`
- `backtest/report.py` — colunas split por leg
- `scripts/sweep_strategies.py` — flag `--cross-pair`

**Novos:**
- `tests/test_engine_dual_leg.py`
- `tests/test_lifecycle_dual_leg.py`
- `tests/test_pnl_dual_leg.py`
- `tests/test_pair_factory_cross_pair.py`
- `tests/test_mock_exchange_multi_symbol.py`
- `tests/test_mock_beefy_dynamic_rebalance.py`
- `tests/test_simulator_dual_leg.py`
- `tests/test_settings_dual_leg.py`

**Não modificados:**
- `engine/curve.py` (math já generalize)
- `chains/uniswap.py` (read_price reutilizado)
- `chains/beefy.py` (read_position reutilizado)
- `chains/beefy_executor.py` / `chains/uniswap_executor.py` (deposit/withdraw genéricos)

## Open questions / out of scope

- **Uptime hardening** — fase futura (RPC fallback ativo, retry policy, heartbeat metric, UptimeRobot)
- **Auto-resume in-flight ops** — MVP marca como failed; fase futura implementa retry from state
- **WBTC/WETH (8, 18) decimals** — fora do MVP; implica generalizar mais a math
- **PEPE/WETH (decimals 18, 18 mas com 1% fee tier)** — ok depois de ARB/WETH validado
- **Testnet rehearsal** — pré-requisito antes de mainnet, não desta fase
- **Anvil fork test** pra validar ABIs Uniswap/Beefy contra contratos reais

## Estimativa de esforço

- Settings + pair factory: 0,5 dia
- Engine dual-leg: 1 dia
- Lifecycle: 1 dia
- PnL + state: 0,5 dia
- Backtest dual-leg: 1 dia
- Testes (unit + integration): 1,5 dia
- Backtest validation runs: 0,5 dia
- **Total: ~6 dias**

---

**Status:** Aguardando revisão do usuário antes de partir pro plan de implementação task-a-task.
