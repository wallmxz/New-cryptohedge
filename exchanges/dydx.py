# exchanges/dydx.py
from __future__ import annotations
import json
import time
import asyncio
import logging
from typing import Callable, Awaitable
import httpx
import websockets
from exchanges.base import ExchangeAdapter, Order, Fill, Position

logger = logging.getLogger(__name__)

INDEXER_REST = "https://indexer.dydx.trade/v4"
INDEXER_WS = "wss://indexer.dydx.trade/v4/ws"


class DydxAdapter(ExchangeAdapter):
    name = "dydx"

    def __init__(self, mnemonic: str, wallet_address: str):
        self._mnemonic = mnemonic
        self._wallet = wallet_address
        self._subaccount = 0
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._http = httpx.AsyncClient(base_url=INDEXER_REST, timeout=10)
        self._book_callback: Callable | None = None
        self._fill_callback: Callable | None = None
        self._ws_task: asyncio.Task | None = None
        self._running = False

    async def connect(self) -> None:
        self._running = True
        self._ws = await websockets.connect(INDEXER_WS)
        self._ws_task = asyncio.create_task(self._ws_loop())
        logger.info("dYdX WS connected")

    async def disconnect(self) -> None:
        self._running = False
        if self._ws_task:
            self._ws_task.cancel()
        if self._ws:
            await self._ws.close()
        await self._http.aclose()

    async def _ws_loop(self) -> None:
        try:
            async for raw in self._ws:
                msg = json.loads(raw)
                channel = msg.get("channel")
                if channel == "v4_orderbook" and self._book_callback:
                    await self._book_callback(msg.get("contents", {}))
                elif channel == "v4_subaccounts" and self._fill_callback:
                    contents = msg.get("contents", {})
                    fills = contents.get("fills", [])
                    for f in fills:
                        fill = self._parse_fill(f)
                        await self._fill_callback(fill)
        except websockets.ConnectionClosed:
            logger.warning("dYdX WS disconnected")
        except asyncio.CancelledError:
            pass

    async def subscribe_orderbook(self, symbol: str, callback: Callable[[dict], Awaitable[None]]) -> None:
        self._book_callback = callback
        sub = {"type": "subscribe", "channel": "v4_orderbook", "id": symbol}
        await self._ws.send(json.dumps(sub))

    async def subscribe_fills(self, symbol: str, callback: Callable[[Fill], Awaitable[None]]) -> None:
        self._fill_callback = callback
        sub = {"type": "subscribe", "channel": "v4_subaccounts", "id": f"{self._wallet}/{self._subaccount}"}
        await self._ws.send(json.dumps(sub))

    async def place_limit_order(self, symbol: str, side: str, size: float, price: float) -> Order:
        order_id = f"dydx-{int(time.time() * 1000)}"
        logger.info(f"dYdX place_limit_order: {side} {size} {symbol} @ {price}")
        return Order(order_id=order_id, symbol=symbol, side=side, size=size, price=price, status="open")

    async def cancel_order(self, order_id: str) -> None:
        logger.info(f"dYdX cancel_order: {order_id}")

    async def get_position(self, symbol: str) -> Position | None:
        resp = await self._http.get("/perpetualPositions", params={"address": self._wallet, "subaccountNumber": self._subaccount})
        data = resp.json()
        for pos in data.get("positions", []):
            if pos.get("market") == symbol and pos.get("status") == "OPEN":
                size = abs(float(pos["size"]))
                side = "long" if float(pos["size"]) > 0 else "short"
                return Position(symbol=symbol, side=side, size=size, entry_price=float(pos.get("entryPrice", "0")), unrealized_pnl=float(pos.get("unrealizedPnl", "0")))
        return None

    async def get_fills(self, symbol: str, since: float | None = None) -> list[Fill]:
        params = {"address": self._wallet, "subaccountNumber": self._subaccount, "ticker": symbol, "limit": 100}
        resp = await self._http.get("/fills", params=params)
        fills = []
        for f in resp.json().get("fills", []):
            fill = self._parse_fill(f)
            if since and fill.timestamp < since:
                continue
            fills.append(fill)
        return fills

    def get_tick_size(self, symbol: str) -> float:
        return 0.0001

    def get_min_notional(self, symbol: str) -> float:
        return 1.0

    def _parse_fill(self, f: dict) -> Fill:
        liquidity = f.get("liquidity", "TAKER").lower()
        if liquidity not in ("maker", "taker"):
            liquidity = "taker"
        return Fill(
            fill_id=str(f.get("id", "")), order_id=str(f.get("orderId", "")),
            symbol=f.get("market", f.get("ticker", "")),
            side=f.get("side", "BUY").lower(), size=float(f.get("size", "0")),
            price=float(f.get("price", "0")), fee=float(f.get("fee", "0")),
            fee_currency="USDC", liquidity=liquidity,
            realized_pnl=float(f.get("realizedPnl", "0")),
            timestamp=float(f.get("createdAtHeight", 0)),
        )
