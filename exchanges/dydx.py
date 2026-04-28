"""dYdX v4 exchange adapter built on dydx-v4-client SDK.

SDK deviations from the task reference code:
1. The dydx-v4-client SDK (1.x) does not re-export NodeClient, IndexerClient,
   Wallet, or IndexerSocket from the top-level package. They live at submodule
   paths.
2. `make_mainnet` is a `partial` that does NOT pre-bind rest_indexer /
   websocket_indexer / node_url (only `make_testnet` and `make_local` do).
   We supply those URLs ourselves with documented mainnet defaults.
3. `IndexerSocket` orderbook channel is `socket.order_book`, not
   `socket.markets` (the latter is the v4_markets info channel).
4. `IndexerSocket(url, on_message=...)` takes the URL positionally; its
   `on_message` is a SYNC callback receiving `(ws, parsed_dict)`. We bridge
   to async user callbacks via `asyncio.create_task`.
5. `IndexerSocket.connect()` invokes `run_forever`, which blocks. We launch
   it as a background task in `_ensure_socket`.
"""
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from dydx_v4_client import OrderFlags
from dydx_v4_client.network import make_mainnet, make_testnet
from dydx_v4_client.node.client import NodeClient
from dydx_v4_client.node.market import Market
from dydx_v4_client.indexer.rest.constants import OrderType
from dydx_v4_client.indexer.rest.indexer_client import IndexerClient
from dydx_v4_client.indexer.socket.websocket import IndexerSocket
from dydx_v4_client.wallet import Wallet
from v4_proto.dydxprotocol.clob.order_pb2 import Order as ProtoOrder

from exchanges.base import ExchangeAdapter, Order, Fill, Position

logger = logging.getLogger(__name__)

# Public dYdX v4 mainnet endpoints. The SDK does not bundle these.
MAINNET_REST_INDEXER = "https://indexer.dydx.trade"
MAINNET_WEBSOCKET_INDEXER = "wss://indexer.dydx.trade/v4/ws"
MAINNET_NODE_URL = "dydx-grpc.publicnode.com:443"


def _parse_created_at(value) -> float:
    """Parse indexer `createdAt` to unix seconds; tolerate ISO 8601, numeric, or missing."""
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        # Numeric string fallback
        try:
            return float(value)
        except ValueError:
            pass
        # ISO 8601 (e.g. "2024-05-01T12:34:56.789Z")
        try:
            iso = value.replace("Z", "+00:00")
            return datetime.fromisoformat(iso).timestamp()
        except ValueError:
            return 0.0
    return 0.0


@dataclass
class MarketMeta:
    ticker: str
    tick_size: float
    step_size: float
    atomic_resolution: int
    min_order_base_quantums: int

    @property
    def min_notional(self) -> float:
        """Min order size in display units."""
        return self.min_order_base_quantums / (10 ** abs(self.atomic_resolution))


class DydxAdapter(ExchangeAdapter):
    name = "dydx"

    def __init__(self, mnemonic: str, wallet_address: str, network: str = "mainnet",
                 subaccount: int = 0):
        self._mnemonic = mnemonic
        self._wallet_address = wallet_address
        self._subaccount = subaccount
        if network == "mainnet":
            self._network = make_mainnet(
                rest_indexer=MAINNET_REST_INDEXER,
                websocket_indexer=MAINNET_WEBSOCKET_INDEXER,
                node_url=MAINNET_NODE_URL,
            )
        else:
            self._network = make_testnet()
        self._node: NodeClient | None = None
        self._indexer: IndexerClient | None = None
        self._wallet: Wallet | None = None
        self._socket: IndexerSocket | None = None
        self._book_callback: Callable | None = None
        self._fill_callback: Callable | None = None
        self._market_metas: dict[str, MarketMeta] = {}

    async def connect(self) -> None:
        self._node = await NodeClient.connect(self._network.node)
        self._indexer = IndexerClient(self._network.rest_indexer)
        self._wallet = await Wallet.from_mnemonic(
            self._node, self._mnemonic, self._wallet_address,
        )
        logger.info(f"dYdX v4 connected (chain_id={self._network.node.chain_id})")

    async def disconnect(self) -> None:
        if self._socket:
            await self._socket.close()

    async def get_market_meta(self, symbol: str) -> MarketMeta:
        if symbol in self._market_metas:
            return self._market_metas[symbol]
        markets = await self._indexer.markets.get_perpetual_markets(symbol)
        m = markets["markets"][symbol]
        meta = MarketMeta(
            ticker=m["ticker"],
            tick_size=float(m["tickSize"]),
            step_size=float(m["stepSize"]),
            atomic_resolution=int(m["atomicResolution"]),
            min_order_base_quantums=int(m["minOrderBaseQuantums"]),
        )
        self._market_metas[symbol] = meta
        return meta

    # The remaining methods on ExchangeAdapter ABC are placeholder stubs.
    # They will be implemented in Tasks 10-13.

    async def place_long_term_order(
        self, *, symbol: str, side: str, size: float, price: float,
        cloid_int: int, ttl_seconds: int = 86400,
    ) -> Order:
        """Place a long-term limit order on dYdX v4.

        cloid_int: int 0..2^32-1 used as client_id. Must be unique per (subaccount, market).
        """
        # Need market data from indexer
        market_data = await self._indexer.markets.get_perpetual_markets(symbol)
        market = Market(market_data["markets"][symbol])

        order_id = market.order_id(
            self._wallet_address, self._subaccount, cloid_int, OrderFlags.LONG_TERM,
        )

        proto_side = ProtoOrder.Side.SIDE_SELL if side == "sell" else ProtoOrder.Side.SIDE_BUY
        good_til_block_time = int(time.time()) + ttl_seconds

        new_order = market.order(
            order_id=order_id,
            order_type=OrderType.LIMIT,
            side=proto_side,
            size=size,
            price=price,
            time_in_force=ProtoOrder.TimeInForce.TIME_IN_FORCE_UNSPECIFIED,
            reduce_only=False,
            good_til_block_time=good_til_block_time,
        )
        tx = await self._node.place_order(wallet=self._wallet, order=new_order)
        if hasattr(self._wallet, "sequence"):
            self._wallet.sequence += 1

        return Order(
            order_id=str(cloid_int),
            symbol=symbol,
            side=side,
            size=size,
            price=price,
            status="open",
        )

    async def place_limit_order(self, symbol, side, size, price):
        """ABC-required compat. Delegates to place_long_term_order with auto-generated cloid."""
        cloid = int(time.time() * 1000) % (2**31)
        return await self.place_long_term_order(
            symbol=symbol, side=side, size=size, price=price, cloid_int=cloid,
        )

    async def cancel_long_term_order(self, *, symbol: str, cloid_int: int) -> None:
        """Cancel a long-term order by its client_id."""
        market_data = await self._indexer.markets.get_perpetual_markets(symbol)
        market = Market(market_data["markets"][symbol])
        order_id = market.order_id(
            self._wallet_address, self._subaccount, cloid_int, OrderFlags.LONG_TERM,
        )
        good_til_block_time = int(time.time()) + 60
        await self._node.cancel_order(
            wallet=self._wallet,
            order_id=order_id,
            good_til_block_time=good_til_block_time,
        )
        if hasattr(self._wallet, "sequence"):
            self._wallet.sequence += 1

    async def cancel_order(self, order_id: str) -> None:
        """Generic cancel by string id (assumes default symbol)."""
        raise NotImplementedError("Use cancel_long_term_order for long-term orders")

    async def batch_place(self, orders: list[dict]) -> list[Order]:
        """Place multiple orders sequentially with small delay to avoid rate limits.

        orders: list of dicts with keys symbol, side, size, price, cloid_int (and optional ttl_seconds).
        """
        placed = []
        for spec in orders:
            try:
                o = await self.place_long_term_order(**spec)
                placed.append(o)
            except Exception as e:
                logger.error(f"Batch place failed for cloid {spec.get('cloid_int')}: {e}")
            await asyncio.sleep(0.05)  # rate limit safety
        return placed

    async def batch_cancel(self, items: list[dict]) -> int:
        """Cancel multiple orders. items: list of dicts with symbol + cloid_int."""
        cancelled = 0
        for spec in items:
            try:
                await self.cancel_long_term_order(**spec)
                cancelled += 1
            except Exception as e:
                logger.error(f"Batch cancel failed for cloid {spec.get('cloid_int')}: {e}")
            await asyncio.sleep(0.05)
        return cancelled

    async def get_position(self, symbol: str) -> Position | None:
        """Read current open position for symbol via indexer subaccount endpoint."""
        sub = await self._indexer.account.get_subaccount(
            address=self._wallet_address,
            subaccount_number=self._subaccount,
        )
        positions = sub.get("subaccount", {}).get("openPerpetualPositions", {})
        pos = positions.get(symbol)
        if not pos or pos.get("status") != "OPEN":
            return None
        raw_size = float(pos["size"])
        if raw_size == 0:
            return None
        return Position(
            symbol=symbol,
            side="long" if raw_size > 0 else "short",
            size=abs(raw_size),
            entry_price=float(pos.get("entryPrice", "0")),
            unrealized_pnl=float(pos.get("unrealizedPnl", "0")),
        )

    async def get_open_orders_cloids(self, symbol: str) -> list[str]:
        """Returns list of cloid strings for currently-open orders on this market.

        Used by the Reconciler to compare exchange-side state against DB. The
        SDK's `get_subaccount_orders` returns the raw `/v4/orders` payload —
        either a list directly or a dict wrapping `orders`. We tolerate both.
        """
        resp = await self._indexer.account.get_subaccount_orders(
            address=self._wallet_address,
            subaccount_number=self._subaccount,
            ticker=symbol,
            status="OPEN",
        )
        # The /v4/orders endpoint normally returns a list; some versions wrap
        # the result in {"orders": [...]} — handle both shapes.
        if isinstance(resp, dict):
            orders_iter = resp.get("orders", [])
        else:
            orders_iter = resp or []
        cloids: list[str] = []
        for o in orders_iter:
            cid = o.get("clientId")
            if cid is not None:
                cloids.append(str(cid))
        return cloids

    async def get_collateral(self) -> float:
        """Total collateral (equity) in subaccount, in quote (USDC)."""
        sub = await self._indexer.account.get_subaccount(
            address=self._wallet_address,
            subaccount_number=self._subaccount,
        )
        return float(sub.get("subaccount", {}).get("equity", "0"))

    async def get_fills(self, symbol: str, since: float | None = None) -> list[Fill]:
        """Fetch recent fills for symbol from indexer; filter by timestamp if since set.

        SDK note: indexer returns `createdAt` as an ISO 8601 string. We convert to
        a unix timestamp (float seconds) for the Fill dataclass and the `since`
        comparison; if parsing fails we fall back to 0.0.
        """
        resp = await self._indexer.account.get_subaccount_fills(
            address=self._wallet_address,
            subaccount_number=self._subaccount,
            ticker=symbol,
            limit=100,
        )
        fills: list[Fill] = []
        for f in resp.get("fills", []):
            ts = _parse_created_at(f.get("createdAt"))
            if since is not None and ts < since:
                continue
            liquidity = str(f.get("liquidity", "TAKER")).lower()
            if liquidity not in ("maker", "taker"):
                liquidity = "taker"
            fills.append(Fill(
                fill_id=str(f.get("id", "")),
                order_id=str(f.get("orderId", "")),
                symbol=f.get("market", symbol),
                side=str(f.get("side", "BUY")).lower(),
                size=float(f.get("size", "0")),
                price=float(f.get("price", "0")),
                fee=float(f.get("fee", "0")),
                fee_currency="USDC",
                liquidity=liquidity,
                realized_pnl=float(f.get("realizedPnl", "0")),
                timestamp=ts,
            ))
        return fills

    async def _ensure_socket(self) -> None:
        """Lazily create + connect the IndexerSocket.

        SDK note: dydx-v4-client `IndexerSocket.__init__` takes (url, header,
        on_open, on_message, ...) — there is no `websocket_indexer` keyword.
        Its `on_message` receives `(ws, parsed_dict)` thanks to the SDK's
        internal `as_json` wrapper. `connect()` calls `run_forever`, so we
        spawn it as a background task to avoid blocking the event loop.
        """
        if self._socket is None:
            self._socket = IndexerSocket(
                self._network.websocket_indexer,
                on_message=self._on_message,
            )
            asyncio.create_task(self._socket.connect())

    def _on_message(self, ws, msg: dict) -> None:
        """Synchronous WS callback (websocket-client style).

        Schedules async work onto the running loop so that user-supplied
        async callbacks can do real I/O (DB writes, hedge re-evaluation, ...).
        """
        channel = msg.get("channel")
        if channel == "v4_orderbook" and self._book_callback:
            asyncio.create_task(self._book_callback(msg.get("contents", {})))
        elif channel == "v4_subaccounts" and self._fill_callback:
            contents = msg.get("contents", {})
            for f in contents.get("fills", []):
                ts = _parse_created_at(f.get("createdAt"))
                liquidity = str(f.get("liquidity", "TAKER")).lower()
                if liquidity not in ("maker", "taker"):
                    liquidity = "taker"
                fill = Fill(
                    fill_id=str(f.get("id", "")),
                    order_id=str(f.get("orderId", "")),
                    symbol=f.get("market", ""),
                    side=str(f.get("side", "BUY")).lower(),
                    size=float(f.get("size", "0")),
                    price=float(f.get("price", "0")),
                    fee=float(f.get("fee", "0")),
                    fee_currency="USDC",
                    liquidity=liquidity,
                    realized_pnl=float(f.get("realizedPnl", "0")),
                    timestamp=ts,
                )
                asyncio.create_task(self._fill_callback(fill))

    async def subscribe_orderbook(self, symbol: str, callback) -> None:
        """Subscribe to v4_orderbook for `symbol`; callback awaited per book event.

        SDK note: orderbook lives on `IndexerSocket.order_book` (the `markets`
        attribute is the v4_markets info channel, not the orderbook).
        """
        self._book_callback = callback
        await self._ensure_socket()
        self._socket.order_book.subscribe(symbol)

    async def subscribe_fills(self, symbol: str, callback) -> None:
        """Subscribe to v4_subaccounts; fills are extracted in `_on_message`.

        SDK note: dYdX does not expose a per-market fill stream — fills are
        delivered through the subaccount channel, scoped by (address, sub#).
        We filter by `market == symbol` server-side via the channel itself
        only loosely; the message handler iterates `contents.fills`.
        """
        self._fill_callback = callback
        await self._ensure_socket()
        self._socket.subaccounts.subscribe(self._wallet_address, self._subaccount)

    def get_tick_size(self, symbol):
        meta = self._market_metas.get(symbol)
        return meta.tick_size if meta else 0.1

    def get_min_notional(self, symbol):
        meta = self._market_metas.get(symbol)
        return meta.min_notional if meta else 1.0
