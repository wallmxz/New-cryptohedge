"""Deterministic in-memory exchange mock for backtesting.

Implements ExchangeAdapter interface but never makes network calls.
Orders fill when simulated price crosses their level.
"""
from __future__ import annotations
from dataclasses import dataclass
from typing import Awaitable, Callable

from exchanges.base import ExchangeAdapter, Order, Fill, Position


@dataclass
class _OpenOrder:
    cloid_int: int
    side: str
    size: float
    price: float


@dataclass
class _MarketMeta:
    ticker: str
    tick_size: float
    step_size: float
    atomic_resolution: int
    min_order_base_quantums: int

    @property
    def min_notional(self) -> float:
        return self.min_order_base_quantums / (10 ** abs(self.atomic_resolution))


class MockExchangeAdapter(ExchangeAdapter):
    name = "mock"

    def __init__(
        self,
        *,
        symbol: str | None = None,             # legacy single-symbol
        symbols: list[str] | None = None,      # new multi-symbol
        min_notional: float = 0.001,
        maker_fee: float = 0.0001,
        taker_fee: float = 0.0005,
    ):
        if symbols is None:
            if symbol is None:
                raise ValueError("Must provide either `symbol` or `symbols`")
            symbols = [symbol]
        self._symbols = symbols
        self._maker_fee = maker_fee
        self._taker_fee = taker_fee

        # Per-symbol state
        self._open_orders: dict[str, dict[int, _OpenOrder]] = {s: {} for s in symbols}
        self._position_size: dict[str, float] = {s: 0.0 for s in symbols}
        self._position_entry: dict[str, float] = {s: 0.0 for s in symbols}
        self._last_price: dict[str, float] = {s: 0.0 for s in symbols}

        # Cross-margin: single collateral pool shared across all positions
        self._collateral: float = 130.0

        self._book_callback: Callable[[dict], Awaitable[None]] | None = None
        self._fill_callback: Callable[[Fill], Awaitable[None]] | None = None
        self._fill_id_seq = 0
        self._meta = _MarketMeta(
            ticker=symbols[0],  # legacy: meta is for the first/only symbol
            tick_size=0.1,
            step_size=min_notional,
            atomic_resolution=-9,
            min_order_base_quantums=int(min_notional * 1e9),
        )

    async def connect(self) -> None:
        pass

    async def disconnect(self) -> None:
        pass

    async def get_market_meta(self, symbol: str) -> _MarketMeta:
        return self._meta  # all symbols share the meta in mock

    async def place_long_term_order(
        self,
        *,
        symbol: str,
        side: str,
        size: float,
        price: float,
        cloid_int: int,
        ttl_seconds: int = 86400,
    ) -> Order:
        # Margin gate: real dYdX rejects orders that would breach the leverage
        # cap. The mock had no such check, which let the engine's
        # _aggressive_correct re-fire pattern stack takers to absurd notionals
        # in the simulator (root cause of the -$502M PnL bug). Apply a
        # conservative 5x leverage cap on hypothetical post-fill *combined*
        # notional across all legs (cross-margin). Closing/reducing orders are
        # always allowed.
        signed_delta = size if side == "buy" else -size
        new_size = self._position_size[symbol] + signed_delta

        # Compute hypothetical total notional across all legs
        total_notional = 0.0
        for s in self._symbols:
            ref = self._last_price[s] if self._last_price[s] > 0 else (price if s == symbol else 0)
            sz = new_size if s == symbol else self._position_size[s]
            total_notional += abs(sz) * ref

        max_notional = max(0.0, self._collateral * 5.0)
        delta_grew = abs(new_size) > abs(self._position_size[symbol])
        if total_notional > max_notional and delta_grew:
            raise ValueError(
                f"Margin insufficient: total notional ${total_notional:.2f} > "
                f"5x collateral ${max_notional:.2f}"
            )

        order_obj = _OpenOrder(
            cloid_int=cloid_int, side=side, size=size, price=price,
        )
        # If the order is already crossed by the last seen price, fill it
        # immediately (mirrors a marketable limit). Without this, an order
        # placed after the price has already arrived at its level never fills,
        # because the per-step cross predicate requires strict prev/price
        # transition.
        last = self._last_price.get(symbol, 0.0)
        already_crossed = (
            (side == "buy" and last > 0 and last <= price)
            or (side == "sell" and last > 0 and last >= price)
        )
        if already_crossed:
            await self._apply_fill(symbol, order_obj, ts=0.0)
        else:
            self._open_orders[symbol][cloid_int] = order_obj
        return Order(
            order_id=str(cloid_int),
            symbol=symbol,
            side=side,
            size=size,
            price=price,
            status="open",
        )

    async def cancel_long_term_order(self, *, symbol: str, cloid_int: int) -> None:
        self._open_orders.get(symbol, {}).pop(cloid_int, None)

    async def batch_place(self, orders: list[dict]) -> list[Order]:
        placed = []
        for spec in orders:
            placed.append(await self.place_long_term_order(**spec))
        return placed

    async def batch_cancel(self, items: list[dict]) -> int:
        cancelled = 0
        for spec in items:
            try:
                await self.cancel_long_term_order(**spec)
                cancelled += 1
            except Exception:
                pass
        return cancelled

    async def get_position(self, symbol: str) -> Position | None:
        ps = self._position_size.get(symbol, 0.0)
        if abs(ps) < 1e-12:
            return None
        side = "short" if ps < 0 else "long"
        unreal = (self._position_entry[symbol] - self._last_price[symbol]) * ps
        return Position(
            symbol=symbol,
            side=side,
            size=abs(ps),
            entry_price=self._position_entry[symbol],
            unrealized_pnl=unreal,
        )

    async def get_oracle_prices(self, symbols: list[str]) -> dict[str, float]:
        """Returns the simulator-driven last price per symbol."""
        return {s: self._last_price.get(s, 0.0) for s in symbols}

    async def get_collateral(self) -> float:
        return self._collateral

    async def get_fills(self, symbol: str, since: float | None = None) -> list[Fill]:
        return []

    async def subscribe_orderbook(
        self, symbol: str, callback: Callable[[dict], Awaitable[None]]
    ) -> None:
        self._book_callback = callback

    async def subscribe_fills(
        self, symbol: str, callback: Callable[[Fill], Awaitable[None]]
    ) -> None:
        self._fill_callback = callback

    async def get_open_orders_cloids(self, symbol: str) -> list[str]:
        return [str(c) for c in self._open_orders.get(symbol, {}).keys()]

    def get_tick_size(self, symbol: str) -> float:
        return self._meta.tick_size

    def get_min_notional(self, symbol: str) -> float:
        return self._meta.min_notional

    # Backtest-specific API ------------------------------------------------

    async def advance_to_prices(self, prices: dict[str, float], *, ts: float) -> None:
        """Step prices for each symbol; fire fills crossed in this step."""
        for sym, price in prices.items():
            await self._advance_symbol(sym, price, ts)

    async def advance_to_price(self, price: float, *, ts: float) -> None:
        """Single-symbol back-compat: advance the first/only symbol."""
        sym = self._symbols[0]
        await self._advance_symbol(sym, price, ts)

    async def _advance_symbol(self, symbol: str, price: float, ts: float) -> None:
        prev = self._last_price[symbol]
        self._last_price[symbol] = price

        # Determine which open orders cross this price step
        to_fill: list[_OpenOrder] = []
        for cloid, order in list(self._open_orders[symbol].items()):
            if order.side == "buy":
                # Buy fills when price <= order.price
                if (prev == 0 and price <= order.price) or (prev > order.price >= price):
                    to_fill.append(order)
            else:  # sell
                # Sell fills when price >= order.price
                if (prev == 0 and price >= order.price) or (prev < order.price <= price):
                    to_fill.append(order)

        for order in to_fill:
            self._open_orders[symbol].pop(order.cloid_int, None)
            await self._apply_fill(symbol, order, ts=ts)

    async def _apply_fill(self, symbol: str, order: _OpenOrder, *, ts: float) -> None:
        # Update position (per-symbol)
        signed_delta = order.size if order.side == "buy" else -order.size
        new_size = self._position_size[symbol] + signed_delta
        if abs(self._position_size[symbol]) > 1e-12 and (
            (self._position_size[symbol] > 0) == (signed_delta > 0)
        ):
            # Same direction — weighted average entry
            denom = self._position_size[symbol] + signed_delta
            self._position_entry[symbol] = (
                (self._position_entry[symbol] * self._position_size[symbol]
                 + order.price * signed_delta) / denom
                if abs(denom) > 1e-12
                else order.price
            )
        elif abs(self._position_size[symbol]) < 1e-12:
            self._position_entry[symbol] = order.price
        # else closing or flipping — keep entry of remaining (simplification)
        self._position_size[symbol] = new_size

        # Fees
        fee = order.size * order.price * self._maker_fee
        self._collateral -= fee

        self._fill_id_seq += 1
        fill = Fill(
            fill_id=str(self._fill_id_seq),
            order_id=str(order.cloid_int),
            symbol=symbol,
            side=order.side,
            size=order.size,
            price=order.price,
            fee=fee,
            fee_currency="USDC",
            liquidity="maker",
            realized_pnl=0.0,
            timestamp=ts,
        )
        if self._fill_callback:
            await self._fill_callback(fill)

    def apply_funding(
        self, rate_per_period: float, ts: float, symbol: str | None = None
    ) -> None:
        """Apply a single funding period to the open short notional.

        Convention: positive rate = longs pay shorts, so short receives.
        Defaults to the first symbol when `symbol` is None (single-leg compat).
        """
        sym = symbol or self._symbols[0]
        ps = self._position_size.get(sym, 0.0)
        if abs(ps) < 1e-12:
            return
        notional = abs(ps) * self._last_price[sym]
        # Bot is short -> if rate > 0, bot receives; if rate < 0, bot pays.
        # We model as a credit/debit on collateral.
        delta = (
            (rate_per_period * notional)
            if ps < 0
            else (-rate_per_period * notional)
        )
        self._collateral += delta
