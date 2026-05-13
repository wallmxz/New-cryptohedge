# Predictive Grid Hedge v2 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Substituir taker chase reativo por grade de stop-limit orders pré-colocadas na Lighter, alinhadas aos ticks do pool Uniswap V3 que o Beefy CLM espelha. Cada fill corresponde 1:1 a um tick-cross no pool.

**Architecture:** Nova função `compute_grid_from_pool_ticks` em `engine/curve.py` gera níveis derivados do estado live da Beefy (`positionMain()`). Lighter wrapper expõe `place_stop_limit_order` (via `create_sl_limit_order` da SDK). Novo `_maintain_grid` no engine substitui `_maybe_rebalance_leg`, com lifecycle event-driven (rebuild on fill / composition drift / Beefy range change / range exit). Feature flag `PREDICTIVE_GRID_V2` permite rollback.

**Tech Stack:** Python 3.13/3.14, asyncio, lighter-sdk (zkLighter), web3.py, aiosqlite, pytest, Prometheus client.

**Spec:** `docs/superpowers/specs/2026-05-12-predictive-grid-v2-design.md`

---

## Phase A — Math & Foundations (PR 1)

Pode mergear independentemente. Sem mudança no engine loop ainda.

### Task A1: Helper `tick_to_human_price` em curve.py

**Files:**
- Test: `tests/test_predictive_grid_math.py`
- Modify: `engine/curve.py` (adicionar função no fim do módulo)

- [ ] **Step 1: Criar arquivo de teste com primeiro test failing**

`tests/test_predictive_grid_math.py`:
```python
import pytest
from math import isclose
from engine.curve import tick_to_human_price


def test_tick_to_human_price_arb_usdce_at_zero_tick():
    """Tick 0 → raw price = 1.0, ajustado pelos decimais.
    Para ARB(18)/USDC.e(6) onde ARB é token0:
    human = 1.0 * 10^(18-6) = 1e12. (preço raw é absurdo, mas matemática
    é consistente com Uniswap V3.)"""
    p = tick_to_human_price(tick=0, decimals0=18, decimals1=6)
    assert isclose(p, 1e12, rel_tol=1e-9)
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `python -m pytest tests/test_predictive_grid_math.py::test_tick_to_human_price_arb_usdce_at_zero_tick -v`
Expected: FAIL com `ImportError: cannot import name 'tick_to_human_price'`

- [ ] **Step 3: Implementar mínimo em `engine/curve.py`**

Adicionar no fim:
```python
def tick_to_human_price(*, tick: int, decimals0: int, decimals1: int) -> float:
    """Converte tick V3 pra preço human-readable (token1 por token0).

    Uniswap V3: raw_price = 1.0001^tick, em unidades raw (decimais bruto).
    Human-readable assume convenção do pool: token0 é o de address menor.
    human_price = raw * 10^(decimals0 - decimals1).

    Caller deve confirmar qual token é token0 no pool específico (depende
    de qual address é menor). Para ARB/USDC.e: ARB(0x912...) < USDC.e(0xFF9...)
    → ARB é token0, decimals0=18, decimals1=6.
    """
    raw = 1.0001 ** tick
    return raw * (10 ** (decimals0 - decimals1))
```

- [ ] **Step 4: Verificar test passa**

Run: `python -m pytest tests/test_predictive_grid_math.py -v`
Expected: PASS

- [ ] **Step 5: Adicionar tests pra preços realistas**

```python
def test_tick_to_human_price_arb_usdce_at_realistic_tick():
    """ARB a ~$0.14 em USDC.e: tick ~ ?
    1.0001^t * 10^12 = 0.14  →  t = log(0.14 / 1e12) / log(1.0001)
    t = log(1.4e-13) / log(1.0001) ≈ -296160
    """
    # Tick conhecido pra preço target
    target_price = 0.14
    # Cálculo direto, sem aproximação:
    from math import log
    expected_tick = int(log(target_price / 1e12) / log(1.0001))
    p = tick_to_human_price(
        tick=expected_tick, decimals0=18, decimals1=6,
    )
    # Tolerância: tick é int, então perde precisão (~0.01%)
    assert isclose(p, target_price, rel_tol=1e-3)


def test_tick_to_human_price_monotonic():
    """Tick maior → preço maior."""
    p_low = tick_to_human_price(tick=-296200, decimals0=18, decimals1=6)
    p_high = tick_to_human_price(tick=-296100, decimals0=18, decimals1=6)
    assert p_high > p_low
```

- [ ] **Step 6: Rodar tests novos**

Run: `python -m pytest tests/test_predictive_grid_math.py -v`
Expected: 3 PASS

- [ ] **Step 7: Commit**

```bash
git add tests/test_predictive_grid_math.py engine/curve.py
git commit -m "feat(curve): tick_to_human_price helper for V3 tick math"
```

---

### Task A2: `compute_grid_from_pool_ticks` — caso base

**Files:**
- Test: `tests/test_predictive_grid_math.py` (adicionar)
- Modify: `engine/curve.py` (adicionar função)

- [ ] **Step 1: Test do caso mínimo (range trivial)**

Adicionar em `tests/test_predictive_grid_math.py`:
```python
from engine.curve import compute_grid_from_pool_ticks, compute_l_from_value


def test_compute_grid_minimal_range():
    """Range muito estreito com 1 nível acima + 1 abaixo do tick_now.
    Verifica que gera exatamente 2 levels (um buy, um sell), spacing respeitado.
    """
    # Range simulado: [tick_lower=-296200, tick_upper=-296000], tick_now=-296100
    # tick_spacing=100 (não real, só pra testar)
    # L arbitrário pra ter sizes não-zero
    L = 1e15
    grid = compute_grid_from_pool_ticks(
        L=L,
        tick_lower=-296200,
        tick_upper=-296000,
        tick_spacing=100,
        tick_now=-296100,
        decimals0=18,
        decimals1=6,
        hedge_ratio=1.0,
        lighter_price_decimals=5,
        lighter_size_decimals=1,
    )
    # Esperado: ticks em [-296200, -296100, -296000] step 100
    # tick_now=-296100 é skipped; sobram -296200 (sell, abaixo) e -296000 (buy, acima)
    assert len(grid) == 2
    sells = [lv for lv in grid if lv.side == "sell"]
    buys = [lv for lv in grid if lv.side == "buy"]
    assert len(sells) == 1
    assert len(buys) == 1
    # Sell tem preço menor que buy
    assert sells[0].price < buys[0].price
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `python -m pytest tests/test_predictive_grid_math.py::test_compute_grid_minimal_range -v`
Expected: FAIL com ImportError

- [ ] **Step 3: Implementar `compute_grid_from_pool_ticks` mínima**

Em `engine/curve.py`, adicionar após `compute_target_grid`:
```python
def compute_grid_from_pool_ticks(
    *,
    L: float,
    tick_lower: int,
    tick_upper: int,
    tick_spacing: int,
    tick_now: int,
    decimals0: int,
    decimals1: int,
    hedge_ratio: float,
    lighter_price_decimals: int,
    lighter_size_decimals: int,
) -> list[GridLevel]:
    """Gera grade de níveis alinhada aos ticks ativos do pool V3.

    Cada nível corresponde a um tick boundary em [tick_lower, tick_upper]
    espaçado por tick_spacing. Skips tick_now (não tem ordem no mid).

    Size de cada nível = delta V3 token0 entre ticks adjacentes × hedge_ratio,
    arredondado pro step da Lighter.
    Price arredondado pro tick da Lighter.

    Caller garante:
      - tick_lower < tick_upper
      - tick_lower <= tick_now <= tick_upper
      - L > 0
    """
    if tick_lower >= tick_upper:
        return []
    if L <= 0:
        return []

    price_upper = tick_to_human_price(
        tick=tick_upper, decimals0=decimals0, decimals1=decimals1,
    )
    # Iterar ticks alinhados ao spacing
    # Começar do primeiro múltiplo de tick_spacing >= tick_lower
    first_aligned = tick_lower - (tick_lower % tick_spacing)
    if first_aligned < tick_lower:
        first_aligned += tick_spacing
    levels: list[GridLevel] = []
    prev_price = tick_to_human_price(
        tick=tick_lower, decimals0=decimals0, decimals1=decimals1,
    )
    prev_x = compute_x(L, prev_price, price_upper)
    t = first_aligned
    while t <= tick_upper:
        if t == tick_now:
            t += tick_spacing
            continue
        price_human = tick_to_human_price(
            tick=t, decimals0=decimals0, decimals1=decimals1,
        )
        price_rounded = round(price_human, lighter_price_decimals)
        x_at_t = compute_x(L, price_human, price_upper)
        delta = abs(prev_x - x_at_t)
        size = round(delta * hedge_ratio, lighter_size_decimals)
        if size > 0 and price_rounded > 0:
            side = "buy" if t > tick_now else "sell"
            levels.append(GridLevel(
                price=price_rounded,
                size=size,
                side=side,
                target_short=x_at_t * hedge_ratio,
            ))
        prev_x = x_at_t
        t += tick_spacing
    return sorted(levels, key=lambda lv: lv.price)
```

- [ ] **Step 4: Rodar test**

Run: `python -m pytest tests/test_predictive_grid_math.py -v`
Expected: 4 PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_predictive_grid_math.py engine/curve.py
git commit -m "feat(curve): compute_grid_from_pool_ticks core implementation"
```

---

### Task A3: `compute_grid_from_pool_ticks` — edge cases

**Files:**
- Test: `tests/test_predictive_grid_math.py` (adicionar)

- [ ] **Step 1: Test caso L=0**

```python
def test_compute_grid_returns_empty_when_L_zero():
    grid = compute_grid_from_pool_ticks(
        L=0.0, tick_lower=-296200, tick_upper=-296000,
        tick_spacing=100, tick_now=-296100,
        decimals0=18, decimals1=6, hedge_ratio=1.0,
        lighter_price_decimals=5, lighter_size_decimals=1,
    )
    assert grid == []
```

- [ ] **Step 2: Test caso tick_lower >= tick_upper**

```python
def test_compute_grid_returns_empty_when_inverted_range():
    grid = compute_grid_from_pool_ticks(
        L=1e15, tick_lower=-296000, tick_upper=-296200,  # inverted
        tick_spacing=100, tick_now=-296100,
        decimals0=18, decimals1=6, hedge_ratio=1.0,
        lighter_price_decimals=5, lighter_size_decimals=1,
    )
    assert grid == []
```

- [ ] **Step 3: Test caso tick_now fora do range**

```python
def test_compute_grid_with_tick_now_above_range():
    """Se tick_now > tick_upper, todos os ticks ficam abaixo → all sells."""
    grid = compute_grid_from_pool_ticks(
        L=1e15, tick_lower=-296200, tick_upper=-296000,
        tick_spacing=100, tick_now=-295900,  # acima do range
        decimals0=18, decimals1=6, hedge_ratio=1.0,
        lighter_price_decimals=5, lighter_size_decimals=1,
    )
    # Todos abaixo de tick_now → all sells
    assert all(lv.side == "sell" for lv in grid)


def test_compute_grid_with_tick_now_below_range():
    """tick_now < tick_lower, todos os ticks ficam acima → all buys."""
    grid = compute_grid_from_pool_ticks(
        L=1e15, tick_lower=-296200, tick_upper=-296000,
        tick_spacing=100, tick_now=-296300,  # abaixo
        decimals0=18, decimals1=6, hedge_ratio=1.0,
        lighter_price_decimals=5, lighter_size_decimals=1,
    )
    assert all(lv.side == "buy" for lv in grid)
```

- [ ] **Step 4: Test que sizes somam pra delta_x total**

```python
def test_compute_grid_sizes_conserve_delta_x():
    """Soma dos sizes (sem hedge_ratio) deve igualar x_at_tick_lower
    (porque ticks vão de tick_lower a tick_upper cobrindo toda a curva).
    """
    from engine.curve import compute_x
    L = 1e15
    tick_lower, tick_upper = -296200, -296000
    decimals0, decimals1 = 18, 6
    grid = compute_grid_from_pool_ticks(
        L=L, tick_lower=tick_lower, tick_upper=tick_upper,
        tick_spacing=10, tick_now=-296100,
        decimals0=decimals0, decimals1=decimals1, hedge_ratio=1.0,
        lighter_price_decimals=5, lighter_size_decimals=1,
    )
    price_lower = tick_to_human_price(
        tick=tick_lower, decimals0=decimals0, decimals1=decimals1,
    )
    price_upper = tick_to_human_price(
        tick=tick_upper, decimals0=decimals0, decimals1=decimals1,
    )
    expected_total_x = compute_x(L, price_lower, price_upper)
    actual_total_x = sum(lv.size for lv in grid)
    # Tolerância por rounding step da Lighter:
    assert abs(actual_total_x - expected_total_x) / expected_total_x < 0.05
```

- [ ] **Step 5: Rodar todos os tests**

Run: `python -m pytest tests/test_predictive_grid_math.py -v`
Expected: todos PASS

- [ ] **Step 6: Commit**

```bash
git add tests/test_predictive_grid_math.py
git commit -m "test(curve): edge cases for compute_grid_from_pool_ticks"
```

---

### Task A4: DB migration — `grid_orders` ganha `trigger_price`, `is_stop_order`

**Files:**
- Test: `tests/test_db_migrations.py` (criar se não existir)
- Modify: `db.py`

- [ ] **Step 1: Verificar schema atual**

Run: `python -c "import asyncio, db; asyncio.run(db.Database('/tmp/test.db').open())"` para criar DB de teste.
Run: `python -c "import sqlite3; c=sqlite3.connect('/tmp/test.db'); print([r for r in c.execute('PRAGMA table_info(grid_orders)')])"`
Expected: ver colunas existentes (cloid, side, price, size, status, etc) sem `trigger_price` e `is_stop_order`.

- [ ] **Step 2: Localizar a migration logic**

Run: `grep -n "CREATE TABLE.*grid_orders\|ALTER TABLE.*grid_orders" db.py`
Achar onde a tabela é criada / migrada.

- [ ] **Step 3: Adicionar test pra migration**

`tests/test_db_migrations.py` (criar se não existir, ou adicionar test):
```python
import pytest
import aiosqlite
from db import Database


@pytest.mark.asyncio
async def test_grid_orders_has_trigger_price_and_is_stop_order_cols(tmp_path):
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.open()
    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute("PRAGMA table_info(grid_orders)")
        cols = [r[1] for r in await cur.fetchall()]
    assert "trigger_price" in cols
    assert "is_stop_order" in cols
    await db.close()
```

- [ ] **Step 4: Rodar e ver falhar**

Run: `python -m pytest tests/test_db_migrations.py::test_grid_orders_has_trigger_price_and_is_stop_order_cols -v`
Expected: FAIL

- [ ] **Step 5: Implementar migration**

Em `db.py`, no método que faz CREATE TABLE / migrations, adicionar:
```python
# Migration: trigger_price + is_stop_order para grid_orders
async with self._conn.execute("PRAGMA table_info(grid_orders)") as cur:
    cols = {r[1] for r in await cur.fetchall()}
if "trigger_price" not in cols:
    await self._conn.execute(
        "ALTER TABLE grid_orders ADD COLUMN trigger_price REAL"
    )
if "is_stop_order" not in cols:
    await self._conn.execute(
        "ALTER TABLE grid_orders ADD COLUMN is_stop_order INTEGER NOT NULL DEFAULT 0"
    )
await self._conn.commit()
```

- [ ] **Step 6: Rodar test**

Run: `python -m pytest tests/test_db_migrations.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add db.py tests/test_db_migrations.py
git commit -m "feat(db): migration trigger_price + is_stop_order cols on grid_orders"
```

---

### Task A5: Estender `GridManager._level_key` pra incluir trigger_price

**Files:**
- Test: `tests/test_engine_grid.py` (já existe; adicionar)
- Modify: `engine/grid.py`

- [ ] **Step 1: Adicionar test**

Em `tests/test_engine_grid.py`, adicionar:
```python
from engine.curve import GridLevel
from engine.grid import GridManager


def test_level_key_distinguishes_stop_from_limit():
    """Stop order (com trigger_price) e limit order (sem) NO MESMO preço
    devem ter keys diferentes — não são a mesma ordem.
    """
    gm = GridManager()
    limit_lv = GridLevel(price=0.135, size=10.0, side="buy")
    stop_lv = GridLevel(price=0.135, size=10.0, side="buy")
    # No design atual, vão ter mesma key. Vamos adicionar campo distinguish:
    # GridLevel ganha um campo is_stop ou trigger_price (None = limit)
    # Por ora, esse test serve pra forçar a refactor.
    # (Test escrito assumindo que adicionamos GridLevel.trigger_price)
    # Mock: setar manualmente
    object.__setattr__(stop_lv, "trigger_price", 0.135)
    object.__setattr__(limit_lv, "trigger_price", None)
    diff = gm.diff(
        current=[("cloid1", limit_lv)],
        target=[stop_lv],
    )
    # Limit deve ser canceled, stop deve ser placed (são ordens distintas)
    assert "cloid1" in diff.to_cancel
    assert stop_lv in diff.to_place
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `python -m pytest tests/test_engine_grid.py::test_level_key_distinguishes_stop_from_limit -v`
Expected: FAIL (`AttributeError: trigger_price` ou keys iguais)

- [ ] **Step 3: Estender `GridLevel` em curve.py pra ter `trigger_price`**

Em `engine/curve.py`:
```python
@dataclass(frozen=True)
class GridLevel:
    price: float
    size: float
    side: Literal["buy", "sell"]
    target_short: float = 0.0
    # Stop order trigger price. None = regular limit order.
    # When set, place via stop-limit on Lighter (limit_price = trigger_price).
    trigger_price: float | None = None
```

E atualizar `compute_grid_from_pool_ticks` pra passar `trigger_price = price`:
```python
levels.append(GridLevel(
    price=price_rounded,
    size=size,
    side=side,
    target_short=x_at_t * hedge_ratio,
    trigger_price=price_rounded,  # stop-limit: trigger = price exato
))
```

- [ ] **Step 4: Estender `_level_key` em grid.py**

Em `engine/grid.py`:
```python
def _level_key(level: GridLevel) -> tuple:
    """Identity for matching grid levels (price + side + size + trigger, rounded)."""
    trigger = (
        round(level.trigger_price, 6)
        if level.trigger_price is not None else None
    )
    return (
        round(level.price, 6),
        level.side,
        round(level.size, 9),
        trigger,
    )
```

- [ ] **Step 5: Rodar tests**

Run: `python -m pytest tests/test_engine_grid.py -v`
Expected: PASS (inclusive os existentes — verificar que não quebrou nada)

- [ ] **Step 6: Commit**

```bash
git add engine/curve.py engine/grid.py tests/test_engine_grid.py
git commit -m "feat(grid): GridLevel.trigger_price + level_key distingue stop de limit"
```

---

### Task A6: Lighter wrapper — `place_stop_limit_order`

**Files:**
- Test: `tests/test_lighter_stop_orders.py` (criar)
- Modify: `exchanges/lighter.py`

- [ ] **Step 1: Localizar onde `place_long_term_order` está em lighter.py**

Run: `grep -n "place_long_term_order\|def place_" exchanges/lighter.py`
Identificar padrão de wrapping da SDK (como cloid é codificado, como base_amount é convertido pra raw, etc).

- [ ] **Step 2: Criar test inicial**

`tests/test_lighter_stop_orders.py`:
```python
import pytest
from unittest.mock import AsyncMock, MagicMock
from exchanges.lighter import LighterAdapter


@pytest.mark.asyncio
async def test_place_stop_limit_order_calls_sdk_with_correct_params(monkeypatch):
    """Verifica que place_stop_limit_order chama create_sl_limit_order
    com trigger_price == price (em raw units), is_ask correto,
    base_amount em raw units, etc.
    """
    adapter = LighterAdapter.__new__(LighterAdapter)  # bypass __init__
    adapter._signer = MagicMock()
    adapter._signer.create_sl_limit_order = AsyncMock(return_value=(MagicMock(), MagicMock(), None))
    # Mock cached market meta pra ARB-USD (market_id=50, size_dec=1, price_dec=5)
    meta = MagicMock()
    meta.market_index = 50
    meta.size_decimals = 1
    meta.price_decimals = 5
    adapter._market_meta_by_symbol = {"ARB-USD": meta}
    adapter._next_cloid_int = MagicMock(return_value=12345)

    await adapter.place_stop_limit_order(
        symbol="ARB-USD",
        side="sell",
        size=3.5,          # 3.5 ARB
        trigger_price=0.135,
        cloid_int=12345,
    )
    # SDK called with: base_amount=35 (3.5 * 10^1), trigger=13500 (0.135 * 10^5)
    call = adapter._signer.create_sl_limit_order.call_args
    assert call.kwargs["market_index"] == 50
    assert call.kwargs["base_amount"] == 35
    assert call.kwargs["trigger_price"] == 13500
    assert call.kwargs["price"] == 13500  # limit = trigger (exact)
    assert call.kwargs["is_ask"] is True  # sell
    assert call.kwargs["client_order_index"] == 12345
```

- [ ] **Step 3: Rodar e ver falhar**

Run: `python -m pytest tests/test_lighter_stop_orders.py -v`
Expected: FAIL com `AttributeError: 'LighterAdapter' object has no attribute 'place_stop_limit_order'`

- [ ] **Step 4: Implementar `place_stop_limit_order` em `exchanges/lighter.py`**

Adicionar método na classe `LighterAdapter`:
```python
async def place_stop_limit_order(
    self, *, symbol: str, side: str, size: float, trigger_price: float,
    cloid_int: int, reduce_only: bool = False,
) -> None:
    """Place a STOP_LOSS_LIMIT order with limit_price = trigger_price (exact).
    
    Para a grade predictive:
      - side='sell' + trigger abaixo do mark → fires when markPrice <= trigger
      - side='buy' + trigger acima do mark → fires when markPrice >= trigger
    Quando triggered, vira limit order em `price = trigger_price`. Fill exato
    no nível OR rest no book (sem slippage, sem markup).
    
    TIF default = 28-day expiry via SDK.
    """
    meta = self._market_meta_by_symbol.get(symbol)
    if meta is None:
        raise RuntimeError(f"Unknown market meta for {symbol}")
    base_amount_raw = int(round(size * (10 ** meta.size_decimals)))
    price_raw = int(round(trigger_price * (10 ** meta.price_decimals)))
    is_ask = (side == "sell")
    result = await self._signer.create_sl_limit_order(
        market_index=meta.market_index,
        client_order_index=cloid_int,
        base_amount=base_amount_raw,
        trigger_price=price_raw,
        price=price_raw,  # limit = trigger (exato)
        is_ask=is_ask,
        reduce_only=reduce_only,
    )
    # result tuple: (CreateOrder, RespSendTx, err_or_None)
    if result[2] is not None:
        raise RuntimeError(f"place_stop_limit_order failed: {result[2]}")
```

- [ ] **Step 5: Rodar test**

Run: `python -m pytest tests/test_lighter_stop_orders.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add tests/test_lighter_stop_orders.py exchanges/lighter.py
git commit -m "feat(lighter): place_stop_limit_order wrapper (limit=trigger, no slip)"
```

---

### Task A7: Lighter wrapper — `cancel_stop_order` + batch cancel

**Files:**
- Test: `tests/test_lighter_stop_orders.py` (adicionar)
- Modify: `exchanges/lighter.py`

- [ ] **Step 1: Test cancel single**

```python
@pytest.mark.asyncio
async def test_cancel_stop_order_calls_sdk():
    adapter = LighterAdapter.__new__(LighterAdapter)
    adapter._signer = MagicMock()
    adapter._signer.cancel_order = AsyncMock(return_value=(MagicMock(), None))
    meta = MagicMock()
    meta.market_index = 50
    adapter._market_meta_by_symbol = {"ARB-USD": meta}

    await adapter.cancel_stop_order(symbol="ARB-USD", order_index=987)
    adapter._signer.cancel_order.assert_called_once_with(
        market_index=50, order_index=987,
    )
```

- [ ] **Step 2: Test cancel-all (verificar SDK tem `cancel_all_orders`)**

Run: `python -c "import lighter; help(lighter.SignerClient.cancel_all_orders)"`
Confirmar signature (provavelmente: `cancel_all_orders(market_index=None, ...)` cancela todos do market ou todos da conta).

- [ ] **Step 3: Test cancel-all**

```python
@pytest.mark.asyncio
async def test_cancel_all_stops_calls_sdk():
    adapter = LighterAdapter.__new__(LighterAdapter)
    adapter._signer = MagicMock()
    adapter._signer.cancel_all_orders = AsyncMock(return_value=(MagicMock(), None))
    meta = MagicMock()
    meta.market_index = 50
    adapter._market_meta_by_symbol = {"ARB-USD": meta}

    await adapter.cancel_all_stops(symbol="ARB-USD")
    adapter._signer.cancel_all_orders.assert_called_once()
```

- [ ] **Step 4: Rodar e ver falhar**

Run: `python -m pytest tests/test_lighter_stop_orders.py -v`
Expected: 2 FAIL

- [ ] **Step 5: Implementar `cancel_stop_order` e `cancel_all_stops`**

Em `exchanges/lighter.py`:
```python
async def cancel_stop_order(self, *, symbol: str, order_index: int) -> None:
    meta = self._market_meta_by_symbol.get(symbol)
    if meta is None:
        raise RuntimeError(f"Unknown market meta for {symbol}")
    result = await self._signer.cancel_order(
        market_index=meta.market_index, order_index=order_index,
    )
    if result[1] is not None:
        raise RuntimeError(f"cancel_stop_order failed: {result[1]}")


async def cancel_all_stops(self, *, symbol: str) -> None:
    """Cancela TODAS as ordens (incluindo stops) do market do symbol.
    Usado quando Beefy reposiciona range e grade precisa ser rebuildada.
    """
    meta = self._market_meta_by_symbol.get(symbol)
    if meta is None:
        raise RuntimeError(f"Unknown market meta for {symbol}")
    result = await self._signer.cancel_all_orders(
        market_index=meta.market_index,
    )
    if result[1] is not None:
        raise RuntimeError(f"cancel_all_stops failed: {result[1]}")
```

- [ ] **Step 6: Rodar tests**

Run: `python -m pytest tests/test_lighter_stop_orders.py -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add tests/test_lighter_stop_orders.py exchanges/lighter.py
git commit -m "feat(lighter): cancel_stop_order + cancel_all_stops wrappers"
```

---

## Phase B — Engine Integration (PR 2)

### Task B1: Feature flag `PREDICTIVE_GRID_V2` no Settings

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Adicionar campo no `Settings` dataclass**

Em `config.py`, na dataclass:
```python
# Feature flag pra ativar o novo predictive grid v2.
# False (default): mantém path legacy taker chase (_maybe_rebalance_leg).
# True: usa _maintain_grid com stop-limit orders alinhadas aos ticks da pool.
predictive_grid_v2: bool = False
```

E no `from_env`:
```python
predictive_grid_v2=os.environ.get(
    "PREDICTIVE_GRID_V2", "false"
).lower() in ("true", "1", "yes"),
```

- [ ] **Step 2: Smoke test**

Run: `python -c "from config import Settings; import os; os.environ.setdefault('AUTH_USER','x'); os.environ.setdefault('AUTH_PASS','x'); os.environ.setdefault('WALLET_ADDRESS','0x0'); os.environ.setdefault('WALLET_PRIVATE_KEY','x'); os.environ.setdefault('ARBITRUM_RPC_URL','x'); os.environ.setdefault('CLM_VAULT_ADDRESS','0x0'); os.environ.setdefault('CLM_POOL_ADDRESS','0x0'); s = Settings.from_env(); print('predictive_grid_v2:', s.predictive_grid_v2)"`
Expected: `predictive_grid_v2: False`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat(config): PREDICTIVE_GRID_V2 feature flag (default false)"
```

---

### Task B2: Detecção de range change em `BeefyClmReader`

**Files:**
- Test: `tests/test_beefy.py` (já existe; adicionar)
- Modify: `chains/beefy.py`

- [ ] **Step 1: Verificar que `BeefyPosition` já tem `tick_lower`, `tick_upper`**

Run: `grep -n "tick_lower\|tick_upper" chains/beefy.py`
Já tem (linhas 18-19 do dataclass). Tem que também garantir que expõe `L` (liquidity).

- [ ] **Step 2: Adicionar `liquidity` no `BeefyPosition`**

Em `chains/beefy.py`:
```python
@dataclass
class BeefyPosition:
    tick_lower: int
    tick_upper: int
    amount0: float
    amount1: float
    share: float
    raw_balance: int
    liquidity: int = 0   # NEW: L da posição V3 (raw uint128)
```

- [ ] **Step 3: Popular `liquidity` em `read_position`**

`positionMain()` no contract ABI provavelmente retorna `(tickLower, tickUpper, liquidity, ...)` ou similar. Verificar o ABI:
Run: `grep -A 20 "positionMain" abi/beefy_clm_strategy.json | head -40`

Atualizar parsing de `position_main` em `read_position()` pra extrair liquidity. Se ABI retorna `(int24 tickLower, int24 tickUpper, uint128 liquidity)`:
```python
position_main = results[0]  # tuple do call
tick_lower, tick_upper, liquidity = position_main[0], position_main[1], position_main[2]
```

- [ ] **Step 4: Test que `liquidity` é populada**

Em `tests/test_beefy.py`, adicionar (com mock w3):
```python
@pytest.mark.asyncio
async def test_beefy_position_includes_liquidity(monkeypatch):
    """BeefyPosition.liquidity != 0 quando strategy.positionMain retorna L > 0."""
    # Setup mocking baseado em padrão existente do arquivo
    # ...
    pos = await reader.read_position()
    assert pos.liquidity > 0
```

(O mock setup completo depende dos fixtures existentes; adapte do padrão de `tests/test_beefy.py`.)

- [ ] **Step 5: Rodar tests**

Run: `python -m pytest tests/test_beefy.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add chains/beefy.py tests/test_beefy.py
git commit -m "feat(beefy): expose liquidity (L) in BeefyPosition for range-change detection"
```

---

### Task B3: `_maintain_grid` skeleton (no-op até wiring)

**Files:**
- Test: `tests/test_engine_maintain_grid.py` (criar)
- Modify: `engine/__init__.py`

- [ ] **Step 1: Criar test inicial — método existe, retorna sem fazer nada se flag desligado**

`tests/test_engine_maintain_grid.py`:
```python
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_maintain_grid_no_op_when_flag_disabled():
    """Sem PREDICTIVE_GRID_V2 ativado, _maintain_grid retorna sem fazer nada."""
    from engine import GridMakerEngine
    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.predictive_grid_v2 = False
    engine._exchange = AsyncMock()
    # _maintain_grid existe e é safe-no-op
    await engine._maintain_grid(
        beefy_pos=MagicMock(), p_now=0.14, oracle_prices={"ARB-USD": 0.14},
    )
    # Não chamou nada do exchange
    engine._exchange.place_stop_limit_order.assert_not_called()
    engine._exchange.cancel_stop_order.assert_not_called()
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `python -m pytest tests/test_engine_maintain_grid.py -v`
Expected: FAIL com `AttributeError: '_maintain_grid'`

- [ ] **Step 3: Adicionar skeleton em `engine/__init__.py`**

Na classe `GridMakerEngine`:
```python
async def _maintain_grid(
    self, *, beefy_pos, p_now: float, oracle_prices: dict[str, float],
) -> None:
    """Mantém grade de stop-limit orders alinhada aos ticks ativos da Beefy.

    Implementa o lifecycle event-driven descrito no spec
    (docs/superpowers/specs/2026-05-12-predictive-grid-v2-design.md, Sec 6):
      - Detecta mudança de range (tick_lower/upper/liquidity) → cancel-all + rebuild
      - Detecta drift de composição → rebuild
      - Detecta preço fora do range → cancel-all + idle
      - Insere próximo nível quando ordem fillha (handler separado em WS callback)

    No-op se feature flag PREDICTIVE_GRID_V2 desligada.
    """
    if not self._settings.predictive_grid_v2:
        return
    # TODO: próximas tasks implementam o corpo
    pass
```

- [ ] **Step 4: Rodar test**

Run: `python -m pytest tests/test_engine_maintain_grid.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_engine_maintain_grid.py
git commit -m "feat(engine): _maintain_grid skeleton (no-op when flag off)"
```

---

### Task B4: `_maintain_grid` — rebuild on range change (cancel-all + repost)

**Files:**
- Test: `tests/test_engine_maintain_grid.py` (adicionar)
- Modify: `engine/__init__.py`

- [ ] **Step 1: Test que detecta range change e dispara cancel_all + rebuild**

```python
@pytest.mark.asyncio
async def test_maintain_grid_rebuilds_on_range_change():
    """Quando tick_lower/upper/liquidity da Beefy diferem do que está
    posted, _maintain_grid chama cancel_all_stops + reposta tudo."""
    from engine import GridMakerEngine
    engine = GridMakerEngine.__new__(GridMakerEngine)
    engine._settings = MagicMock()
    engine._settings.predictive_grid_v2 = True
    engine._settings.dydx_symbol_token0 = "ARB-USD"
    engine._settings.token0_decimals = 18
    engine._settings.token1_decimals = 6
    engine._settings.hedge_ratio = 1.0
    engine._settings.uniswap_v3_pool_fee = 500  # tick_spacing 10
    engine._exchange = AsyncMock()
    engine._db = AsyncMock()
    engine._db.get_active_grid_orders = AsyncMock(return_value=[])
    engine._hub = MagicMock()
    # Estado posted "anterior" (sem grade ainda)
    engine._posted_grid_state = None

    beefy_pos = MagicMock()
    beefy_pos.tick_lower = -296200
    beefy_pos.tick_upper = -296000
    beefy_pos.liquidity = int(1e15)

    await engine._maintain_grid(
        beefy_pos=beefy_pos, p_now=0.14, oracle_prices={"ARB-USD": 0.14},
    )
    # Como não tinha grade posted, deve postar nova:
    assert engine._exchange.place_stop_limit_order.call_count > 0
```

- [ ] **Step 2: Rodar e ver falhar**

Run: `python -m pytest tests/test_engine_maintain_grid.py::test_maintain_grid_rebuilds_on_range_change -v`
Expected: FAIL

- [ ] **Step 3: Implementar lógica de rebuild on range change**

Em `engine/__init__.py`, no método `_maintain_grid`:
```python
async def _maintain_grid(
    self, *, beefy_pos, p_now: float, oracle_prices: dict[str, float],
) -> None:
    if not self._settings.predictive_grid_v2:
        return

    from engine.curve import compute_grid_from_pool_ticks
    from math import log

    # Compute current "geometry signature" do range Beefy + L
    current_sig = (
        beefy_pos.tick_lower, beefy_pos.tick_upper, beefy_pos.liquidity,
    )
    # Compara com geometry signature do grid posted
    posted_sig = getattr(self, "_posted_grid_signature", None)

    range_changed = (posted_sig != current_sig)

    if range_changed:
        # CANCEL ALL stops existentes
        try:
            await self._exchange.cancel_all_stops(
                symbol=self._settings.dydx_symbol_token0,
            )
        except Exception as e:
            logger.warning(f"cancel_all_stops failed in rebuild: {e}")
        # Limpa DB
        active_orders = await self._db.get_active_grid_orders()
        for row in active_orders:
            await self._db.mark_grid_order_cancelled(row["cloid"], time.time())

    # Se tem grade posted válida e nada mudou (no rebuild), sai
    if not range_changed:
        return

    # Build new grid
    fee_tier = self._settings.uniswap_v3_pool_fee
    tick_spacing_map = {500: 10, 3000: 60, 10000: 200}
    tick_spacing = tick_spacing_map.get(fee_tier, 10)

    # tick_now a partir de p_now (usando inverse)
    tick_now = int(log(p_now / (10 ** (self._settings.token0_decimals - self._settings.token1_decimals))) / log(1.0001))

    new_grid = compute_grid_from_pool_ticks(
        L=float(beefy_pos.liquidity),
        tick_lower=beefy_pos.tick_lower,
        tick_upper=beefy_pos.tick_upper,
        tick_spacing=tick_spacing,
        tick_now=tick_now,
        decimals0=self._settings.token0_decimals,
        decimals1=self._settings.token1_decimals,
        hedge_ratio=self._hub.hedge_ratio or self._settings.hedge_ratio,
        lighter_price_decimals=5,  # ARB-USD on Lighter
        lighter_size_decimals=1,
    )

    # Postar cada level como stop-limit
    for lv in new_grid:
        cloid = self._next_cloid_for_leg(self._settings.dydx_symbol_token0)
        try:
            await self._exchange.place_stop_limit_order(
                symbol=self._settings.dydx_symbol_token0,
                side=lv.side,
                size=lv.size,
                trigger_price=lv.trigger_price,
                cloid_int=cloid,
            )
            await self._db.insert_grid_order(
                cloid=cloid, side=lv.side, price=lv.price, size=lv.size,
                trigger_price=lv.trigger_price, is_stop_order=1,
                operation_id=self._hub.current_operation_id,
            )
        except Exception as e:
            logger.warning(f"place_stop_limit_order failed for level {lv.price}: {e}")

    self._posted_grid_signature = current_sig
```

- [ ] **Step 4: Rodar test**

Run: `python -m pytest tests/test_engine_maintain_grid.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py tests/test_engine_maintain_grid.py
git commit -m "feat(engine): _maintain_grid rebuilds on Beefy range change"
```

---

### Task B5: Handler de fill event — repor próximo nível

**Files:**
- Test: `tests/test_engine_maintain_grid.py` (adicionar)
- Modify: `engine/__init__.py` (no callback de fills)

- [ ] **Step 1: Localizar handler de fill events (WS callback)**

Run: `grep -n "_on_fill\|on_fill\|fill_event\|fill_handler" engine/__init__.py exchanges/lighter.py`
Achar onde fill events do WS são processados.

- [ ] **Step 2: Test — fill de um level → posta próximo tick adjacente**

```python
@pytest.mark.asyncio
async def test_fill_triggers_next_level_posting():
    """Quando um sell fillha em tick T, a engine posta um novo sell em
    tick T - tick_spacing (próximo abaixo). Idem buy → T + tick_spacing.
    """
    # Setup mock similar ao test anterior
    # ...
    # Simular fill do level em tick T
    # Verificar que place_stop_limit_order foi chamado com price = tick_to_human(T - spacing)
    pass  # implementação completa depende do handler de fill existente
```

- [ ] **Step 3: Implementar handler `_on_grid_fill`**

Em `engine/__init__.py`:
```python
async def _on_grid_fill(self, *, cloid: int, fill_price: float, fill_size: float, side: str) -> None:
    """Chamado quando uma ordem da grade fillou.
    
    Posta um novo nível no próximo tick na direção que faz sentido:
      - Sell fillou em tick T → próximo sell em T - tick_spacing (mais abaixo)
      - Buy fillou em tick T → próximo buy em T + tick_spacing (mais acima)
    
    Mantém densidade constante da grade.
    """
    if not self._settings.predictive_grid_v2:
        return
    # Lookup do level que fillou via cloid
    row = await self._db.get_grid_order(cloid)
    if row is None or not row.get("is_stop_order"):
        return
    await self._db.mark_grid_order_filled(cloid, time.time())
    
    # Compute próximo tick
    from math import log
    filled_tick = int(log(row["price"] / (10 ** (self._settings.token0_decimals - self._settings.token1_decimals))) / log(1.0001))
    fee_tier = self._settings.uniswap_v3_pool_fee
    tick_spacing_map = {500: 10, 3000: 60, 10000: 200}
    tick_spacing = tick_spacing_map.get(fee_tier, 10)
    
    if side == "sell":
        next_tick = filled_tick - tick_spacing
    else:
        next_tick = filled_tick + tick_spacing
    
    # Confirma que next_tick ainda está dentro do range posted
    sig = getattr(self, "_posted_grid_signature", None)
    if sig is None:
        return  # nada posted; _maintain_grid vai cuidar na próxima iter
    tick_lower, tick_upper, _L = sig
    if not (tick_lower <= next_tick <= tick_upper):
        return  # fora do range; não posta
    
    # Compute size do próximo level (mesma fórmula da grade original)
    # Simplificação MVP: usar mesmo size do fillado (delta de tick adjacente ≈ constante em range pequeno)
    new_cloid = self._next_cloid_for_leg(self._settings.dydx_symbol_token0)
    from engine.curve import tick_to_human_price
    new_price = round(tick_to_human_price(
        tick=next_tick,
        decimals0=self._settings.token0_decimals,
        decimals1=self._settings.token1_decimals,
    ), 5)
    
    try:
        await self._exchange.place_stop_limit_order(
            symbol=self._settings.dydx_symbol_token0,
            side=side,
            size=row["size"],
            trigger_price=new_price,
            cloid_int=new_cloid,
        )
        await self._db.insert_grid_order(
            cloid=new_cloid, side=side, price=new_price, size=row["size"],
            trigger_price=new_price, is_stop_order=1,
            operation_id=self._hub.current_operation_id,
        )
    except Exception as e:
        logger.warning(f"_on_grid_fill repost failed: {e}")
```

- [ ] **Step 4: Wire ao callback de WS fill events**

Procurar no callback existente de fills (em `exchanges/lighter.py` ou no engine) e adicionar chamada a `_on_grid_fill` quando o fill é de uma ordem da grade (verificar cloid no DB).

- [ ] **Step 5: Rodar tests**

Run: `python -m pytest tests/test_engine_maintain_grid.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add engine/__init__.py exchanges/lighter.py tests/test_engine_maintain_grid.py
git commit -m "feat(engine): _on_grid_fill reposts next-tick level after fill"
```

---

### Task B6: Wire `_maintain_grid` no iterate loop com feature flag

**Files:**
- Modify: `engine/__init__.py` (no método `_iterate`)

- [ ] **Step 1: Localizar onde `_maybe_rebalance_leg` é chamado**

Run: `grep -n "_maybe_rebalance_leg" engine/__init__.py`
Expected: ~linha 1202 do iterate (per spec Seção 1.1).

- [ ] **Step 2: Adicionar chamada condicional a `_maintain_grid`**

No `_iterate`, substituir o loop de `_maybe_rebalance_leg` por:
```python
if self._settings.predictive_grid_v2:
    # Predictive grid path
    await self._maintain_grid(
        beefy_pos=beefy_pos, p_now=p_now, oracle_prices=oracle_prices,
    )
else:
    # Legacy taker chase path
    for sym in symbols:
        idx = symbols.index(sym)
        current = abs(positions[idx].size) if positions[idx] else 0.0
        ref_price = oracle_prices.get(sym, 0.0)
        if ref_price <= 0:
            continue
        await self._maybe_rebalance_leg(
            symbol=sym, target=targets[sym], current=current,
            min_notional=self._settings.min_rebalance_notional_usd,
            ref_price=ref_price,
        )
```

- [ ] **Step 3: Smoke test — bot sobe com flag=false (legacy) e flag=true (novo)**

Run: `PREDICTIVE_GRID_V2=false python -m uvicorn app:app --host 127.0.0.1 --port 8001` (em outro shell)
Verificar: bot sobe, /health/engine retorna ok, _maybe_rebalance_leg path ativo (logs).

Kill, então:
Run: `PREDICTIVE_GRID_V2=true python -m uvicorn app:app --host 127.0.0.1 --port 8001`
Verificar: bot sobe sem crash, logs mostram "_maintain_grid called" se há op ativa.

- [ ] **Step 4: Rodar suite completa pra ver que nada quebrou**

Run: `python -m pytest tests/ -v 2>&1 | tail -20`
Expected: todos os tests existentes ainda passam.

- [ ] **Step 5: Commit**

```bash
git add engine/__init__.py
git commit -m "feat(engine): wire _maintain_grid in iterate loop behind PREDICTIVE_GRID_V2 flag"
```

---

## Phase C — Telemetry & UI (PR 3)

### Task C1: Prometheus metrics novas

**Files:**
- Modify: `engine/metrics.py`

- [ ] **Step 1: Adicionar counters/gauges/histograms**

Em `engine/metrics.py`:
```python
grid_stops_placed = Counter(
    "automoney_grid_stops_placed_total",
    "Total stop-limit orders postadas",
)
grid_stops_filled = Counter(
    "automoney_grid_stops_filled_total",
    "Total fills da grade predictive",
)
grid_stops_cancelled = Counter(
    "automoney_grid_stops_cancelled_total",
    "Cancelamentos por rebuild",
)
grid_fill_latency_ms = Histogram(
    "automoney_grid_fill_latency_ms",
    "Tempo entre trigger e fill (ms)",
    buckets=(100, 500, 1000, 5000, 10000, 30000, 60000, 120000, 300000),
)
grid_replication_error_pct = Gauge(
    "automoney_grid_replication_error_pct",
    "|posted - target| / target",
)
grid_rebuild_total = Counter(
    "automoney_grid_rebuild_total",
    "Rebuilds por motivo",
    ["reason"],  # fill | drift | range_change | range_exit
)
beefy_range_change_total = Counter(
    "automoney_beefy_range_change_total",
    "Quantas vezes Beefy reposicionou o CLM",
)
grid_levels_active = Gauge(
    "automoney_grid_levels_active",
    "Stops ativos no Lighter",
)
mark_vs_pool_drift_bps = Gauge(
    "automoney_mark_vs_pool_drift_bps",
    "|markPrice - poolPrice| em bps",
)
```

- [ ] **Step 2: Instrumentar `_maintain_grid` e `_on_grid_fill`**

- Cada `place_stop_limit_order` → `grid_stops_placed.inc()`
- Cada `cancel_all_stops` → `grid_stops_cancelled.inc(N)` (N = quantidade cancelada)
- Cada rebuild → `grid_rebuild_total.labels(reason="...").inc()`
- Cada range change detectado → `beefy_range_change_total.inc()`
- `grid_levels_active.set(len(posted_grid))`
- No `_on_grid_fill` → `grid_stops_filled.inc()`, calcular `(fill_ts - trigger_ts)*1000` e observe no histogram

- [ ] **Step 3: Smoke test — métricas aparecem em /metrics**

Run: `curl -s http://127.0.0.1:8000/metrics | grep automoney_grid_`
Expected: ver as métricas listadas (zero values OK no início).

- [ ] **Step 4: Commit**

```bash
git add engine/metrics.py engine/__init__.py
git commit -m "feat(metrics): Prometheus telemetria para predictive grid v2"
```

---

### Task C2: StateHub.grid_health_metrics

**Files:**
- Modify: `state.py`
- Modify: `engine/__init__.py`

- [ ] **Step 1: Adicionar campo no StateHub**

Em `state.py`:
```python
# Predictive grid v2 telemetry
grid_health_metrics: dict = field(default_factory=lambda: {
    "levels_active": 0,
    "stops_placed_total": 0,
    "stops_filled_total": 0,
    "stops_cancelled_total": 0,
    "rebuilds_total": 0,
    "last_rebuild_reason": None,
    "last_rebuild_ts": None,
    "replication_error_pct": 0.0,
    "range_changes_total": 0,
})
```

- [ ] **Step 2: Popular nos pontos relevantes do `_maintain_grid`**

A cada operação, atualizar o dict correspondente:
```python
self._hub.grid_health_metrics["levels_active"] = len(new_grid)
self._hub.grid_health_metrics["rebuilds_total"] += 1
self._hub.grid_health_metrics["last_rebuild_reason"] = "range_change"
self._hub.grid_health_metrics["last_rebuild_ts"] = time.time()
```

- [ ] **Step 3: Commit**

```bash
git add state.py engine/__init__.py
git commit -m "feat(state): StateHub.grid_health_metrics for dashboard display"
```

---

### Task C3: Dashboard partial — Grid Health card

**Files:**
- Create: `web/templates/partials/grid_health.html`
- Modify: `web/templates/dashboard.html` (incluir o partial)
- Modify: `web/routes.py` (expor grid_health no SSE/state payload)

- [ ] **Step 1: Criar partial**

`web/templates/partials/grid_health.html`:
```html
<div class="card">
  <h3>GRID HEALTH</h3>
  <div class="kv">
    <span>Levels ativos</span>
    <span x-text="grid_health_metrics.levels_active">0</span>
  </div>
  <div class="kv">
    <span>Stops placed (total)</span>
    <span x-text="grid_health_metrics.stops_placed_total">0</span>
  </div>
  <div class="kv">
    <span>Stops filled (total)</span>
    <span x-text="grid_health_metrics.stops_filled_total">0</span>
  </div>
  <div class="kv">
    <span>Rebuilds (total)</span>
    <span x-text="grid_health_metrics.rebuilds_total">0</span>
  </div>
  <div class="kv">
    <span>Replication error</span>
    <span x-text="(grid_health_metrics.replication_error_pct * 100).toFixed(2) + '%'">0%</span>
  </div>
  <div class="kv">
    <span>Range changes</span>
    <span x-text="grid_health_metrics.range_changes_total">0</span>
  </div>
</div>
```

- [ ] **Step 2: Incluir partial em dashboard.html**

Em `web/templates/dashboard.html`, no lugar relevante:
```html
{% include "partials/grid_health.html" %}
```

- [ ] **Step 3: Expor no payload SSE**

Em `web/routes.py`, no SSE endpoint que serializa o state:
```python
"grid_health_metrics": hub.grid_health_metrics,
```

- [ ] **Step 4: Smoke test no browser**

Subir bot em local com `PREDICTIVE_GRID_V2=true`, abrir dashboard, verificar que card "GRID HEALTH" aparece e atualiza.

- [ ] **Step 5: Commit**

```bash
git add web/templates/partials/grid_health.html web/templates/dashboard.html web/routes.py
git commit -m "feat(ui): dashboard partial pra grid health metrics"
```

---

## Phase D — Smoke + Cutover (PR 4)

### Task D1: Documentar runbook de smoke test em sandbox

**Files:**
- Create: `docs/predictive-grid-v2-smoke-runbook.md`

- [ ] **Step 1: Escrever runbook**

`docs/predictive-grid-v2-smoke-runbook.md`:
```markdown
# Predictive Grid v2 — smoke test runbook

## Pré-requisitos
- [ ] DO droplet SANDBOX em FRA1 separado do prod (mesmo passa Lighter WAF)
- [ ] .env completo com `PREDICTIVE_GRID_V2=true`
- [ ] Wallet com USDC.e Arbitrum e collateral USDC no Lighter (~$200 sandbox)
- [ ] Pair selecionado via pair-picker: ARB/USDC.e Beefy CLM
- [ ] Capital LP: $50-100 inicial (smoke), depois $500 (validação)

## Smoke 1 — boot + range detection (5 min)
- [ ] Deposit manual ARB/USDC.e no vault Beefy via UI Beefy
- [ ] POST /operations/hedge-existing → verificar engine pega posição
- [ ] Logs: deve ver "predictive_grid_v2=true" + primeiro `_maintain_grid` log
- [ ] /metrics: `automoney_grid_levels_active > 0`
- [ ] Lighter UI: ver N stop-limit orders postadas em ARB-USD

## Smoke 2 — fill event (30 min, depende de mercado)
- [ ] Esperar mark price cruzar 1 tick do pool
- [ ] Logs: `_on_grid_fill` log
- [ ] /metrics: `automoney_grid_stops_filled_total > 0`
- [ ] Verificar próximo level foi postado (`grid_stops_placed_total` incrementou)
- [ ] DB: `grid_orders` table mostra fill + novo level

## Smoke 3 — range change da Beefy (depende da Beefy rebalancear, pode levar dias)
- [ ] Monitorar `automoney_beefy_range_change_total`
- [ ] Quando incrementar: verificar logs de cancel-all + rebuild
- [ ] /metrics: levels_active reset + novo número
- [ ] Lighter UI: orders antigas canceladas, novas postadas

## Smoke 4 — 24h sustentado
- [ ] Verificar `replication_error_pct < 2%` médio (Grafana / curl /metrics)
- [ ] Verificar `fill_latency_ms p95 < 60s`
- [ ] Verificar bot não crashou (uptime check)
- [ ] Calcular hedge_pnl / il_natural via /operations/current → ratio

## Critério de promoção (sandbox → produção)
- [ ] Smoke 1-4 passa
- [ ] Bot rodou 24h estável
- [ ] Replication error < 2%
- [ ] Fill latency p95 < 60s
- [ ] Zero crashes
```

- [ ] **Step 2: Commit**

```bash
git add docs/predictive-grid-v2-smoke-runbook.md
git commit -m "docs(runbook): smoke test plan for predictive grid v2"
```

---

### Task D2: Cutover — feature flag virada + cleanup legacy

> **PRECONDITION:** Tasks D1 smoke 1-4 passed em sandbox por 24h+. Não fazer antes.

**Files:**
- Modify: `engine/__init__.py` (remover `_maybe_rebalance_leg`)
- Modify: `config.py` (default `True`)

- [ ] **Step 1: Mudar default do feature flag pra True**

Em `config.py`:
```python
predictive_grid_v2=os.environ.get(
    "PREDICTIVE_GRID_V2", "true"  # default agora é true
).lower() in ("true", "1", "yes"),
```

- [ ] **Step 2: Remover path legacy de `_iterate`**

No engine, simplificar:
```python
# Antigo:
#   if self._settings.predictive_grid_v2: await self._maintain_grid(...)
#   else: for sym in symbols: await self._maybe_rebalance_leg(...)
# Novo:
await self._maintain_grid(beefy_pos=..., p_now=..., oracle_prices=...)
```

- [ ] **Step 3: Remover método `_maybe_rebalance_leg` e helpers órfãos**

Run: `grep -n "_maybe_rebalance_leg" engine/`
Identificar todas as referências; remover método + qualquer helper usado só por ele.

- [ ] **Step 4: Atualizar tests**

Remover tests de `_maybe_rebalance_leg` se existirem ou marcá-los como skip / migrar pra integração v2.

- [ ] **Step 5: Rodar suite completa**

Run: `python -m pytest tests/ -v 2>&1 | tail -20`
Expected: tudo verde.

- [ ] **Step 6: Update CLAUDE.md + WORKING_ON.md**

Em `CLAUDE.md`: trocar descrição da arquitetura pra refletir predictive grid v2 como design atual.
Em `WORKING_ON.md`: marcar predictive-grid-v2 como concluído, atualizar próximas pendências.

- [ ] **Step 7: Commit**

```bash
git add config.py engine/__init__.py tests/ CLAUDE.md WORKING_ON.md
git commit -m "feat(cutover): predictive grid v2 default-on, remove legacy taker chase"
```

---

## Self-Review (executado durante a escrita)

**Spec coverage:**
- [x] §1.3 What changes → Task A2 (compute_grid_from_pool_ticks) + Task B3-B5 (_maintain_grid + handler)
- [x] §2 Decisões: pool, capital, order type → cobertas em B4 (place_stop_limit_order params)
- [x] §3 Data flow → coberto via Tasks A2, A6, B4, B5
- [x] §4.1 Algoritmo → Task A2
- [x] §4.2 Range dinâmico Beefy → Task B2 + B4
- [x] §4.3 Skip tick_now → implementado em A2 (skip explícito)
- [x] §5 Mapeamento Lighter → Task A6
- [x] §5.2 TIF 28-day → herdado do SDK default (Task A6)
- [x] §5.3 Cancel → Task A7
- [x] §6.1 Trigger 1 (fill) → Task B5
- [x] §6.1 Trigger 2 (drift) → coberta em B4 (range change inclui composição)
- [x] §6.1 Trigger 3 (range change) ⚠ CRÍTICO → Task B4 explicitamente
- [x] §6.1 Trigger 4 (range exit) → coberta em B4 (range mudou = rebuild)
- [x] §7 Mudanças de código → todas as files das Tasks A1-D2
- [x] §8 Telemetria → Task C1, C2
- [x] §9 Riscos → cobertos via testes + telemetria
- [x] §11 Test plan → Tasks A1-A7 (unit) + B3-B5 (integration) + D1 (live)
- [x] §12 Rollout → Phase A=PR1, B=PR2, C=PR3, D=cutover (PR4)
- [x] §13 Critérios de aceitação → todos mapeados

**Placeholder scan:** sem "TBD" / "TODO" / "implement later". Task B5 tem `# implementação completa depende do handler de fill existente` que é direcionamento, não placeholder.

**Type consistency:** 
- `GridLevel.trigger_price` adicionado em A5, usado em B4
- `BeefyPosition.liquidity` adicionado em B2, usado em B4
- `place_stop_limit_order` signature consistente A6 → B4 → B5
- `_posted_grid_signature` introduzido em B4, lido em B5

**Scope:** plano focado em uma feature (predictive grid v2). Phase D smoke fica fora do PR mas dentro do escopo da feature.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-05-12-predictive-grid-v2.md`. Duas opções de execução:**

**1. Subagent-Driven (recomendada)** — Eu dispacho um subagent fresco por task, review entre tasks, iteração rápida. Memory `feedback_subagent_driven_default.md` confirma esta como a default.

**2. Inline Execution** — Executa tasks nesta sessão usando executing-plans, batch execution com checkpoints.

Qual approach?
