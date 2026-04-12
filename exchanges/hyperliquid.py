# exchanges/hyperliquid.py
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

WS_URL = "wss://api.hyperliquid.xyz/ws"
REST_URL = "https://api.hyperliquid.xyz"


class HyperliquidAdapter(ExchangeAdapter):
    name = "hyperliquid"

    def __init__(self, api_key: str, api_secret: str, wallet_address: str):
        self._api_key = api_key
        self._api_secret = api_secret
        self._wallet = wallet_address
        self._ws: websockets.WebSocketClientProtocol | None = None
        self._http = httpx.AsyncClient(base_url=REST_URL, timeout=10)
        self._book_callback: Callable | None = None
        self._fill_callback: Callable | None = None
        self._ws_task: asyncio.Task | None = None
        self._running = False
        self._tick_sizes: dict[str, float] = {}

    async def connect(self) -> None:
        self._running = True
        self._ws = await websockets.connect(WS_URL)
        self._ws_task = asyncio.create_task(self._ws_loop())
        logger.info("Hyperliquid WS connected")

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
                if channel == "l2Book" and self._book_callback:
                    await self._book_callback(msg.get("data", {}))
                elif channel == "user" and self._fill_callback:
                    data = msg.get("data", {})
                    if "fills" in data:
                        for f in data["fills"]:
                            fill = self._parse_fill(f)
                            await self._fill_callback(fill)
        except websockets.ConnectionClosed:
            logger.warning("Hyperliquid WS disconnected")
        except asyncio.CancelledError:
            pass

    async def subscribe_orderbook(self, symbol: str, callback: Callable[[dict], Awaitable[None]]) -> None:
        self._book_callback = callback
        sub = {"method": "subscribe", "subscription": {"type": "l2Book", "coin": symbol}}
        await self._ws.send(json.dumps(sub))

    async def subscribe_fills(self, symbol: str, callback: Callable[[Fill], Awaitable[None]]) -> None:
        self._fill_callback = callback
        sub = {"method": "subscribe", "subscription": {"type": "userFills", "user": self._wallet}}
        await self._ws.send(json.dumps(sub))

    async def place_limit_order(self, symbol: str, side: str, size: float, price: float) -> Order:
        is_buy = side == "buy"
        order_req = {
            "type": "order",
            "orders": [{"a": self._asset_index(symbol), "b": is_buy, "p": str(price), "s": str(size), "r": False, "t": {"limit": {"tif": "Gtc"}}}],
            "grouping": "na",
        }
        resp = await self._post_action(order_req)
        statuses = resp.get("response", {}).get("data", {}).get("statuses", [{}])
        status = statuses[0] if statuses else {}
        oid = status.get("resting", {}).get("oid", str(time.time()))
        return Order(order_id=str(oid), symbol=symbol, side=side, size=size, price=price, status="open")

    async def cancel_order(self, order_id: str) -> None:
        cancel_req = {"type": "cancel", "cancels": [{"a": 0, "o": int(order_id)}]}
        await self._post_action(cancel_req)

    async def get_position(self, symbol: str) -> Position | None:
        resp = await self._http.post("/info", json={"type": "clearinghouseState", "user": self._wallet})
        data = resp.json()
        for pos in data.get("assetPositions", []):
            p = pos.get("position", {})
            coin = p.get("coin", "")
            if coin == symbol and float(p.get("szi", "0")) != 0:
                size = abs(float(p["szi"]))
                side = "long" if float(p["szi"]) > 0 else "short"
                return Position(symbol=symbol, side=side, size=size, entry_price=float(p.get("entryPx", "0")), unrealized_pnl=float(p.get("unrealizedPnl", "0")))
        return None

    async def get_fills(self, symbol: str, since: float | None = None) -> list[Fill]:
        resp = await self._http.post("/info", json={"type": "userFills", "user": self._wallet})
        fills = []
        for f in resp.json():
            if f.get("coin") != symbol:
                continue
            ts = f.get("time", 0) / 1000.0
            if since and ts < since:
                continue
            fills.append(self._parse_fill(f))
        return fills

    def get_tick_size(self, symbol: str) -> float:
        return self._tick_sizes.get(symbol, 0.0001)

    def get_min_notional(self, symbol: str) -> float:
        return 10.0

    def _parse_fill(self, f: dict) -> Fill:
        return Fill(
            fill_id=str(f.get("tid", "")), order_id=str(f.get("oid", "")),
            symbol=f.get("coin", ""), side="buy" if f.get("side") == "B" else "sell",
            size=float(f.get("sz", "0")), price=float(f.get("px", "0")),
            fee=float(f.get("fee", "0")), fee_currency="USDC",
            liquidity="maker" if f.get("liquidityType") == "Maker" else "taker",
            realized_pnl=float(f.get("closedPnl", "0")),
            timestamp=f.get("time", 0) / 1000.0,
        )

    def _asset_index(self, symbol: str) -> int:
        mapping = {"BTC": 0, "ETH": 1, "ARB": 2}
        return mapping.get(symbol, 0)

    async def _post_action(self, action: dict) -> dict:
        resp = await self._http.post("/exchange", json={"action": action})
        return resp.json()
