"""Stub the dydx-v4-client SDK on platforms where the real wheels can't
install (Windows, no MSVC). Imported by `run.py` and `tests/conftest.py`.

Production (Linux / Fly.io) installs the real SDK via requirements.txt,
so `dydx_v4_client` is already in sys.modules and `install_dydx_stubs()`
returns early. The stub only kicks in when the import would otherwise fail.
"""
from __future__ import annotations
import sys
import types
from unittest.mock import MagicMock


def _ensure(modpath: str) -> types.ModuleType:
    mod = sys.modules.get(modpath)
    if mod is None:
        mod = types.ModuleType(modpath)
        sys.modules[modpath] = mod
    return mod


def install_dydx_stubs() -> None:
    if "dydx_v4_client" in sys.modules:
        return  # real SDK present; leave it alone

    pkg = _ensure("dydx_v4_client")
    pkg.OrderFlags = MagicMock(name="OrderFlags")

    network = _ensure("dydx_v4_client.network")
    network.make_mainnet = MagicMock(name="make_mainnet")
    network.make_testnet = MagicMock(name="make_testnet")

    node_pkg = _ensure("dydx_v4_client.node")
    node_client = _ensure("dydx_v4_client.node.client")
    node_client.NodeClient = MagicMock(name="NodeClient")
    node_pkg.client = node_client

    node_market = _ensure("dydx_v4_client.node.market")
    node_market.Market = MagicMock(name="Market")
    node_pkg.market = node_market

    indexer_pkg = _ensure("dydx_v4_client.indexer")
    rest_pkg = _ensure("dydx_v4_client.indexer.rest")
    constants = _ensure("dydx_v4_client.indexer.rest.constants")
    constants.OrderType = MagicMock(name="OrderType")
    indexer_client = _ensure("dydx_v4_client.indexer.rest.indexer_client")
    indexer_client.IndexerClient = MagicMock(name="IndexerClient")
    socket_pkg = _ensure("dydx_v4_client.indexer.socket")
    websocket = _ensure("dydx_v4_client.indexer.socket.websocket")
    websocket.IndexerSocket = MagicMock(name="IndexerSocket")
    rest_pkg.constants = constants
    rest_pkg.indexer_client = indexer_client
    socket_pkg.websocket = websocket
    indexer_pkg.rest = rest_pkg
    indexer_pkg.socket = socket_pkg

    wallet = _ensure("dydx_v4_client.wallet")
    wallet.Wallet = MagicMock(name="Wallet")

    pkg.network = network
    pkg.node = node_pkg
    pkg.indexer = indexer_pkg
    pkg.wallet = wallet

    v4_proto = _ensure("v4_proto")
    proto_pkg = _ensure("v4_proto.dydxprotocol")
    proto_clob = _ensure("v4_proto.dydxprotocol.clob")
    order_pb2 = _ensure("v4_proto.dydxprotocol.clob.order_pb2")
    order_pb2.Order = MagicMock(name="ProtoOrder")
    proto_clob.order_pb2 = order_pb2
    proto_pkg.clob = proto_clob
    v4_proto.dydxprotocol = proto_pkg
