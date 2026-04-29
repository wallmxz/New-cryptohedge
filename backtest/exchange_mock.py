"""Deterministic in-memory exchange mock for backtesting.

Implements ExchangeAdapter interface but never makes network calls.
Orders fill when simulated price crosses their level.
"""
from __future__ import annotations
import asyncio
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
        symbol: str,
        min_notional: float = 0.001,
        maker_fee: float = 0.0001,
        taker_fee: float = 0.0005,
    ):
        self._symbol = symbol
        self._maker_fee = maker_fee
        self._taker_fee = taker_fee
        self._open_orders: dict[int, _OpenOrder] = {}
        self._position_size: float = 0.0  # signed: + long, - short
        self._position_entry: float = 0.0  # weighted avg
        self._collateral: float = 130.0
        self._book_callback: Callable[[dict], Awaitable[None]] | None = None
        self._fill_callback: Callable[[Fill], Awaitable[None]] | None = None
        self._last_price: float = 0.0
        self._fill_id_seq = 0
        self._meta = _MarketMeta(
            ticker=symbol,
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
        return self._meta

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
        # conservative 5x leverage cap on hypothetical post-fill notional.
        # Closing/reducing orders are always allowed.
        signed_delta = size if side == "buy" else -size
        hypothetical_size = self._position_size + signed_delta
        ref_price = price if price > 0 else self._last_price
        hypothetical_notional = abs(hypothetical_size) * ref_price
        max_notional = max(0.0, self._collateral * 5.0)
        if (
            hypothetical_notional > max_notional
            and abs(hypothetical_size) > abs(self._position_size)
        ):
            raise ValueError(
                f"Margin insufficient: notional ${hypothetical_notional:.2f} exceeds "
                f"5x collateral ${max_notional:.2f}"
            )

        self._open_orders[cloid_int] = _OpenOrder(
            cloid_int=cloid_int, side=side, size=size, price=price,
        )
        return Order(
            order_id=str(cloid_int),
            symbol=symbol,
            side=side,
            size=size,
            price=price,
            status="open",
        )

    async def place_limit_order(
        self, symbol: str, side: str, size: float, price: float
    ) -> Order:
        return await self.place_long_term_order(
            symbol=symbol,
            side=side,
            size=size,
            price=price,
            cloid_int=int(asyncio.get_event_loop().time() * 1000) % (2**31),
        )

    async def cancel_long_term_order(self, *, symbol: str, cloid_int: int) -> None:
        self._open_orders.pop(cloid_int, None)

    async def cancel_order(self, order_id: str) -> None:
        try:
            self._open_orders.pop(int(order_id), None)
        except ValueError:
            pass

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
        if abs(self._position_size) < 1e-12:
            return None
        side = "short" if self._position_size < 0 else "long"
        unreal = (self._position_entry - self._last_price) * self._position_size
        return Position(
            symbol=symbol,
            side=side,
            size=abs(self._position_size),
            entry_price=self._position_entry,
            unrealized_pnl=unreal,
        )

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
        return [str(c) for c in self._open_orders.keys()]

    def get_tick_size(self, symbol: str) -> float:
        return self._meta.tick_size

    def get_min_notional(self, symbol: str) -> float:
        return self._meta.min_notional

    # Backtest-specific API ------------------------------------------------

    async def advance_to_price(self, price: float, *, ts: float) -> None:
        """Advance the mock clock to a new price, firing fills for crossed orders."""
        prev = self._last_price
        self._last_price = price

        # Determine which open orders cross this price step
        to_fill: list[_OpenOrder] = []
        for cloid, order in list(self._open_orders.items()):
            if order.side == "buy":
                # Buy fills when price <= order.price
                if (prev == 0 and price <= order.price) or (prev > order.price >= price):
                    to_fill.append(order)
            else:  # sell
                # Sell fills when price >= order.price
                if (prev == 0 and price >= order.price) or (prev < order.price <= price):
                    to_fill.append(order)

        for order in to_fill:
            self._open_orders.pop(order.cloid_int, None)
            await self._apply_fill(order, ts=ts)

    async def _apply_fill(self, order: _OpenOrder, *, ts: float) -> None:
        # Update position
        signed_delta = order.size if order.side == "buy" else -order.size
        new_size = self._position_size + signed_delta
        if abs(self._position_size) > 1e-12 and (
            (self._position_size > 0) == (signed_delta > 0)
        ):
            # Same direction — weighted average entry
            denom = self._position_size + signed_delta
            self._position_entry = (
                (self._position_entry * self._position_size + order.price * signed_delta)
                / denom
                if abs(denom) > 1e-12
                else order.price
            )
        elif abs(self._position_size) < 1e-12:
            self._position_entry = order.price
        # else closing or flipping — keep entry of remaining (simplification)
        self._position_size = new_size

        # Fees
        fee = order.size * order.price * self._maker_fee
        self._collateral -= fee

        self._fill_id_seq += 1
        fill = Fill(
            fill_id=str(self._fill_id_seq),
            order_id=str(order.cloid_int),
            symbol=self._symbol,
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

    def apply_funding(self, rate_per_period: float, ts: float) -> None:
        """Apply a single funding period to the open short notional.

        Convention: positive rate = longs pay shorts, so short receives.
        """
        if abs(self._position_size) < 1e-12:
            return
        notional = abs(self._position_size) * self._last_price
        # Bot is short -> if rate > 0, bot receives; if rate < 0, bot pays.
        # We model as a credit/debit on collateral.
        delta = (
            (rate_per_period * notional)
            if self._position_size < 0
            else (-rate_per_period * notional)
        )
        self._collateral += delta
