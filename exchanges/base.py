from __future__ import annotations
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Callable, Awaitable


@dataclass
class Order:
    order_id: str
    symbol: str
    side: str
    size: float
    price: float
    status: str

    @property
    def is_open(self) -> bool:
        return self.status in ("open", "partial")


@dataclass
class Fill:
    fill_id: str
    order_id: str
    symbol: str
    side: str
    size: float
    price: float
    fee: float
    fee_currency: str
    liquidity: str
    realized_pnl: float
    timestamp: float


@dataclass
class Position:
    symbol: str
    side: str
    size: float
    entry_price: float
    unrealized_pnl: float

    @property
    def notional(self) -> float:
        return self.size * self.entry_price


class ExchangeAdapter(ABC):
    name: str

    @abstractmethod
    async def connect(self) -> None: ...
    @abstractmethod
    async def disconnect(self) -> None: ...
    @abstractmethod
    async def subscribe_orderbook(self, symbol: str, callback: Callable[[dict], Awaitable[None]]) -> None: ...
    @abstractmethod
    async def subscribe_fills(self, symbol: str, callback: Callable[[Fill], Awaitable[None]]) -> None: ...
    @abstractmethod
    async def place_limit_order(self, symbol: str, side: str, size: float, price: float) -> Order: ...
    @abstractmethod
    async def cancel_order(self, order_id: str) -> None: ...
    @abstractmethod
    async def get_position(self, symbol: str) -> Position | None: ...
    @abstractmethod
    async def get_fills(self, symbol: str, since: float | None = None) -> list[Fill]: ...
    @abstractmethod
    def get_tick_size(self, symbol: str) -> float: ...
    @abstractmethod
    def get_min_notional(self, symbol: str) -> float: ...
