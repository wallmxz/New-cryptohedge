import time
from decimal import Decimal

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from exchanges.dydx import DydxAdapter, MarketMeta


@pytest.mark.asyncio
async def test_dydx_get_market_meta_eth_usd(monkeypatch):
    """Market meta returns step_size, tick_size, min_notional."""
    indexer = MagicMock()
    indexer.markets.get_perpetual_markets = AsyncMock(return_value={
        "markets": {
            "ETH-USD": {
                "ticker": "ETH-USD",
                "stepSize": "0.001",
                "tickSize": "0.1",
                "atomicResolution": -9,
                "minOrderBaseQuantums": 1000000,
            }
        }
    })

    with patch("exchanges.dydx.IndexerClient", return_value=indexer):
        adapter = DydxAdapter(
            mnemonic="test", wallet_address="dydx1test", network="mainnet", subaccount=0,
        )
        adapter._indexer = indexer
        meta = await adapter.get_market_meta("ETH-USD")
        assert isinstance(meta, MarketMeta)
        assert meta.tick_size == 0.1
        assert meta.step_size == 0.001
        assert meta.atomic_resolution == -9


@pytest.mark.asyncio
async def test_dydx_place_long_term_order():
    """place_long_term_order returns Order with cloid mapped."""
    node = MagicMock()
    node.latest_block_height = AsyncMock(return_value=100)
    node.place_order = AsyncMock(return_value={"hash": "0xtxhash"})

    adapter = DydxAdapter(mnemonic="m", wallet_address="dydx1test")
    adapter._node = node
    adapter._wallet = MagicMock()
    adapter._market_metas = {"ETH-USD": MarketMeta("ETH-USD", 0.1, 0.001, -9, 1000000)}

    with patch("exchanges.dydx.Market") as MockMarket:
        market_instance = MagicMock()
        market_instance.order_id = MagicMock(return_value="oid123")
        market_instance.order = MagicMock(return_value="order_obj")
        MockMarket.return_value = market_instance
        adapter._indexer = MagicMock()
        adapter._indexer.markets.get_perpetual_markets = AsyncMock(
            return_value={"markets": {"ETH-USD": {"ticker": "ETH-USD"}}}
        )

        order = await adapter.place_long_term_order(
            symbol="ETH-USD", side="sell", size=0.001, price=3050.0,
            cloid_int=42, ttl_seconds=86400,
        )
        assert order.symbol == "ETH-USD"
        assert order.side == "sell"
        assert order.size == 0.001
        assert order.price == 3050.0
        assert order.status == "open"


@pytest.mark.asyncio
async def test_dydx_cancel_order():
    node = MagicMock()
    node.latest_block_height = AsyncMock(return_value=100)
    node.cancel_order = AsyncMock(return_value={"hash": "0xcancelhash"})

    adapter = DydxAdapter(mnemonic="m", wallet_address="dydx1test")
    adapter._node = node
    adapter._wallet = MagicMock()
    adapter._indexer = MagicMock()
    adapter._indexer.markets.get_perpetual_markets = AsyncMock(
        return_value={"markets": {"ETH-USD": {"ticker": "ETH-USD"}}}
    )

    with patch("exchanges.dydx.Market") as MockMarket:
        market_instance = MagicMock()
        market_instance.order_id = MagicMock(return_value="oid123")
        MockMarket.return_value = market_instance

        await adapter.cancel_long_term_order(symbol="ETH-USD", cloid_int=42)
        node.cancel_order.assert_called_once()


@pytest.mark.asyncio
async def test_dydx_batch_place():
    """batch_place chunks orders and places sequentially."""
    from exchanges.base import Order
    adapter = DydxAdapter(mnemonic="m", wallet_address="dydx1test")
    adapter.place_long_term_order = AsyncMock(side_effect=lambda **kw: Order(
        order_id=str(kw["cloid_int"]), symbol=kw["symbol"], side=kw["side"],
        size=kw["size"], price=kw["price"], status="open",
    ))
    placed = await adapter.batch_place([
        dict(symbol="ETH-USD", side="sell", size=0.001, price=2900.0, cloid_int=1),
        dict(symbol="ETH-USD", side="buy", size=0.001, price=3100.0, cloid_int=2),
    ])
    assert len(placed) == 2
    assert placed[0].order_id == "1"
    assert placed[1].order_id == "2"
