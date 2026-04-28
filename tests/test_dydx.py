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
