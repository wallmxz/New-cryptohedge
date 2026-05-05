import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from chains.beefy_api import BeefyApiFetcher


def _meta_lookup(addr):
    """Pretend the DB cache already has metadata for our test addresses."""
    table = {
        "0x82af49447d8a07e3bd95bd0d56f35241523fbab1": {"symbol": "WETH", "decimals": 18},
        "0xaf88d065e77c8cc2239327c5edb3a432268e5831": {"symbol": "USDC", "decimals": 6},
        "0x912ce59144191c1204e64559fe8253a0e49e6548": {"symbol": "ARB", "decimals": 18},
    }
    return table.get((addr or "").lower())


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.upsert_beefy_pair = AsyncMock()
    db.clear_beefy_cache = AsyncMock()
    db.list_cached_pairs = AsyncMock(return_value=[])
    db.get_token_metadata = AsyncMock(side_effect=lambda addr: _meta_lookup(addr))
    db.upsert_token_metadata = AsyncMock()
    return db


def _arb_clm_payload():
    """Minimal Beefy CLM data shape (from /cow-vaults endpoint)."""
    return [
        {
            "id": "cow-uniswap-arb-eth-usdc",
            "chain": "arbitrum",
            "status": "active",
            "earnContractAddress": "0xVAULT1",
            "tokenAddress": "0xPOOL1",
            "depositTokenAddresses": [
                "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
                "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC native
            ],
            "feeTier": "0.05",
            "tokenProviderId": "uniswap",
        },
        {
            "id": "cow-uniswap-arb-arb-eth",
            "chain": "arbitrum",
            "status": "active",
            "earnContractAddress": "0xVAULT2",
            "depositTokenAddresses": [
                "0x912CE59144191C1204E64559FE8253a0e49E6548",  # ARB
                "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
            ],
            "tokenAddress": "0xPOOL2",
            "feeTier": "0.30",
            "tokenProviderId": "uniswap",
        },
        {
            "id": "cow-uniswap-eth-eth-usdc",  # not arbitrum -> filter out
            "chain": "ethereum",
            "earnContractAddress": "0xVAULTETH",
            "depositTokenAddresses": [],
        },
    ]


def _tvl_payload():
    """Beefy /tvl returns {chain_id_str: {vault_id: tvl_usd}} (Arbitrum = '42161')."""
    return {
        "42161": {
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


def _make_http_mock(cows, tvl, apy):
    async def fake_get(url, *a, **kw):
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        if url.endswith("/cow-vaults"):
            resp.json = MagicMock(return_value=cows)
        elif url.endswith("/tvl"):
            resp.json = MagicMock(return_value=tvl)
        elif "apy/breakdown" in url:
            resp.json = MagicMock(return_value=apy)
        else:
            resp.json = MagicMock(return_value={})
        return resp

    fake_client = AsyncMock()
    fake_client.get = AsyncMock(side_effect=fake_get)
    fake_client.__aenter__ = AsyncMock(return_value=fake_client)
    fake_client.__aexit__ = AsyncMock(return_value=None)
    return fake_client


@pytest.mark.asyncio
async def test_refresh_writes_arbitrum_pairs_only(mock_db):
    """Filter out non-arbitrum vaults; persist arbitrum ones."""
    fake_client = _make_http_mock(_arb_clm_payload(), _tvl_payload(), _apy_payload())
    fetcher = BeefyApiFetcher(db=mock_db)
    with patch("chains.beefy_api.httpx.AsyncClient", return_value=fake_client):
        n = await fetcher.refresh(active_dydx_tickers={"ETH-USD", "ARB-USD"})

    assert n == 2  # ethereum vault filtered out
    mock_db.clear_beefy_cache.assert_awaited_once()
    assert mock_db.upsert_beefy_pair.await_count == 2


@pytest.mark.asyncio
async def test_refresh_classifies_usd_vs_cross(mock_db):
    """ETH-USDC -> is_usd_pair=True; ARB-WETH -> is_usd_pair=False."""
    captured = []

    async def capture_upsert(*, pair):
        captured.append(pair)

    mock_db.upsert_beefy_pair = AsyncMock(side_effect=capture_upsert)

    fake_client = _make_http_mock(_arb_clm_payload(), _tvl_payload(), _apy_payload())
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
async def test_refresh_skips_vaults_with_unresolvable_token(mock_db):
    """Vault whose token addresses can't be resolved is excluded."""
    cows = [
        {
            "id": "cow-uniswap-arb-rare-usdc",
            "chain": "arbitrum",
            "status": "active",
            "earnContractAddress": "0xRARE",
            "depositTokenAddresses": [
                "0x9999999999999999999999999999999999999999",  # not in mock cache
                "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC
            ],
            "tokenAddress": "0xRARE_POOL",
            "feeTier": "0.30",
        }
    ]

    fake_client = _make_http_mock(cows, {}, {})
    fetcher = BeefyApiFetcher(db=mock_db)  # no w3 → unknown tokens skipped
    with patch("chains.beefy_api.httpx.AsyncClient", return_value=fake_client):
        n = await fetcher.refresh(active_dydx_tickers={"ETH-USD"})

    assert n == 0
    mock_db.upsert_beefy_pair.assert_not_awaited()


@pytest.mark.asyncio
async def test_refresh_skips_vaults_without_dydx_perp(mock_db):
    """Vault whose token0 has no dYdX perp is excluded."""
    cows = [
        {
            "id": "cow-uniswap-arb-weth-arb",
            "chain": "arbitrum",
            "status": "active",
            "earnContractAddress": "0xV_NO_PERP_TOKEN0",
            "depositTokenAddresses": [
                "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH (resolvable)
                "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC
            ],
            "tokenAddress": "0xPOOL",
            "feeTier": "0.05",
        }
    ]

    fake_client = _make_http_mock(cows, {}, {})
    fetcher = BeefyApiFetcher(db=mock_db)
    # WETH is in our cache (so symbol resolves) but no perp ticker is active
    with patch("chains.beefy_api.httpx.AsyncClient", return_value=fake_client):
        n = await fetcher.refresh(active_dydx_tickers=set())  # no active perps

    assert n == 0
    mock_db.upsert_beefy_pair.assert_not_awaited()


@pytest.mark.asyncio
async def test_extract_pair_populates_dydx_perp_token1_for_cross_pair(mock_db):
    """When token1 is volatile (WETH) and has dYdX perp active, populate dydx_perp_token1."""
    fetcher = BeefyApiFetcher(db=mock_db)

    clm = {
        "earnContractAddress": "0xV1",
        "id": "test-arb-weth",
        "chain": "arbitrum",
            "status": "active",
        "tokenAddress": "0xPOOL",
        "depositTokenAddresses": [
            "0x912CE59144191C1204E64559FE8253a0e49E6548",  # ARB
            "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
        ],
        "feeTier": "0.30",
    }
    pair = await fetcher._extract_pair(
        clm,
        tvl_data={"42161": {"test-arb-weth": 1000}},
        apy_data={"test-arb-weth": {"vaultApr": 0.5}},
        active_dydx_tickers={"ARB-USD", "ETH-USD"},
        now=0,
    )
    assert pair is not None
    assert pair["dydx_perp"] == "ARB-USD"
    assert pair["dydx_perp_token1"] == "ETH-USD"
    assert pair["is_usd_pair"] is False


@pytest.mark.asyncio
async def test_extract_pair_dydx_perp_token1_null_for_usd_pair(mock_db):
    """USD-pair (token1 stable) leaves dydx_perp_token1 as None."""
    fetcher = BeefyApiFetcher(db=mock_db)

    clm = {
        "earnContractAddress": "0xV2",
        "id": "test-weth-usdc",
        "chain": "arbitrum",
            "status": "active",
        "tokenAddress": "0xPOOL2",
        "depositTokenAddresses": [
            "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH
            "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # USDC
        ],
        "feeTier": "0.05",
    }
    pair = await fetcher._extract_pair(
        clm,
        tvl_data={"42161": {"test-weth-usdc": 1_000_000}},
        apy_data={"test-weth-usdc": {"vaultApr": 0.15}},
        active_dydx_tickers={"ETH-USD"},
        now=0,
    )
    assert pair is not None
    assert pair["dydx_perp"] == "ETH-USD"
    assert pair["dydx_perp_token1"] is None
    assert pair["is_usd_pair"] is True
    assert pair["pool_fee"] == 500  # "0.05" pct → 500 bps


@pytest.mark.asyncio
async def test_resolve_token_persists_on_chain_reads(mock_db):
    """Cache miss + w3 available → on-chain read + DB upsert."""
    # Empty cache for this address
    mock_db.get_token_metadata = AsyncMock(return_value=None)
    mock_db.upsert_token_metadata = AsyncMock()

    # Fake w3 returning a contract whose symbol/decimals calls resolve to NEW
    fake_contract = MagicMock()
    fake_contract.functions.symbol.return_value.call = AsyncMock(return_value="NEW")
    fake_contract.functions.decimals.return_value.call = AsyncMock(return_value=12)
    fake_w3 = MagicMock()
    fake_w3.to_checksum_address = lambda a: a
    fake_w3.eth.contract = MagicMock(return_value=fake_contract)

    fetcher = BeefyApiFetcher(db=mock_db, w3=fake_w3)
    meta = await fetcher._resolve_token("0xAA00000000000000000000000000000000000001")
    assert meta == {"symbol": "NEW", "decimals": 12}
    mock_db.upsert_token_metadata.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_token_partial_erc20_failure_returns_none(mock_db):
    """If decimals() reverts, the token is unresolvable; nothing is cached."""
    mock_db.get_token_metadata = AsyncMock(return_value=None)
    mock_db.upsert_token_metadata = AsyncMock()

    fake_contract = MagicMock()
    fake_contract.functions.symbol.return_value.call = AsyncMock(return_value="WHATEVER")
    fake_contract.functions.decimals.return_value.call = AsyncMock(
        side_effect=Exception("execution reverted")
    )
    fake_w3 = MagicMock()
    fake_w3.to_checksum_address = lambda a: a
    fake_w3.eth.contract = MagicMock(return_value=fake_contract)

    fetcher = BeefyApiFetcher(db=mock_db, w3=fake_w3)
    meta = await fetcher._resolve_token("0xBB00000000000000000000000000000000000002")
    assert meta is None
    mock_db.upsert_token_metadata.assert_not_awaited()


def test_fee_tier_to_pool_fee_handles_edge_cases():
    """Non-numeric, NaN, infinite, and negative values all return 0."""
    from chains.beefy_api import _fee_tier_to_pool_fee
    assert _fee_tier_to_pool_fee("0.05") == 500
    assert _fee_tier_to_pool_fee("0.30") == 3000
    assert _fee_tier_to_pool_fee("Dynamic") == 0
    assert _fee_tier_to_pool_fee(None) == 0
    assert _fee_tier_to_pool_fee("NaN") == 0
    assert _fee_tier_to_pool_fee("-0.05") == 0
    assert _fee_tier_to_pool_fee("inf") == 0
