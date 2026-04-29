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


@pytest.mark.asyncio
async def test_mock_exchange_fills_when_price_crosses_buy():
    """A buy order at price P fills when simulated price drops to <= P."""
    from backtest.exchange_mock import MockExchangeAdapter
    from exchanges.base import Order

    received_fills = []
    async def on_fill(fill):
        received_fills.append(fill)

    ex = MockExchangeAdapter(symbol="ETH-USD", min_notional=0.001)
    await ex.connect()
    await ex.subscribe_fills("ETH-USD", on_fill)

    await ex.place_long_term_order(
        symbol="ETH-USD", side="buy", size=0.001, price=3000.0,
        cloid_int=1, ttl_seconds=60,
    )

    # Price moves up — no fill
    await ex.advance_to_price(3010.0, ts=1000.0)
    assert received_fills == []

    # Price drops to 3000 — order fills
    await ex.advance_to_price(2999.0, ts=2000.0)
    assert len(received_fills) == 1
    f = received_fills[0]
    assert f.side == "buy"
    assert abs(f.price - 3000.0) < 1e-9
    assert f.liquidity == "maker"


@pytest.mark.asyncio
async def test_mock_exchange_position_tracks_fills():
    """Sell fill increases short size; buy fill reduces it."""
    from backtest.exchange_mock import MockExchangeAdapter

    ex = MockExchangeAdapter(symbol="ETH-USD", min_notional=0.001)
    await ex.connect()

    async def _noop(_): pass
    await ex.subscribe_fills("ETH-USD", _noop)

    await ex.place_long_term_order(
        symbol="ETH-USD", side="sell", size=0.005, price=3000.0,
        cloid_int=1, ttl_seconds=60,
    )
    await ex.advance_to_price(3001.0, ts=1000.0)
    pos = await ex.get_position("ETH-USD")
    assert pos is not None
    assert pos.side == "short"
    assert abs(pos.size - 0.005) < 1e-9


@pytest.mark.asyncio
async def test_mock_pool_returns_current_price():
    from backtest.chain_mock import MockPoolReader

    pool = MockPoolReader()
    pool.set_price(3000.0)
    assert await pool.read_price() == 3000.0

    pool.set_price(2950.5)
    assert await pool.read_price() == 2950.5


@pytest.mark.asyncio
async def test_mock_beefy_returns_current_position():
    from backtest.chain_mock import MockBeefyReader, _BeefyPosition

    beefy = MockBeefyReader()
    beefy.set_position(
        tick_lower=-197310, tick_upper=-195303,
        amount0=0.5, amount1=1500.0, share=0.01, raw_balance=10**16,
    )
    pos = await beefy.read_position()
    assert pos.tick_lower == -197310
    assert pos.tick_upper == -195303
    assert abs(pos.amount0 - 0.5) < 1e-9
    assert abs(pos.share - 0.01) < 1e-9


@pytest.mark.asyncio
async def test_simulator_runs_synthetic_period(tmp_path):
    """Simulator runs through a tiny synthetic timeline and produces a result dict."""
    from backtest.simulator import Simulator, SimConfig

    # Synthetic timeline: 3 ETH price points, no funding, single static range
    config = SimConfig(
        vault_address="0xvault",
        pool_address="0xpool",
        start_ts=1700000000.0,
        end_ts=1700000900.0,  # 15 min
        capital_lp=300.0,
        capital_dydx=130.0,
        hedge_ratio=1.0,
        threshold_aggressive=0.01,
        max_open_orders=50,
    )

    eth_prices = [
        (1700000000.0, 3000.0),
        (1700000300.0, 3001.0),
        (1700000600.0, 2999.0),
    ]
    funding = []
    apr_history = [(1700000000.0, 0.40)]
    range_events = []  # constant range
    static_range = {
        "tick_lower": -197310, "tick_upper": -195303,
        "amount0": 0.5, "amount1": 1500.0, "share": 0.01, "raw_balance": 10**16,
    }

    sim = Simulator(
        config=config,
        eth_prices=eth_prices,
        funding=funding,
        apr_history=apr_history,
        range_events=range_events,
        static_range=static_range,
    )
    result = await sim.run()
    # Result has expected top-level keys
    assert "net_pnl" in result
    assert "fills_maker" in result
    assert "fills_taker" in result
    assert "duration_seconds" in result
    assert result["duration_seconds"] == 900


def test_report_formats_text():
    from backtest.report import format_text_report

    result = {
        "net_pnl": 174.61,
        "fills_maker": 1240,
        "fills_taker": 12,
        "lp_fees_earned": 187.40,
        "range_resets": 18,
        "out_of_range_seconds": 11520,
        "max_drawdown": -3.40,
        "duration_seconds": 86400 * 181,
        "pnl_series": [],
    }
    text = format_text_report(
        result,
        capital_lp=300.0,
        capital_dydx=130.0,
        symbol="WETH/USDC",
        start_iso="2024-01-01",
        end_iso="2024-06-30",
    )
    assert "Net PnL" in text
    assert "$174.61" in text
    assert "1240" in text
    # APR roughly: 174.61/300 = 58.2% raw return on LP over 181 days,
    # which annualizes (x365/181) to ~117.4% APR on LP. Test that some
    # plausible APR-shaped percentage shows up.
    assert "58.2%" in text or "117.4%" in text or "117." in text


def test_report_apr_calc():
    from backtest.report import annualized_apr
    # 100 net on 300 over 365 days = 33.3%
    apr = annualized_apr(net=100.0, capital=300.0, duration_seconds=365 * 86400)
    assert abs(apr - 0.3333) < 0.001
