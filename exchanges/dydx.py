"""dYdX v4 exchange adapter built on dydx-v4-client SDK.

SDK deviations from the task reference code:
1. The dydx-v4-client SDK (1.x) does not re-export NodeClient, IndexerClient,
   Wallet, or IndexerSocket from the top-level package. They live at submodule
   paths.
2. `make_mainnet` is a `partial` that does NOT pre-bind rest_indexer /
   websocket_indexer / node_url (only `make_testnet` and `make_local` do).
   We supply those URLs ourselves with documented mainnet defaults.
"""
from __future__ import annotations
import logging
import time
from dataclasses import dataclass
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

    async def cancel_order(self, order_id):
        raise NotImplementedError("Implementado em Task 11")

    async def get_position(self, symbol):
        raise NotImplementedError("Implementado em Task 12")

    async def get_fills(self, symbol, since=None):
        raise NotImplementedError("Implementado em Task 12")

    async def subscribe_orderbook(self, symbol, callback):
        raise NotImplementedError("Implementado em Task 13")

    async def subscribe_fills(self, symbol, callback):
        raise NotImplementedError("Implementado em Task 13")

    def get_tick_size(self, symbol):
        meta = self._market_metas.get(symbol)
        return meta.tick_size if meta else 0.1

    def get_min_notional(self, symbol):
        meta = self._market_metas.get(symbol)
        return meta.min_notional if meta else 1.0
