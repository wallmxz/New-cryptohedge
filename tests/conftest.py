"""Stub the dydx-v4-client SDK so tests run on platforms where it's not installed.

The real SDK has native deps (`ed25519-blake2b`, `coincurve`, `grpcio`) that lack
prebuilt wheels for some Python/OS combos. Production runs on Linux/Fly.io where
the real SDK is available. Tests only need importable symbols — they `patch()`
the names inside `exchanges.dydx` per-test.
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


def _install_dydx_stubs() -> None:
    if "dydx_v4_client" in sys.modules:
        return  # real SDK present (Linux/Fly.io); leave it alone

    # Top-level + submodules referenced by exchanges/dydx.py
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

    # v4_proto pb2 module used for ProtoOrder
    v4_proto = _ensure("v4_proto")
    proto_pkg = _ensure("v4_proto.dydxprotocol")
    proto_clob = _ensure("v4_proto.dydxprotocol.clob")
    order_pb2 = _ensure("v4_proto.dydxprotocol.clob.order_pb2")
    order_pb2.Order = MagicMock(name="ProtoOrder")
    proto_clob.order_pb2 = order_pb2
    proto_pkg.clob = proto_clob
    v4_proto.dydxprotocol = proto_pkg


_install_dydx_stubs()
