"""DydxAdapter.get_oracle_prices reads from /v4/perpetualMarkets."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from exchanges.dydx import DydxAdapter


@pytest.mark.asyncio
async def test_get_oracle_prices_returns_dict_per_symbol():
    indexer = MagicMock()
    indexer.markets.get_perpetual_markets = AsyncMock(return_value={
        "markets": {
            "ETH-USD": {"oraclePrice": "4000.50"},
            "ARB-USD": {"oraclePrice": "1.55"},
        }
    })
    adapter = DydxAdapter(mnemonic="m", wallet_address="dydx1test")
    adapter._indexer = indexer

    prices = await adapter.get_oracle_prices(["ETH-USD", "ARB-USD"])
    assert prices == {"ETH-USD": 4000.50, "ARB-USD": 1.55}


@pytest.mark.asyncio
async def test_get_oracle_prices_skips_missing_symbols():
    indexer = MagicMock()
    indexer.markets.get_perpetual_markets = AsyncMock(return_value={
        "markets": {"ETH-USD": {"oraclePrice": "4000"}},
    })
    adapter = DydxAdapter(mnemonic="m", wallet_address="dydx1test")
    adapter._indexer = indexer

    prices = await adapter.get_oracle_prices(["ETH-USD", "MISSING-USD"])
    assert prices["ETH-USD"] == 4000.0
    assert "MISSING-USD" not in prices


@pytest.mark.asyncio
async def test_get_oracle_prices_skips_invalid_oracle_values():
    """When oraclePrice is missing, None, or not parseable, drop that symbol."""
    indexer = MagicMock()
    indexer.markets.get_perpetual_markets = AsyncMock(return_value={
        "markets": {
            "ETH-USD": {"oraclePrice": "4000.0"},
            "ARB-USD": {"oraclePrice": None},
            "BTC-USD": {},  # no oraclePrice key
            "FOO-USD": {"oraclePrice": "not-a-number"},
        }
    })
    adapter = DydxAdapter(mnemonic="m", wallet_address="dydx1test")
    adapter._indexer = indexer

    prices = await adapter.get_oracle_prices(["ETH-USD", "ARB-USD", "BTC-USD", "FOO-USD"])
    assert prices == {"ETH-USD": 4000.0}
