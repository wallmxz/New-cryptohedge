# Event-driven grid reconciler — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Substituir o reconciler "self-healing" (que recomputa desired grid a cada iter e satura o rate limit Lighter) por um modelo event-driven que só muta a grid quando a posição muda.

**Architecture:** Loop dedicado a 100ms faz `get_position()`, compara com `_last_known_position`. Se diferente, query `get_open_orders`, identifica cloids filled via diff vs `_local_grid`, dispara 3 writes por fill (cancel contra-parte + 2 posts). Safety net 90s pra audit completo. `_main_loop` segue a 1Hz pra state refresh + drift correction (sem mexer em ordens). Substituição completa do código atual em `_maintain_grid`/`_reconcile_grid` — sem feature flag novo, dentro do path `predictive_grid_v2=true` que já existe.

**Tech Stack:** Python 3.12 asyncio, pytest-asyncio, aiosqlite. Lighter SDK pra place/cancel/get_position/get_open_orders.

**Spec:** [docs/superpowers/specs/2026-05-15-event-driven-grid-design.md](../specs/2026-05-15-event-driven-grid-design.md)

---

## File structure

**Files to create:**
- `engine/grid_state.py` — `GridStop` dataclass + lookup helpers (lowest_buy, highest_sell, etc.)
- `tests/test_engine_event_driven_grid.py` — full coverage da nova lógica
- `tests/test_grid_state.py` — unit tests do dataclass + helpers

**Files to modify:**
- `engine/__init__.py`:
  - Adicionar state em `__init__` (~linha 58-97)
  - Adicionar `_grid_event_loop()` task method
  - Adicionar `_apply_fills_to_grid()` helper
  - Adicionar `_safety_reconcile()` helper (bootstrap + steady-state paths)
  - Substituir body de `_maintain_grid` (linha 1336) por versão simplificada (só range/out-of-range/initial placement)
  - Deletar `_reconcile_grid` (linha 1432) — substituído pelo event loop
  - Hook em `_aggressive_correct` (~linha 1736 `_aggressive_correct`) pra atualizar `_last_known_position` pós-taker
  - Iniciar/cancelar `_grid_event_loop` em `start()`/`stop()`

---

## Branch setup

### Task 0: Create branch

- [ ] **Step 1:** Criar branch a partir de master atual
```bash
git checkout master && git pull && git checkout -b feature/event-driven-grid
```

- [ ] **Step 2:** Verificar branch ativa
```bash
git branch --show-current
```
Expected: `feature/event-driven-grid`

---

## Foundation — GridStop dataclass

### Task 1: GridStop dataclass + helpers

**Files:**
- Create: `engine/grid_state.py`
- Test: `tests/test_grid_state.py`

- [ ] **Step 1: Failing test**

```python
# tests/test_grid_state.py
from engine.grid_state import GridStop, lowest_buy, highest_sell, top_sell, bottom_buy


def test_gridstop_dataclass_fields():
    s = GridStop(cloid=12345, side="sell", trigger_price=0.135, size=3.0)
    assert s.cloid == 12345
    assert s.side == "sell"
    assert s.trigger_price == 0.135
    assert s.size == 3.0


def test_lowest_buy_returns_lowest_price_buy():
    grid = {
        1: GridStop(1, "buy", 0.130, 3.0),
        2: GridStop(2, "buy", 0.131, 3.0),
        3: GridStop(3, "sell", 0.140, 3.0),
    }
    result = lowest_buy(grid)
    assert result.cloid == 1
    assert result.trigger_price == 0.130


def test_highest_sell_returns_highest_price_sell():
    grid = {
        1: GridStop(1, "sell", 0.140, 3.0),
        2: GridStop(2, "sell", 0.142, 3.0),
        3: GridStop(3, "buy", 0.130, 3.0),
    }
    result = highest_sell(grid)
    assert result.cloid == 2
    assert result.trigger_price == 0.142


def test_top_sell_returns_highest_price_sell():
    """top_sell == highest_sell (alias for clarity in event-driven algo)."""
    grid = {1: GridStop(1, "sell", 0.140, 3.0), 2: GridStop(2, "sell", 0.142, 3.0)}
    assert top_sell(grid).trigger_price == 0.142


def test_bottom_buy_returns_lowest_price_buy():
    """bottom_buy == lowest_buy (alias)."""
    grid = {1: GridStop(1, "buy", 0.130, 3.0), 2: GridStop(2, "buy", 0.128, 3.0)}
    assert bottom_buy(grid).trigger_price == 0.128


def test_helpers_return_none_when_no_matching_side():
    grid = {1: GridStop(1, "sell", 0.140, 3.0)}
    assert lowest_buy(grid) is None
    assert bottom_buy(grid) is None
```

- [ ] **Step 2: Run test (expect fail)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_grid_state.py -v
```
Expected: ImportError (`engine.grid_state` doesn't exist)

- [ ] **Step 3: Implement**

```python
# engine/grid_state.py
"""GridStop dataclass + lookup helpers for the event-driven grid reconciler.

A GridStop represents one stop order the bot has posted on Lighter and is
tracking locally in `_local_grid`. The lookup helpers (lowest_buy, etc.)
support the algorithm in `_apply_fills_to_grid`.
"""
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class GridStop:
    cloid: int
    side: Literal["sell", "buy"]
    trigger_price: float
    size: float


def lowest_buy(grid: dict[int, GridStop]) -> GridStop | None:
    """Return the buy GridStop with the LOWEST trigger_price (farthest from market below).

    None if no buys in grid.
    """
    buys = [s for s in grid.values() if s.side == "buy"]
    return min(buys, key=lambda s: s.trigger_price) if buys else None


def highest_sell(grid: dict[int, GridStop]) -> GridStop | None:
    """Return the sell GridStop with the HIGHEST trigger_price (farthest from market above)."""
    sells = [s for s in grid.values() if s.side == "sell"]
    return max(sells, key=lambda s: s.trigger_price) if sells else None


# Aliases for clarity in the event-driven algorithm.
top_sell = highest_sell
bottom_buy = lowest_buy
```

- [ ] **Step 4: Run test (expect pass)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_grid_state.py -v
```
Expected: 6 passed

- [ ] **Step 5: Commit**
```bash
git add engine/grid_state.py tests/test_grid_state.py
git commit -m "feat(grid): add GridStop dataclass + lookup helpers for event-driven reconciler"
```

---

## Engine state initialization

### Task 2: Add state vars to GridMakerEngine

**Files:**
- Modify: `engine/__init__.py:58-97` (init block)
- Test: `tests/test_engine_event_driven_grid.py` (new)

- [ ] **Step 1: Failing test**

```python
# tests/test_engine_event_driven_grid.py
import pytest
from unittest.mock import MagicMock
from engine import GridMakerEngine


def _make_engine():
    settings = MagicMock()
    settings.dydx_symbol_token0 = "ARB-USD"
    settings.dydx_symbol_token1 = "ETH-USD"
    return GridMakerEngine(
        settings=settings, hub=MagicMock(), db=MagicMock(), exchange=None,
    )


def test_engine_has_event_driven_state_vars_init_empty():
    engine = _make_engine()
    assert engine._last_known_position is None
    assert engine._local_grid == {}
    assert engine._last_safety_reconcile_at == 0.0
```

- [ ] **Step 2: Run test (expect fail)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_engine_has_event_driven_state_vars_init_empty -v
```
Expected: AttributeError (`_last_known_position` not set)

- [ ] **Step 3: Implement — adicionar 3 linhas no final do `__init__` em `engine/__init__.py` (depois da linha 97 `self._refresh_task = None`)**

```python
        # Event-driven grid state (spec 2026-05-15-event-driven-grid-design).
        # _last_known_position: Position | None — last seen, compared against
        #   pos_now in _grid_event_loop to detect fills.
        # _local_grid: dict[cloid, GridStop] — snapshot of stops we posted.
        # _last_safety_reconcile_at: timestamp of last full audit (90s cadence).
        self._last_known_position = None
        self._local_grid: dict[int, "GridStop"] = {}
        self._last_safety_reconcile_at: float = 0.0
```

- [ ] **Step 4: Run test (expect pass)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_engine_has_event_driven_state_vars_init_empty -v
```
Expected: 1 passed

- [ ] **Step 5: Commit**
```bash
git add engine/__init__.py tests/test_engine_event_driven_grid.py
git commit -m "feat(engine): add event-driven grid state vars (_last_known_position, _local_grid, _last_safety_reconcile_at)"
```

---

## Fill application — sell

### Task 3: _apply_fills_to_grid for sell fill

**Files:**
- Modify: `engine/__init__.py` (add new method after `_reconcile_grid`)
- Test: `tests/test_engine_event_driven_grid.py`

- [ ] **Step 1: Failing test**

```python
# Append to tests/test_engine_event_driven_grid.py
import pytest
from unittest.mock import AsyncMock
from engine.grid_state import GridStop


@pytest.mark.asyncio
async def test_single_sell_fill_triggers_3_writes():
    """A sell fills → cancel lowest buy + post buy at fill trigger + post sell at top+step."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.cancel_stop_order = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
    engine._next_cloid_for_leg = MagicMock(side_effect=[9001, 9002])

    # Pre-populate local_grid with 4-stop micro grid (2 sells + 2 buys)
    # sells at 0.140, 0.142  | buys at 0.130, 0.128
    engine._local_grid = {
        100: GridStop(100, "sell", 0.140, 3.0),  # this one filled
        101: GridStop(101, "sell", 0.142, 3.0),  # top sell
        200: GridStop(200, "buy", 0.130, 3.0),
        201: GridStop(201, "buy", 0.128, 3.0),   # lowest buy
    }
    step = 0.002  # arbitrary fixed step for the test

    await engine._apply_fills_to_grid(filled_cloids={100}, step=step)

    # Assertions
    assert engine._exchange.cancel_stop_order.call_count == 1
    cancel_call = engine._exchange.cancel_stop_order.call_args
    # lowest buy should be cancelled (cloid 201)
    assert cancel_call.kwargs.get("cloid_int") == 201 or cancel_call.args[0] == 201 or 201 in str(cancel_call)

    assert engine._exchange.place_stop_market.call_count == 2
    posts = engine._exchange.place_stop_market.call_args_list
    # First post: new buy at filled sell's trigger price (0.140)
    assert posts[0].kwargs["side"] == "buy"
    assert posts[0].kwargs["trigger_price"] == 0.140
    # Second post: new sell at top + step = 0.142 + 0.002 = 0.144
    assert posts[1].kwargs["side"] == "sell"
    assert abs(posts[1].kwargs["trigger_price"] - 0.144) < 1e-9

    # local_grid updated: removed cloid 100 (filled) and 201 (cancelled), added 9001 (new buy) and 9002 (new sell)
    assert 100 not in engine._local_grid
    assert 201 not in engine._local_grid
    assert 9001 in engine._local_grid
    assert engine._local_grid[9001].side == "buy"
    assert engine._local_grid[9001].trigger_price == 0.140
    assert 9002 in engine._local_grid
    assert engine._local_grid[9002].side == "sell"
    assert abs(engine._local_grid[9002].trigger_price - 0.144) < 1e-9
```

- [ ] **Step 2: Run test (expect fail)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_single_sell_fill_triggers_3_writes -v
```
Expected: AttributeError (`_apply_fills_to_grid` not defined)

- [ ] **Step 3: Implement — adicionar método em `engine/__init__.py` (logo depois de `_reconcile_grid`, ~linha 1601)**

```python
    async def _apply_fills_to_grid(
        self, *, filled_cloids: set[int], step: float,
    ) -> None:
        """Process detected fills: for each filled cloid, cancel the opposite
        side's farthest stop and post 2 replacements (1 near market at fill
        trigger, 1 extending the same-side range).

        Multi-fill handling: process fills in order of distance-from-market
        (closest first), so each iteration sees a coherent local_grid.

        Spec: docs/superpowers/specs/2026-05-15-event-driven-grid-design.md
        """
        from engine.grid_state import GridStop, lowest_buy, top_sell, highest_sell, bottom_buy

        symbol = self._settings.dydx_symbol_token0

        # Sort filled cloids by distance from extremes (closest to market first).
        # For a sell fill, "closest to market" = lowest trigger price among sells.
        # For a buy fill, "closest to market" = highest trigger price among buys.
        # We can't know the market price here without re-reading, so use a
        # heuristic: sells sorted ASC by trigger (lowest = was closest to market),
        # buys sorted DESC by trigger (highest = was closest to market).
        ordered = sorted(
            filled_cloids,
            key=lambda c: (
                self._local_grid[c].trigger_price
                if self._local_grid[c].side == "sell"
                else -self._local_grid[c].trigger_price
            ),
        )

        for cloid in ordered:
            stop = self._local_grid.get(cloid)
            if stop is None:
                continue  # already processed (race)

            if stop.side == "sell":
                opp = lowest_buy(self._local_grid)
                tip = top_sell(self._local_grid)
                if opp is None or tip is None:
                    continue  # malformed grid; safety net will recover
                # Cancel lowest buy
                try:
                    await self._exchange.cancel_stop_order(symbol=symbol, cloid_int=opp.cloid)
                except Exception as e:
                    logger.warning(f"event-driven cancel failed: {e}")
                # Post replacement buy at filled sell's trigger price (closest to market)
                new_buy_cloid = self._next_cloid_for_leg(symbol)
                try:
                    await self._exchange.place_stop_market(
                        symbol=symbol, side="buy", size=stop.size,
                        trigger_price=stop.trigger_price, cloid_int=new_buy_cloid,
                    )
                except Exception as e:
                    logger.warning(f"event-driven post buy failed: {e}")
                # Post new sell extending the top
                new_sell_price = tip.trigger_price + step
                new_sell_cloid = self._next_cloid_for_leg(symbol)
                try:
                    await self._exchange.place_stop_market(
                        symbol=symbol, side="sell", size=stop.size,
                        trigger_price=new_sell_price, cloid_int=new_sell_cloid,
                    )
                except Exception as e:
                    logger.warning(f"event-driven post sell failed: {e}")
                # Update local_grid
                self._local_grid.pop(cloid, None)
                self._local_grid.pop(opp.cloid, None)
                self._local_grid[new_buy_cloid] = GridStop(
                    new_buy_cloid, "buy", stop.trigger_price, stop.size,
                )
                self._local_grid[new_sell_cloid] = GridStop(
                    new_sell_cloid, "sell", new_sell_price, stop.size,
                )
            else:  # buy filled
                opp = highest_sell(self._local_grid)
                tip = bottom_buy(self._local_grid)
                if opp is None or tip is None:
                    continue
                try:
                    await self._exchange.cancel_stop_order(symbol=symbol, cloid_int=opp.cloid)
                except Exception as e:
                    logger.warning(f"event-driven cancel failed: {e}")
                new_sell_cloid = self._next_cloid_for_leg(symbol)
                try:
                    await self._exchange.place_stop_market(
                        symbol=symbol, side="sell", size=stop.size,
                        trigger_price=stop.trigger_price, cloid_int=new_sell_cloid,
                    )
                except Exception as e:
                    logger.warning(f"event-driven post sell failed: {e}")
                new_buy_price = tip.trigger_price - step
                new_buy_cloid = self._next_cloid_for_leg(symbol)
                try:
                    await self._exchange.place_stop_market(
                        symbol=symbol, side="buy", size=stop.size,
                        trigger_price=new_buy_price, cloid_int=new_buy_cloid,
                    )
                except Exception as e:
                    logger.warning(f"event-driven post buy failed: {e}")
                self._local_grid.pop(cloid, None)
                self._local_grid.pop(opp.cloid, None)
                self._local_grid[new_sell_cloid] = GridStop(
                    new_sell_cloid, "sell", stop.trigger_price, stop.size,
                )
                self._local_grid[new_buy_cloid] = GridStop(
                    new_buy_cloid, "buy", new_buy_price, stop.size,
                )
```

- [ ] **Step 4: Run test (expect pass)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_single_sell_fill_triggers_3_writes -v
```
Expected: 1 passed

- [ ] **Step 5: Commit**
```bash
git add engine/__init__.py tests/test_engine_event_driven_grid.py
git commit -m "feat(engine): add _apply_fills_to_grid handler for sell fills (cancel lowest buy + 2 posts)"
```

---

## Fill application — buy (mirror)

### Task 4: Test buy fill mirror logic

**Files:**
- Test: `tests/test_engine_event_driven_grid.py` (append)
- (No new impl — same `_apply_fills_to_grid` covers buy path; test validates symmetry)

- [ ] **Step 1: Add test (impl already exists from Task 3 buy branch)**

```python
@pytest.mark.asyncio
async def test_single_buy_fill_triggers_3_writes():
    """A buy fills → cancel highest sell + post sell at fill trigger + post buy at bottom-step."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.cancel_stop_order = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
    engine._next_cloid_for_leg = MagicMock(side_effect=[9101, 9102])

    engine._local_grid = {
        100: GridStop(100, "sell", 0.140, 3.0),
        101: GridStop(101, "sell", 0.142, 3.0),  # highest sell — gets cancelled
        200: GridStop(200, "buy", 0.130, 3.0),   # this one filled
        201: GridStop(201, "buy", 0.128, 3.0),   # bottom buy
    }
    step = 0.002

    await engine._apply_fills_to_grid(filled_cloids={200}, step=step)

    assert engine._exchange.cancel_stop_order.call_count == 1
    posts = engine._exchange.place_stop_market.call_args_list
    assert engine._exchange.place_stop_market.call_count == 2
    # First post: new sell at filled buy's trigger (0.130)
    assert posts[0].kwargs["side"] == "sell"
    assert posts[0].kwargs["trigger_price"] == 0.130
    # Second post: new buy at bottom - step = 0.128 - 0.002 = 0.126
    assert posts[1].kwargs["side"] == "buy"
    assert abs(posts[1].kwargs["trigger_price"] - 0.126) < 1e-9

    assert 200 not in engine._local_grid
    assert 101 not in engine._local_grid
    assert engine._local_grid[9101].side == "sell"
    assert engine._local_grid[9101].trigger_price == 0.130
    assert engine._local_grid[9102].side == "buy"
    assert abs(engine._local_grid[9102].trigger_price - 0.126) < 1e-9
```

- [ ] **Step 2: Run test (expect pass)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_single_buy_fill_triggers_3_writes -v
```
Expected: 1 passed (impl from Task 3 covers it)

- [ ] **Step 3: Commit**
```bash
git add tests/test_engine_event_driven_grid.py
git commit -m "test(engine): cover buy fill mirror in _apply_fills_to_grid"
```

---

## Multi-fill handling

### Task 5: Test multi-fill in single iter

**Files:**
- Test: `tests/test_engine_event_driven_grid.py` (append)

- [ ] **Step 1: Add test**

```python
@pytest.mark.asyncio
async def test_two_sells_filled_same_iter_processed_in_order():
    """Two sells filled simultaneously → 6 writes (2 cancels + 4 posts).
    Closest-to-market processed first."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.cancel_stop_order = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
    # 6 cloids needed (2 fills × 2 posts each + buffer); use a generator
    cloid_seq = iter(range(9201, 9210))
    engine._next_cloid_for_leg = MagicMock(side_effect=lambda sym: next(cloid_seq))

    # 4-stop pre-grid; 2 lowest sells fill
    engine._local_grid = {
        100: GridStop(100, "sell", 0.140, 3.0),  # filled (closest to market)
        101: GridStop(101, "sell", 0.142, 3.0),  # filled
        102: GridStop(102, "sell", 0.144, 3.0),  # top sell BEFORE fills
        200: GridStop(200, "buy", 0.130, 3.0),
        201: GridStop(201, "buy", 0.128, 3.0),  # lowest buy
        202: GridStop(202, "buy", 0.126, 3.0),  # 2nd lowest
    }
    step = 0.002

    await engine._apply_fills_to_grid(filled_cloids={100, 101}, step=step)

    # 2 cancels + 4 posts = 6 writes
    assert engine._exchange.cancel_stop_order.call_count == 2
    assert engine._exchange.place_stop_market.call_count == 4

    # First fill processed (cloid 100, lowest sell at 0.140):
    #  - cancel lowest buy (201 at 0.128)
    #  - post buy at 0.140
    #  - post sell at 0.144 + 0.002 = 0.146
    # Second fill processed (cloid 101, sell at 0.142):
    #  - now lowest_buy is 202 (since 201 was cancelled), at 0.126; cancel it
    #  - post buy at 0.142
    #  - post sell at 0.146 + 0.002 = 0.148  (top extended again)

    cancels = engine._exchange.cancel_stop_order.call_args_list
    posts = engine._exchange.place_stop_market.call_args_list

    cancel_cloids = [c.kwargs.get("cloid_int") for c in cancels]
    assert cancel_cloids == [201, 202]

    post_prices = [(p.kwargs["side"], p.kwargs["trigger_price"]) for p in posts]
    assert post_prices[0] == ("buy", 0.140)
    assert post_prices[1] == ("sell", pytest.approx(0.146))
    assert post_prices[2] == ("buy", 0.142)
    assert post_prices[3] == ("sell", pytest.approx(0.148))
```

- [ ] **Step 2: Run test (expect pass; impl already correct)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_two_sells_filled_same_iter_processed_in_order -v
```
Expected: 1 passed

- [ ] **Step 3: Commit**
```bash
git add tests/test_engine_event_driven_grid.py
git commit -m "test(engine): cover multi-fill ordering in _apply_fills_to_grid"
```

---

## Safety net — bootstrap path

### Task 6: _safety_reconcile bootstrap (populate local_grid from Lighter+DB)

**Files:**
- Modify: `engine/__init__.py` (add `_safety_reconcile` after `_apply_fills_to_grid`)
- Test: `tests/test_engine_event_driven_grid.py` (append)

- [ ] **Step 1: Failing test**

```python
@pytest.mark.asyncio
async def test_safety_reconcile_bootstrap_populates_local_grid_from_lighter():
    """First call: local_grid empty → query Lighter open_orders + DB lookup,
    populate local_grid. NO cancellations."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    # Lighter returns 3 live orders
    engine._exchange.get_open_orders = AsyncMock(return_value=[
        {"cloid": "5001", "side": "sell", "trigger_price": 0.140, "size": 3.0},
        {"cloid": "5002", "side": "sell", "trigger_price": 0.142, "size": 3.0},
        {"cloid": "6001", "side": "buy", "trigger_price": 0.130, "size": 3.0},
    ])
    engine._exchange.cancel_stop_order = AsyncMock()  # should NOT be called

    # local_grid empty (post-restart state)
    engine._local_grid = {}

    await engine._safety_reconcile()

    # local_grid now has 3 entries matching Lighter
    assert len(engine._local_grid) == 3
    assert 5001 in engine._local_grid
    assert engine._local_grid[5001].side == "sell"
    assert engine._local_grid[5001].trigger_price == 0.140
    assert 5002 in engine._local_grid
    assert 6001 in engine._local_grid
    assert engine._local_grid[6001].side == "buy"

    # No cancel calls during bootstrap
    engine._exchange.cancel_stop_order.assert_not_called()
```

- [ ] **Step 2: Run test (expect fail)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_safety_reconcile_bootstrap_populates_local_grid_from_lighter -v
```
Expected: AttributeError

- [ ] **Step 3: Implement — adicionar em `engine/__init__.py` logo depois de `_apply_fills_to_grid`**

```python
    async def _safety_reconcile(self) -> None:
        """Periodic full audit (90s cadence). Behavior depends on local_grid state.

        Bootstrap path (local_grid empty after restart): query Lighter live
        orders, populate local_grid from them. NO cancellations.

        Steady-state path (local_grid populated): bidirectional diff.
          - Orders on Lighter not in local_grid → orphan, cancel.
          - Cloids in local_grid not on Lighter → assumed filled, re-trigger
            fill detection via _apply_fills_to_grid (idempotent).
        """
        from engine.grid_state import GridStop

        symbol = self._settings.dydx_symbol_token0
        try:
            live = await self._exchange.get_open_orders(symbol)
        except Exception as e:
            logger.warning(f"_safety_reconcile: get_open_orders failed: {e}")
            return

        live_by_cloid = {int(o["cloid"]): o for o in live}

        if not self._local_grid:
            # Bootstrap path
            for cloid, o in live_by_cloid.items():
                self._local_grid[cloid] = GridStop(
                    cloid=cloid, side=o["side"],
                    trigger_price=float(o["trigger_price"]),
                    size=float(o.get("size", 0.0)),
                )
            logger.info(
                f"_safety_reconcile bootstrap: populated local_grid with {len(self._local_grid)} stops"
            )
            return

        # Steady-state path
        local_cloids = set(self._local_grid.keys())
        live_cloids = set(live_by_cloid.keys())

        # Orphans on Lighter (not in local) → cancel
        orphans = live_cloids - local_cloids
        for cloid in orphans:
            try:
                await self._exchange.cancel_stop_order(symbol=symbol, cloid_int=cloid)
                logger.info(f"_safety_reconcile cancelled orphan cloid={cloid}")
            except Exception as e:
                logger.warning(f"_safety_reconcile orphan cancel failed: {e}")

        # Missing on Lighter (in local but not live) → assumed filled
        missing = local_cloids - live_cloids
        if missing:
            # Compute step from current top sell vs second-from-top sell (if exists)
            # Fallback: 0 (don't extend) — safe; next real fill loop will use proper step.
            step = self._estimate_grid_step()
            await self._apply_fills_to_grid(filled_cloids=missing, step=step)
```

Adicionar também o helper `_estimate_grid_step` (no mesmo arquivo, perto):

```python
    def _estimate_grid_step(self) -> float:
        """Estimate grid step from existing _local_grid (diff between consecutive same-side prices).

        Used by _safety_reconcile when applying fills detected outside the normal flow.
        Falls back to 0.0 if grid too sparse (1 stop will be added at fill price; safety net
        next iter will fix any imbalance).
        """
        sells = sorted(
            (s.trigger_price for s in self._local_grid.values() if s.side == "sell"),
        )
        if len(sells) >= 2:
            return sells[1] - sells[0]
        buys = sorted(
            (s.trigger_price for s in self._local_grid.values() if s.side == "buy"),
            reverse=True,
        )
        if len(buys) >= 2:
            return buys[0] - buys[1]
        return 0.0
```

- [ ] **Step 4: Run test (expect pass)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_safety_reconcile_bootstrap_populates_local_grid_from_lighter -v
```
Expected: 1 passed

- [ ] **Step 5: Commit**
```bash
git add engine/__init__.py tests/test_engine_event_driven_grid.py
git commit -m "feat(engine): add _safety_reconcile bootstrap path (populate local_grid from Lighter post-restart)"
```

---

## Safety net — steady-state

### Task 7: _safety_reconcile orphan + missing detection

**Files:**
- Test: `tests/test_engine_event_driven_grid.py` (append; impl from Task 6 already covers)

- [ ] **Step 1: Add test**

```python
@pytest.mark.asyncio
async def test_safety_reconcile_steady_state_cancels_orphan():
    """Steady-state: Lighter has 1 cloid that's not in local_grid → cancel it."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.get_open_orders = AsyncMock(return_value=[
        {"cloid": "5001", "side": "sell", "trigger_price": 0.140, "size": 3.0},
        {"cloid": "9999", "side": "sell", "trigger_price": 0.150, "size": 3.0},  # orphan
    ])
    engine._exchange.cancel_stop_order = AsyncMock()

    engine._local_grid = {
        5001: GridStop(5001, "sell", 0.140, 3.0),  # known
    }

    await engine._safety_reconcile()

    engine._exchange.cancel_stop_order.assert_called_once()
    args = engine._exchange.cancel_stop_order.call_args
    assert args.kwargs.get("cloid_int") == 9999


@pytest.mark.asyncio
async def test_safety_reconcile_steady_state_detects_missing_as_fill():
    """Steady-state: cloid in local_grid not on Lighter → treat as filled,
    re-trigger _apply_fills_to_grid."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    # Lighter only has 1 order; local has 4 (3 missing = filled)
    engine._exchange.get_open_orders = AsyncMock(return_value=[
        {"cloid": "200", "side": "buy", "trigger_price": 0.130, "size": 3.0},
    ])
    engine._exchange.cancel_stop_order = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()
    cloid_seq = iter(range(9301, 9310))
    engine._next_cloid_for_leg = MagicMock(side_effect=lambda sym: next(cloid_seq))

    engine._local_grid = {
        100: GridStop(100, "sell", 0.140, 3.0),
        101: GridStop(101, "sell", 0.142, 3.0),
        200: GridStop(200, "buy", 0.130, 3.0),
        201: GridStop(201, "buy", 0.128, 3.0),
    }

    await engine._safety_reconcile()

    # 100 and 101 missing from Lighter (sells filled), 201 missing (buy filled)
    # Each fill = 1 cancel + 2 posts; multi-fill handled in order
    # Total: 3 fills × 3 writes = 9 writes minimum (some posts may overlap with cancels)
    assert engine._exchange.cancel_stop_order.call_count + engine._exchange.place_stop_market.call_count >= 9
```

- [ ] **Step 2: Run tests (expect pass)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_safety_reconcile_steady_state_cancels_orphan tests/test_engine_event_driven_grid.py::test_safety_reconcile_steady_state_detects_missing_as_fill -v
```
Expected: 2 passed

- [ ] **Step 3: Commit**
```bash
git add tests/test_engine_event_driven_grid.py
git commit -m "test(engine): cover _safety_reconcile steady-state (orphan cancel + missing→fill)"
```

---

## Grid event loop

### Task 8: _grid_event_loop task method

**Files:**
- Modify: `engine/__init__.py` (add new method + task management)
- Test: `tests/test_engine_event_driven_grid.py` (append)

- [ ] **Step 1: Failing test (no_position_change → no writes)**

```python
@pytest.mark.asyncio
async def test_grid_event_loop_iter_no_position_change_no_writes():
    """One iter of the event loop with pos_now == last_known_position → 0 writes."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    pos = MagicMock(symbol="ARB-USD", side="short", size=10.0, entry_price=0.135, unrealized_pnl=0.0)
    engine._exchange.get_position = AsyncMock(return_value=pos)
    engine._exchange.get_open_orders = AsyncMock()
    engine._exchange.cancel_stop_order = AsyncMock()
    engine._exchange.place_stop_market = AsyncMock()

    engine._last_known_position = pos
    engine._local_grid = {1: GridStop(1, "sell", 0.140, 3.0)}
    engine._last_safety_reconcile_at = 9999999999.0  # far future, so safety net doesn't fire

    await engine._grid_event_iter()

    # No writes
    engine._exchange.cancel_stop_order.assert_not_called()
    engine._exchange.place_stop_market.assert_not_called()
    # No open_orders read either (only on position change or safety net)
    engine._exchange.get_open_orders.assert_not_called()
```

- [ ] **Step 2: Run test (expect fail)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_grid_event_loop_iter_no_position_change_no_writes -v
```
Expected: AttributeError

- [ ] **Step 3: Implement — adicionar `_grid_event_iter` e `_grid_event_loop` em `engine/__init__.py`**

```python
    async def _grid_event_iter(self) -> None:
        """One iteration of the event-driven grid loop. Public for testing.

        - get_position (cheap read)
        - if changed since last iter → query open_orders, identify filled cloids,
          apply fills (3 writes per fill)
        - every 90s, safety_reconcile audit
        """
        symbol = self._settings.dydx_symbol_token0

        # Safety net (90s)
        now = time.time()
        if now - self._last_safety_reconcile_at > 90.0:
            await self._safety_reconcile()
            self._last_safety_reconcile_at = now

        # Position read
        try:
            pos_now = await self._exchange.get_position(symbol)
        except Exception as e:
            logger.warning(f"_grid_event_iter: get_position failed: {e}")
            return

        # Position-equality short-circuit
        if self._position_equal(pos_now, self._last_known_position):
            return

        # Position changed — query open_orders, identify filled cloids
        try:
            live = await self._exchange.get_open_orders(symbol)
        except Exception as e:
            logger.warning(f"_grid_event_iter: get_open_orders failed: {e}")
            return
        live_cloids = {int(o["cloid"]) for o in live}
        filled = set(self._local_grid.keys()) - live_cloids

        if filled:
            step = self._estimate_grid_step()
            await self._apply_fills_to_grid(filled_cloids=filled, step=step)

        self._last_known_position = pos_now

    @staticmethod
    def _position_equal(a, b) -> bool:
        """Compare two Position-ish objects by side + size (entry_price/PnL change frequently)."""
        if a is None and b is None:
            return True
        if a is None or b is None:
            return False
        return (
            getattr(a, "side", None) == getattr(b, "side", None)
            and abs(getattr(a, "size", 0.0) - getattr(b, "size", 0.0)) < 1e-9
        )

    async def _grid_event_loop(self) -> None:
        """Long-running task: event-driven grid maintenance at 100ms cadence."""
        period = 0.1  # 100ms
        while self._running:
            t0 = time.monotonic()
            try:
                await self._grid_event_iter()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.exception(f"_grid_event_loop iter error: {e}")
            elapsed = time.monotonic() - t0
            await asyncio.sleep(max(0.0, period - elapsed))
```

- [ ] **Step 4: Run test (expect pass)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_grid_event_loop_iter_no_position_change_no_writes -v
```
Expected: 1 passed

- [ ] **Step 5: Commit**
```bash
git add engine/__init__.py tests/test_engine_event_driven_grid.py
git commit -m "feat(engine): add _grid_event_loop task (100ms cadence, position-diff fill detection)"
```

---

## Wire grid event loop into engine lifecycle

### Task 9: Start/stop _grid_event_loop with engine

**Files:**
- Modify: `engine/__init__.py` (find `start()`/`stop()` methods, add task management)

- [ ] **Step 1: Find current `start()` and `stop()` methods**
```bash
grep -nE "def start\(|def stop\(|self\._task" engine/__init__.py | head -10
```
Note the line numbers.

- [ ] **Step 2: Add test that verifies `_grid_task` is created on start**

```python
# Append to tests/test_engine_event_driven_grid.py
import asyncio


@pytest.mark.asyncio
async def test_engine_start_creates_grid_event_loop_task():
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.get_position = AsyncMock(return_value=None)

    # Mock out _main_loop and chain reads to keep test fast
    async def _noop(): pass
    engine._main_loop = _noop

    engine._running = False
    engine.start()
    # Both tasks should be created
    assert engine._task is not None
    assert engine._grid_task is not None

    engine._running = False
    await asyncio.sleep(0.05)  # allow tasks to exit
    if not engine._task.done():
        engine._task.cancel()
    if not engine._grid_task.done():
        engine._grid_task.cancel()
```

- [ ] **Step 3: Run test (expect fail)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_engine_start_creates_grid_event_loop_task -v
```
Expected: AttributeError or assertion fail

- [ ] **Step 4: Implement — adicionar `self._grid_task: asyncio.Task | None = None` no `__init__` (depois das outras task vars), e modificar `start()` e `stop()`**

Em `__init__` (perto do `self._task = None`):
```python
        self._grid_task: asyncio.Task | None = None
```

Em `start()` (substituir o trecho que cria `self._task`):
```python
    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._main_loop())
        self._grid_task = asyncio.create_task(self._grid_event_loop())
```

Em `stop()` (cancelar ambas):
```python
    async def stop(self) -> None:
        self._running = False
        for t in (self._task, self._grid_task):
            if t is not None and not t.done():
                t.cancel()
                try:
                    await t
                except asyncio.CancelledError:
                    pass
        self._task = None
        self._grid_task = None
```

- [ ] **Step 5: Run test (expect pass)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_engine_start_creates_grid_event_loop_task -v
```
Expected: 1 passed

- [ ] **Step 6: Commit**
```bash
git add engine/__init__.py tests/test_engine_event_driven_grid.py
git commit -m "feat(engine): start/stop _grid_event_loop alongside _main_loop"
```

---

## Drift correction integration

### Task 10: Update _last_known_position after _aggressive_correct

**Files:**
- Modify: `engine/__init__.py` (`_aggressive_correct` method, ~line 1736)
- Test: `tests/test_engine_event_driven_grid.py` (append)

- [ ] **Step 1: Failing test**

```python
@pytest.mark.asyncio
async def test_aggressive_correct_updates_last_known_position():
    """After _aggressive_correct dispatches a taker, _last_known_position must be
    updated to the post-correction position so the grid event loop doesn't
    misinterpret the position change as a fill."""
    engine = _make_engine()
    engine._exchange = MagicMock()
    engine._exchange.place_long_term_order = AsyncMock()

    # Mock the post-correction position the engine should record
    new_pos = MagicMock(symbol="ARB-USD", side="short", size=20.0, entry_price=0.135)
    engine._exchange.get_position = AsyncMock(return_value=new_pos)

    # Pre-state: last_known is something else
    engine._last_known_position = MagicMock(symbol="ARB-USD", side="short", size=10.0)

    # Dispatch a corrective taker (stub args inferred from current signature)
    await engine._aggressive_correct(
        symbol="ARB-USD", drift=10.0, ref_price=0.135,
    )

    # _last_known_position should now equal the post-correction read
    assert engine._last_known_position is new_pos
```

- [ ] **Step 2: Run test (expect fail)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_aggressive_correct_updates_last_known_position -v
```
Expected: assertion fail (last_known_position not updated)

- [ ] **Step 3: Find current `_aggressive_correct` signature**
```bash
grep -nA 5 "def _aggressive_correct" engine/__init__.py | head -15
```

- [ ] **Step 4: Modify `_aggressive_correct` — adicionar no FINAL do método (after the `try/except` que dispara `place_long_term_order`)**

```python
        # Update _last_known_position so the grid event loop doesn't see this
        # position change as a fill. Read fresh after the taker resolves.
        try:
            self._last_known_position = await self._exchange.get_position(symbol)
        except Exception:
            pass  # next grid_event_iter will re-read; non-fatal
```

- [ ] **Step 5: Run test (expect pass)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_aggressive_correct_updates_last_known_position -v
```
Expected: 1 passed

- [ ] **Step 6: Commit**
```bash
git add engine/__init__.py tests/test_engine_event_driven_grid.py
git commit -m "feat(engine): update _last_known_position after _aggressive_correct so grid loop doesn't react"
```

---

## Disable old reconciler path

### Task 11: Replace _maintain_grid body — keep only structural handlers

**Files:**
- Modify: `engine/__init__.py:1336-1430` (`_maintain_grid`) and `:1432-1601` (`_reconcile_grid`)

- [ ] **Step 1: Add safety test (no regression: range change still cancels)**

```python
@pytest.mark.asyncio
async def test_maintain_grid_still_cancels_on_range_change():
    """Range change (Beefy rebalance) detected → cancel_all_stops still fires.
    The event-driven loop handles fills; _maintain_grid handles structural changes."""
    from engine import GridMakerEngine
    from state import StateHub

    state = StateHub(hedge_ratio=1.0)
    state.operation_state = "active"

    settings = MagicMock()
    settings.predictive_grid_v2 = True
    settings.dydx_symbol_token0 = "ARB-USD"
    settings.dydx_symbol_token1 = ""
    settings.token0_decimals = 18
    settings.token1_decimals = 6
    settings.uniswap_v3_pool_fee = 500
    settings.alert_webhook_url = ""

    db = MagicMock()
    db.get_active_grid_orders = AsyncMock(return_value=[])
    db.mark_grid_order_cancelled = AsyncMock()

    exchange = MagicMock()
    exchange.cancel_all_stops = AsyncMock()

    engine = GridMakerEngine(settings=settings, hub=state, db=db, exchange=exchange)
    # Simulate: previously posted with sig X, now cache shows different sig
    engine._posted_grid_signature = (1.0, -100, 100)
    cache = MagicMock()
    cache.L_main = 2.0  # different
    cache.tick_lower_main = -100
    cache.tick_upper_main = 100
    engine._hedge_model = MagicMock()
    engine._hedge_model._cache = cache

    beefy_pos = MagicMock(share=1.0)

    # p_now within range
    await engine._maintain_grid(beefy_pos=beefy_pos, p_now=0.135, oracle_prices={})

    exchange.cancel_all_stops.assert_called()
```

- [ ] **Step 2: Run test (expect pass with current impl)**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py::test_maintain_grid_still_cancels_on_range_change -v
```
Expected: 1 passed (current impl supports it)

- [ ] **Step 3: Replace `_maintain_grid` body — substituir linhas 1336-1430 em `engine/__init__.py`**

```python
    async def _maintain_grid(
        self, *, beefy_pos, p_now: float, oracle_prices: dict[str, float],
    ) -> None:
        """Mantém estrutura da grade — out-of-range guard + range change detection.
        Mutações fill-driven são responsabilidade do _grid_event_loop (100ms cadence).

        Spec: docs/superpowers/specs/2026-05-15-event-driven-grid-design.md (replaces
        self-healing reconciler from 2026-05-13).
        """
        if not self._settings.predictive_grid_v2:
            return
        if self._hedge_model is None or self._hedge_model._cache is None:
            return

        from engine.curve import tick_to_human_price

        cache = self._hedge_model._cache
        decimals0 = self._settings.token0_decimals
        decimals1 = self._settings.token1_decimals
        p_a = tick_to_human_price(
            tick=cache.tick_lower_main, decimals0=decimals0, decimals1=decimals1,
        )
        p_b = tick_to_human_price(
            tick=cache.tick_upper_main, decimals0=decimals0, decimals1=decimals1,
        )

        # Out-of-range guard: cancel + idle. Event loop will rebuild when back.
        if not (p_a <= p_now <= p_b):
            if getattr(self, "_posted_grid_signature", None) is not None:
                try:
                    await self._exchange.cancel_all_stops(
                        symbol=self._settings.dydx_symbol_token0,
                    )
                except Exception as e:
                    logger.warning(f"out-of-range cancel_all_stops failed: {e}")
                self._posted_grid_signature = None
                self._local_grid.clear()  # event loop will repopulate when in range
                metrics.grid_levels_active.set(0)
            return

        current_sig = (cache.L_main, cache.tick_lower_main, cache.tick_upper_main)
        posted_sig = getattr(self, "_posted_grid_signature", None)

        # Range change → cancel all + clear local_grid; safety_reconcile populates fresh.
        if posted_sig is not None and posted_sig != current_sig:
            metrics.beefy_range_change_total.inc()
            try:
                await self._exchange.cancel_all_stops(
                    symbol=self._settings.dydx_symbol_token0,
                )
            except Exception as e:
                logger.warning(f"range_change cancel_all_stops failed: {e}")
            try:
                active = await self._db.get_active_grid_orders()
                for row in active:
                    await self._db.mark_grid_order_cancelled(
                        row["cloid"], time.time(),
                    )
            except Exception as e:
                logger.warning(f"range_change db cleanup failed: {e}")
            self._posted_grid_signature = None
            self._local_grid.clear()
            self._last_safety_reconcile_at = 0.0  # force immediate safety reconcile

        # Initial placement: if no signature posted yet AND in range, post the
        # initial 8+8 grid via _post_initial_grid (helper from current code path).
        if posted_sig is None:
            await self._post_initial_grid(beefy_pos=beefy_pos, p_now=p_now, cache=cache)
            self._posted_grid_signature = current_sig
```

- [ ] **Step 4: Extract initial-grid post logic into `_post_initial_grid` (move from old `_reconcile_grid`'s posting branch).**

Adicionar novo método em `engine/__init__.py` (perto do `_safety_reconcile`):

```python
    async def _post_initial_grid(
        self, *, beefy_pos, p_now: float, cache,
    ) -> None:
        """Initial grid placement: 8 sells + 8 buys aligned to V3 ticks around p_now.
        Populates _local_grid and _last_known_position.
        """
        from math import log, floor
        from engine.curve import compute_grid_from_pool_ticks
        from engine.grid_state import GridStop

        decimals0 = self._settings.token0_decimals
        decimals1 = self._settings.token1_decimals
        decimal_factor = 10 ** (decimals0 - decimals1)
        tick_now = floor(log(p_now / decimal_factor) / log(1.0001))
        fee_tier = self._settings.uniswap_v3_pool_fee
        tick_spacing = {500: 10, 3000: 60, 10000: 200}.get(fee_tier, 10)
        hedge_ratio = (
            getattr(self._hub, "hedge_ratio", None)
            or self._settings.hedge_ratio
        )
        l_decimal_factor = 10 ** ((decimals0 + decimals1) / 2)
        share = float(getattr(beefy_pos, "share", 1.0) or 1.0)
        L_for_grid = float(cache.L_main) / l_decimal_factor * share

        full_grid = compute_grid_from_pool_ticks(
            L=L_for_grid,
            tick_lower=cache.tick_lower_main,
            tick_upper=cache.tick_upper_main,
            tick_spacing=tick_spacing,
            tick_now=tick_now,
            decimals0=decimals0,
            decimals1=decimals1,
            hedge_ratio=hedge_ratio,
            lighter_price_decimals=5,
            lighter_size_decimals=1,
        )

        max_orders = int(getattr(self._settings, "max_open_orders", 16) or 16)
        per_side = max_orders // 2
        sells = sorted([lv for lv in full_grid if lv.side == "sell"], key=lambda lv: -lv.price)[:per_side]
        buys = sorted([lv for lv in full_grid if lv.side == "buy"], key=lambda lv: lv.price)[:per_side]

        symbol = self._settings.dydx_symbol_token0
        buffer = float(getattr(self._settings, "grid_anticipation_buffer", 0.0) or 0.0)
        safety_frac = 0.0001

        for lv in sells + buys:
            if lv.side == "sell":
                max_trigger = p_now * (1 - safety_frac)
                if lv.price >= max_trigger:
                    continue
                trigger = min(lv.price + buffer, max_trigger)
            else:
                min_trigger = p_now * (1 + safety_frac)
                if lv.price <= min_trigger:
                    continue
                trigger = max(lv.price - buffer, min_trigger)
            cloid = self._next_cloid_for_leg(symbol)
            try:
                await self._exchange.place_stop_market(
                    symbol=symbol, side=lv.side, size=lv.size,
                    trigger_price=trigger, cloid_int=cloid,
                )
                self._local_grid[cloid] = GridStop(
                    cloid=cloid, side=lv.side, trigger_price=trigger, size=lv.size,
                )
                try:
                    await self._db.insert_grid_order(
                        cloid=str(cloid), side=lv.side, target_price=lv.price,
                        size=lv.size, placed_at=time.time(),
                        operation_id=self._hub.current_operation_id,
                        trigger_price=trigger, is_stop_order=1,
                    )
                except Exception as e:
                    logger.warning(f"initial grid db insert failed: {e}")
            except Exception as e:
                logger.warning(f"initial grid place_stop_market failed @ {lv.price}: {e}")

        # Set last_known_position to current so the event loop doesn't misinterpret
        try:
            self._last_known_position = await self._exchange.get_position(symbol)
        except Exception:
            pass
```

- [ ] **Step 5: Delete old `_reconcile_grid` method (lines ~1432-1601 in original file)**

- [ ] **Step 6: Run regression test + new test**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/test_engine_event_driven_grid.py tests/test_engine_grid.py tests/test_engine_maintain_grid.py -v --tb=short 2>&1 | tail -30
```
Expected: all green (some old tests in test_engine_maintain_grid may need adjustment if they tested deleted reconcile behavior — adjust on the fly).

- [ ] **Step 7: Commit**
```bash
git add engine/__init__.py tests/test_engine_event_driven_grid.py
git commit -m "refactor(engine): replace _maintain_grid body + delete _reconcile_grid; extract _post_initial_grid

Self-healing reconciler removed (caused 19k UNIQUE failures + Lighter
rate-limit saturation by re-posting on every tick). _maintain_grid now
only handles structural changes (range/out-of-range/initial). Fill-driven
mutations live in _grid_event_loop (100ms cadence)."
```

---

## Metrics

### Task 12: Add new Prometheus counters

**Files:**
- Modify: `engine/__init__.py` (find metrics import) and possibly `engine/metrics.py` if it exists

- [ ] **Step 1: Find metrics module**
```bash
grep -rn "metrics\." engine/__init__.py | head -10
ls engine/ | grep -i metric
```

- [ ] **Step 2: Add 3 new counters in metrics file**
```python
position_polls_total = Counter("bot_position_polls_total", "Total get_position reads from grid event loop")
grid_writes_total = Counter("bot_grid_writes_total", "Total grid write ops", ["reason"])
fill_detection_latency = Histogram("bot_grid_fill_detection_latency_seconds", "Time from position change to grid response", buckets=[0.05, 0.1, 0.2, 0.5, 1.0, 2.0])
```

- [ ] **Step 3: Wire into `_grid_event_iter` and `_apply_fills_to_grid`**

In `_grid_event_iter` after `get_position`:
```python
metrics.position_polls_total.inc()
```

In `_apply_fills_to_grid` after each cancel/post:
```python
metrics.grid_writes_total.labels(reason="fill").inc()
```

In `_safety_reconcile` for orphan cancels:
```python
metrics.grid_writes_total.labels(reason="safety").inc()
```

In `_post_initial_grid` for placements:
```python
metrics.grid_writes_total.labels(reason="initial").inc()
```

In `_aggressive_correct` (existing taker dispatch):
```python
metrics.grid_writes_total.labels(reason="drift").inc()
```

- [ ] **Step 4: Smoke-test metrics endpoint locally**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/ -k metrics -v --tb=short 2>&1 | tail -10
```
Expected: existing metrics tests still pass

- [ ] **Step 5: Commit**
```bash
git add engine/__init__.py engine/metrics.py
git commit -m "feat(metrics): add position_polls/grid_writes/fill_detection_latency counters for event-driven grid"
```

---

## Full suite + smoke

### Task 13: Full test suite

- [ ] **Step 1: Run full pytest**
```bash
C:/Users/Wallace/Python313/python.exe -m pytest tests/ -q --tb=short 2>&1 | tail -20
```
Expected: ≥ previous count of passes (388+ before this PR + ~9 new event-driven tests = ~397+); 1 pre-existing `test_settings_defaults` failure under full-suite is acceptable.

- [ ] **Step 2: If any test regressed, fix before proceeding. NO commits with red tests.**

- [ ] **Step 3: Commit if any cleanup needed**
```bash
git status
# if anything modified, commit it
git commit -am "test: cleanup post-event-driven-grid refactor"
```

---

## Deploy + verification

### Task 14: Merge to master + deploy

- [ ] **Step 1: Merge to master**
```bash
git checkout master && git merge --no-ff feature/event-driven-grid -m "Merge feature/event-driven-grid: replace self-healing reconciler with event-driven (position-diff trigger)"
git push origin master
```

- [ ] **Step 2: Deploy to prod**
```bash
ssh -i C:/Users/Wallace/.ssh/id_ed25519 root@104.248.44.6 "bash -s" <<'BASH'
cd /opt/automoney
mv /var/log/automoney.log /var/log/automoney.log.pre-event-driven.$(date +%Y%m%d_%H%M%S)
touch /var/log/automoney.log
git fetch origin master && git pull --ff-only origin master
git log --oneline -1
systemctl restart automoney
sleep 5
systemctl is-active automoney
BASH
```

- [ ] **Step 3: Wait 90s + verify counters**
```bash
ssh -i C:/Users/Wallace/.ssh/id_ed25519 root@104.248.44.6 "bash -s" <<'BASH'
sleep 90
echo "=== Lighter rate limit hits last 90s ==="
grep "L1Address ratelimit" /var/log/automoney.log | wc -l
echo "=== UNIQUE constraint failures last 90s ==="
grep -c "UNIQUE constraint" /var/log/automoney.log
echo "=== reconcile post count (deveria ser BAIXO) ==="
grep -c "reconcile post" /var/log/automoney.log || echo 0
echo "=== event-driven loop activity ==="
grep -E "_grid_event|_safety_reconcile|_apply_fills" /var/log/automoney.log | tail -10
echo "=== Lighter live orders ==="
/opt/automoney/venv/bin/python /tmp/sg.py 2>&1 | grep -E "Total|sells|buys|range" | head -6
echo "=== Prometheus metrics ==="
curl -s http://127.0.0.1:8000/metrics | grep -E "bot_grid_writes_total|bot_position_polls_total" | head -10
BASH
```
Expected:
- Lighter rate limit hits: **0**
- UNIQUE constraint failures: **0**
- `reconcile post` count: 0 (old code path deleted)
- Event-driven activity: bootstrap log + safety reconcile log
- Lighter live orders: 16 (8 sells + 8 buys)
- `bot_position_polls_total`: ~900 (10/sec × 90s)
- `bot_grid_writes_total{reason="initial"}`: 16 (or fewer if grid pre-existed via bootstrap path)

- [ ] **Step 4: Update WORKING_ON.md and handoff.md per `feedback_keep_state_fresh.md`**

```bash
# Edit WORKING_ON.md and handoff.md to reflect event-driven deployment
# (current state of bot, what changed, any remaining issues)
git add WORKING_ON.md
git commit -m "docs: WORKING_ON post event-driven grid deploy"
git push origin master
```

---

## Self-review checklist (executed inline)

**Spec coverage:**
- ✅ Algoritmo central (sell/buy fill, 3 writes) — Tasks 3, 4
- ✅ Multi-fill — Task 5
- ✅ Safety net 90s + bootstrap path — Tasks 6, 7
- ✅ Drift correction integration — Task 10
- ✅ Initial placement populates local_grid — Task 11 (`_post_initial_grid`)
- ✅ Out-of-range — Task 11 (in new `_maintain_grid`)
- ✅ Range change (Beefy rebalance) — Task 11
- ✅ State vars — Task 2
- ✅ Metrics — Task 12
- ✅ Engine lifecycle (start/stop) — Task 9

**Placeholder scan:** No TBDs/TODOs left in tasks. Code blocks complete in every step.

**Type consistency:** `_local_grid: dict[int, GridStop]` consistent throughout. `cloid_int` arg name used consistently in cancel/place calls. `_position_equal` defined in T8, used in T8 only.

**Open issue:** `_aggressive_correct` signature was inferred (Task 10 test passes `symbol`, `drift`, `ref_price`). If actual signature differs, adjust test args before run. Current code uses positional `symbol`, has internal `drift`/`size` derivation — verify with grep before T10 implementation.

---

## Execution

**Recommended:** subagent-driven-development per user's `feedback_subagent_driven_default.md` (default option 1, no need to ask again).
