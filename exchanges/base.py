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
    async def get_position(self, symbol: str) -> Position | None: ...
    @abstractmethod
    async def get_oracle_prices(self, symbols: list[str]) -> dict[str, float]: ...
    @abstractmethod
    async def get_fills(self, symbol: str, since: float | None = None) -> list[Fill]: ...
    @abstractmethod
    def get_tick_size(self, symbol: str) -> float: ...
    @abstractmethod
    def get_min_notional(self, symbol: str) -> float: ...

    def subscribe_funding(
        self, callback: "Callable[..., Awaitable[None]]",
    ) -> None:
        """Register a callback fired once per funding payment received from
        the exchange. Default: no-op (adapters that support funding history
        override this). Engine relies on the override to populate the
        operation's funding_paid_token0/1 accumulators.
        """
        return None

    async def get_trade_pnl_since(
        self, start_ts: float, end_ts: float,
    ) -> tuple[float, float] | None:
        """Returns (trade_pnl_baseline, trade_pnl_latest) cumulative
        trade_pnl from the venue's account-pnl endpoint. The caller
        subtracts baseline from latest to get pnl during the window.
        Default: not supported (returns None) — adapters that integrate
        the venue's cumulative-pnl endpoint override this."""
        return None
