import pytest
from backtest.cache import Cache
from unittest.mock import AsyncMock, MagicMock, patch
import json


@pytest.mark.asyncio
async def test_cache_set_get_string(tmp_path):
    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    await cache.set("k1", "value1")
    assert await cache.get("k1") == "value1"
    assert await cache.get("missing") is None
    await cache.close()


@pytest.mark.asyncio
async def test_cache_overwrites(tmp_path):
    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    await cache.set("k1", "first")
    await cache.set("k1", "second")
    assert await cache.get("k1") == "second"
    await cache.close()


@pytest.mark.asyncio
async def test_cache_persists_across_instances(tmp_path):
    path = str(tmp_path / "c.db")
    c1 = Cache(path)
    await c1.initialize()
    await c1.set("k", "persisted")
    await c1.close()
    c2 = Cache(path)
    await c2.initialize()
    assert await c2.get("k") == "persisted"
    await c2.close()


@pytest.mark.asyncio
async def test_fetch_eth_prices_uses_cache(tmp_path):
    from backtest.data import DataFetcher
    from backtest.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()

    fetcher = DataFetcher(cache=cache)

    # Pre-populate cache with a known result
    cached_payload = json.dumps([[1700000000.0, 2000.5], [1700000300.0, 2001.0]])
    await cache.set("eth_prices:1700000000:1700000600:300", cached_payload)

    result = await fetcher.fetch_eth_prices(start=1700000000, end=1700000600, interval=300)
    assert result == [(1700000000.0, 2000.5), (1700000300.0, 2001.0)]
    await cache.close()


@pytest.mark.asyncio
async def test_fetch_eth_prices_calls_api_on_miss(tmp_path):
    from backtest.data import DataFetcher
    from backtest.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    fetcher = DataFetcher(cache=cache)

    # Mock httpx to return a Coinbase-like response
    fake_response = MagicMock()
    fake_response.json = MagicMock(return_value=[
        # Coinbase candles: [time, low, high, open, close, volume]
        [1700000600, 1999.0, 2002.0, 2000.0, 2001.0, 100.0],
        [1700000300, 1998.0, 2001.5, 2000.5, 2000.5, 80.0],
        [1700000000, 1997.0, 2001.0, 2000.0, 2000.5, 50.0],
    ])
    fake_response.raise_for_status = MagicMock()

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("backtest.data.httpx.AsyncClient", return_value=fake_client):
        result = await fetcher.fetch_eth_prices(start=1700000000, end=1700000600, interval=300)

    assert len(result) == 3
    # Sorted ascending by timestamp
    assert result[0][0] < result[1][0] < result[2][0]
    # Cached
    cached = await cache.get("eth_prices:1700000000:1700000600:300")
    assert cached is not None
    await cache.close()


@pytest.mark.asyncio
async def test_fetch_dydx_funding_uses_cache(tmp_path):
    from backtest.data import DataFetcher
    from backtest.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    fetcher = DataFetcher(cache=cache)

    cached = json.dumps([[1700000000.0, 0.0001], [1700003600.0, -0.00005]])
    await cache.set("dydx_funding:ETH-USD:1700000000:1700007200", cached)

    result = await fetcher.fetch_dydx_funding(symbol="ETH-USD", start=1700000000, end=1700007200)
    assert result == [(1700000000.0, 0.0001), (1700003600.0, -0.00005)]
    await cache.close()


@pytest.mark.asyncio
async def test_fetch_dydx_funding_calls_indexer_on_miss(tmp_path):
    from backtest.data import DataFetcher
    from backtest.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    fetcher = DataFetcher(cache=cache)

    fake_response = MagicMock()
    fake_response.json = MagicMock(return_value={
        "historicalFunding": [
            {"effectiveAt": "2023-11-15T00:00:00Z", "rate": "0.000125"},
            {"effectiveAt": "2023-11-15T01:00:00Z", "rate": "-0.000050"},
        ]
    })
    fake_response.raise_for_status = MagicMock()
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("backtest.data.httpx.AsyncClient", return_value=fake_client):
        result = await fetcher.fetch_dydx_funding(symbol="ETH-USD", start=1700000000, end=1700007200)

    assert len(result) == 2
    assert result[0][1] == 0.000125
    await cache.close()


@pytest.mark.asyncio
async def test_fetch_beefy_apr_history_uses_cache(tmp_path):
    from backtest.data import DataFetcher
    from backtest.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    fetcher = DataFetcher(cache=cache)

    cached = json.dumps([[1700000000.0, 0.45], [1700086400.0, 0.50]])
    await cache.set("beefy_apr:0xvault:1700000000:1700172800", cached)

    result = await fetcher.fetch_beefy_apr_history(
        vault="0xvault", start=1700000000, end=1700172800,
    )
    assert result == [(1700000000.0, 0.45), (1700086400.0, 0.50)]
    await cache.close()


@pytest.mark.asyncio
async def test_fetch_beefy_apr_history_falls_back_constant(tmp_path):
    """If Beefy API doesn't return useful data, fetcher falls back to a constant APR."""
    from backtest.data import DataFetcher
    from backtest.cache import Cache

    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    fetcher = DataFetcher(cache=cache, fallback_apr=0.40)

    # Mock httpx to return empty response
    fake_response = MagicMock()
    fake_response.json = MagicMock(return_value={})
    fake_response.raise_for_status = MagicMock()
    fake_client = AsyncMock()
    fake_client.get = AsyncMock(return_value=fake_response)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    with patch("backtest.data.httpx.AsyncClient", return_value=fake_client):
        result = await fetcher.fetch_beefy_apr_history(
            vault="0xvault", start=1700000000, end=1700172800,
        )

    # 2 days = 2 daily samples
    assert len(result) >= 2
    for ts, apr in result:
        assert apr == 0.40
    await cache.close()
