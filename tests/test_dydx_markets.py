import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from chains.dydx_markets import DydxMarketsFetcher


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.upsert_dydx_market = AsyncMock()
    db.clear_dydx_cache = AsyncMock()
    db.get_active_dydx_tickers = AsyncMock(return_value=set())
    return db


@pytest.mark.asyncio
async def test_fetch_persists_active_markets(mock_db):
    """Fetcher writes each market to cache; only ACTIVE ones are returned active."""
    fake_response = MagicMock()
    fake_response.json = MagicMock(return_value={
        "markets": {
            "ETH-USD": {"ticker": "ETH-USD", "status": "ACTIVE"},
            "BTC-USD": {"ticker": "BTC-USD", "status": "ACTIVE"},
            "OLD-USD": {"ticker": "OLD-USD", "status": "PAUSED"},
        }
    })
    fake_response.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    fetcher = DydxMarketsFetcher(db=mock_db)
    with patch("chains.dydx_markets.httpx.AsyncClient", return_value=fake_client):
        n = await fetcher.refresh()

    assert n == 3  # all 3 written to cache
    # Verify cache cleared first
    mock_db.clear_dydx_cache.assert_awaited_once()
    # Verify each market upserted
    assert mock_db.upsert_dydx_market.await_count == 3


@pytest.mark.asyncio
async def test_fetch_handles_http_error_gracefully(mock_db):
    """If indexer is down, refresh raises but cache untouched."""
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(side_effect=Exception("connection refused"))
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    fetcher = DydxMarketsFetcher(db=mock_db)
    with patch("chains.dydx_markets.httpx.AsyncClient", return_value=fake_client):
        with pytest.raises(Exception, match="connection refused"):
            await fetcher.refresh()

    mock_db.clear_dydx_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_get_active_tickers_returns_set(mock_db):
    """get_active_tickers passes through DB query."""
    mock_db.get_active_dydx_tickers = AsyncMock(return_value={"ETH-USD", "BTC-USD"})

    fetcher = DydxMarketsFetcher(db=mock_db)
    tickers = await fetcher.get_active_tickers()

    assert tickers == {"ETH-USD", "BTC-USD"}
