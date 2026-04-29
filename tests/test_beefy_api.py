import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from chains.beefy_api import BeefyApiFetcher


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.upsert_beefy_pair = AsyncMock()
    db.clear_beefy_cache = AsyncMock()
    db.list_cached_pairs = AsyncMock(return_value=[])
    return db


def _arb_clm_payload():
    """Minimal Beefy CLM data shape (from /cows endpoint)."""
    return [
        {
            "id": "cow-uniswap-arb-eth-usdc",
            "chain": "arbitrum",
            "earnContractAddress": "0xVAULT1",
            "tokenAddress": "0xPOOL1",
            "depositTokenAddresses": [
                "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
                "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC native
            ],
            "tokens": [
                {"symbol": "WETH", "address": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "decimals": 18},
                {"symbol": "USDC", "address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
            ],
            "lpAddress": "0xPOOL1",
            "feeTier": "500",
            "strategyTypeId": "bell-curve",
            "tickLower": -197310,
            "tickUpper": -195303,
        },
        {
            "id": "cow-uniswap-arb-arb-eth",
            "chain": "arbitrum",
            "earnContractAddress": "0xVAULT2",
            "depositTokenAddresses": [
                "0x912CE59144191C1204E64559FE8253a0e49E6548",  # ARB
                "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
            ],
            "tokens": [
                {"symbol": "ARB", "address": "0x912CE59144191C1204E64559FE8253a0e49E6548", "decimals": 18},
                {"symbol": "WETH", "address": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1", "decimals": 18},
            ],
            "lpAddress": "0xPOOL2",
            "feeTier": "3000",
            "strategyTypeId": "wide",
        },
        {
            "id": "cow-uniswap-eth-eth-usdc",  # not arbitrum -> filter out
            "chain": "ethereum",
            "earnContractAddress": "0xVAULTETH",
            "depositTokenAddresses": [],
            "tokens": [],
        },
    ]


def _tvl_payload():
    """Beefy /tvl returns {chain: {vault_id: tvl_usd}}."""
    return {
        "arbitrum": {
            "cow-uniswap-arb-eth-usdc": 5210000.0,
            "cow-uniswap-arb-arb-eth": 1900000.0,
        }
    }


def _apy_payload():
    """Beefy /apy/breakdown returns dict per vault with apy fields."""
    return {
        "cow-uniswap-arb-eth-usdc": {"vaultApr": 0.2842, "vaultAprDaily30d": 0.2842},
        "cow-uniswap-arb-arb-eth": {"vaultApr": 0.7835, "vaultAprDaily30d": 0.7835},
    }


@pytest.mark.asyncio
async def test_refresh_writes_arbitrum_pairs_only(mock_db):
    """Filter out non-arbitrum vaults; persist arbitrum ones."""
    cows = _arb_clm_payload()
    tvl = _tvl_payload()
    apy = _apy_payload()

    async def fake_get(url, *a, **kw):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith("/cows"):
            resp.json = MagicMock(return_value=cows)
        elif url.endswith("/tvl"):
            resp.json = MagicMock(return_value=tvl)
        elif "apy/breakdown" in url:
            resp.json = MagicMock(return_value=apy)
        return resp

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(side_effect=fake_get)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    fetcher = BeefyApiFetcher(db=mock_db)
    with patch("chains.beefy_api.httpx.AsyncClient", return_value=fake_client):
        n = await fetcher.refresh(active_dydx_tickers={"ETH-USD", "ARB-USD"})

    assert n == 2  # ethereum vault filtered out
    mock_db.clear_beefy_cache.assert_awaited_once()
    assert mock_db.upsert_beefy_pair.await_count == 2


@pytest.mark.asyncio
async def test_refresh_classifies_usd_vs_cross(mock_db):
    """ETH-USDC -> is_usd_pair=True; ARB-WETH -> is_usd_pair=False."""
    cows = _arb_clm_payload()
    tvl = _tvl_payload()
    apy = _apy_payload()

    captured = []

    async def capture_upsert(*, pair):
        captured.append(pair)

    mock_db.upsert_beefy_pair = AsyncMock(side_effect=capture_upsert)

    async def fake_get(url, *a, **kw):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith("/cows"):
            resp.json = MagicMock(return_value=cows)
        elif url.endswith("/tvl"):
            resp.json = MagicMock(return_value=tvl)
        elif "apy/breakdown" in url:
            resp.json = MagicMock(return_value=apy)
        return resp

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(side_effect=fake_get)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    fetcher = BeefyApiFetcher(db=mock_db)
    with patch("chains.beefy_api.httpx.AsyncClient", return_value=fake_client):
        await fetcher.refresh(active_dydx_tickers={"ETH-USD", "ARB-USD"})

    eth_usdc = next(p for p in captured if p["vault_id"].lower() == "0xvault1".lower())
    arb_eth = next(p for p in captured if p["vault_id"].lower() == "0xvault2".lower())
    assert eth_usdc["is_usd_pair"] is True
    assert eth_usdc["dydx_perp"] == "ETH-USD"
    assert arb_eth["is_usd_pair"] is False  # token1 = WETH, not stable
    assert arb_eth["dydx_perp"] == "ARB-USD"  # token0 still has perp


@pytest.mark.asyncio
async def test_refresh_skips_vaults_without_dydx_perp(mock_db):
    """Vault whose token0 has no dYdX perp is excluded."""
    cows = [
        {
            "id": "cow-uniswap-arb-rare-usdc",
            "chain": "arbitrum",
            "earnContractAddress": "0xRARE",
            "tokens": [
                {"symbol": "RAREUNKNOWN", "address": "0xRARE_TKN", "decimals": 18},
                {"symbol": "USDC", "address": "0xaf88d065e77c8cC2239327C5EDb3A432268e5831", "decimals": 6},
            ],
            "lpAddress": "0xRARE_POOL",
            "feeTier": "3000",
        }
    ]

    async def fake_get(url, *a, **kw):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith("/cows"):
            resp.json = MagicMock(return_value=cows)
        else:
            resp.json = MagicMock(return_value={})
        return resp

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(side_effect=fake_get)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)

    fetcher = BeefyApiFetcher(db=mock_db)
    with patch("chains.beefy_api.httpx.AsyncClient", return_value=fake_client):
        n = await fetcher.refresh(active_dydx_tickers={"ETH-USD"})

    assert n == 0  # RAREUNKNOWN has no perp -> filtered
    mock_db.upsert_beefy_pair.assert_not_awaited()
