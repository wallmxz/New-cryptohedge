import pytest
import dataclasses
from unittest.mock import AsyncMock, MagicMock, patch
from engine.pair_factory import build_lifecycle


@pytest.fixture
def mock_settings():
    """Use real Settings dataclass shape so dataclasses.replace works."""
    from config import Settings
    s = Settings(
        auth_user="admin", auth_pass="pass",
        wallet_address="0xWALLET", wallet_private_key="0x" + "1" * 64,
        arbitrum_rpc_url="https://rpc", arbitrum_rpc_fallback="",
        clm_vault_address="0xVAULT_OLD", clm_pool_address="0xPOOL_OLD",
        dydx_mnemonic="m", dydx_address="d", dydx_network="mainnet",
        dydx_subaccount=0, dydx_symbol="ETH-USD",
        alert_webhook_url="",
        max_open_orders=200, hedge_ratio=1.0,
        threshold_aggressive=0.01,
        active_exchange="dydx",
        pool_token0_symbol="WETH", pool_token1_symbol="USDC",
        pool_token1_is_stable=True, pool_token1_usd_price=1.0,
        uniswap_v3_router_address="0xROUTER",
        token0_address="0xWETH", token1_address="0xUSDC",
        token0_decimals=18, token1_decimals=6,
        slippage_bps=10, uniswap_v3_pool_fee=500,
    )
    return s


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.get_pair_from_cache = AsyncMock(return_value=None)
    return db


@pytest.fixture
def mock_hub():
    return MagicMock()


@pytest.fixture
def mock_w3():
    w3 = MagicMock()
    w3.eth = MagicMock()
    w3.to_checksum_address = lambda a: a
    w3.eth.contract = MagicMock()
    return w3


@pytest.fixture
def mock_account():
    a = MagicMock()
    a.address = "0xWALLET"
    return a


@pytest.fixture
def mock_exchange():
    return MagicMock()


@pytest.mark.asyncio
async def test_build_lifecycle_raises_when_vault_not_in_cache(
    mock_settings, mock_hub, mock_db, mock_exchange, mock_w3, mock_account,
):
    mock_db.get_pair_from_cache = AsyncMock(return_value=None)
    with pytest.raises(ValueError, match="not in cache"):
        await build_lifecycle(
            settings=mock_settings, hub=mock_hub, db=mock_db,
            exchange=mock_exchange,
            selected_vault_id="0xMISSING",
            w3=mock_w3, account=mock_account,
        )


@pytest.mark.asyncio
async def test_build_lifecycle_raises_for_cross_pair(
    mock_settings, mock_hub, mock_db, mock_exchange, mock_w3, mock_account,
):
    mock_db.get_pair_from_cache = AsyncMock(return_value={
        "vault_id": "0xV2", "is_usd_pair": 0,
        "token0_address": "0xARB", "token1_address": "0xWETH",
        "token0_decimals": 18, "token1_decimals": 18,
        "pool_address": "0xPOOL", "pool_fee": 3000, "dydx_perp": "ARB-USD",
        "token0_symbol": "ARB", "token1_symbol": "WETH",
    })
    with pytest.raises(ValueError, match="cross-pair"):
        await build_lifecycle(
            settings=mock_settings, hub=mock_hub, db=mock_db,
            exchange=mock_exchange,
            selected_vault_id="0xV2",
            w3=mock_w3, account=mock_account,
        )


@pytest.mark.asyncio
async def test_build_lifecycle_returns_lifecycle_with_pair_settings(
    mock_settings, mock_hub, mock_db, mock_exchange, mock_w3, mock_account,
):
    """Successful build returns OperationLifecycle with settings overridden by pair data."""
    mock_db.get_pair_from_cache = AsyncMock(return_value={
        "vault_id": "0xV1", "is_usd_pair": 1,
        "token0_address": "0xWETH_NEW", "token1_address": "0xUSDC_NEW",
        "token0_decimals": 18, "token1_decimals": 6,
        "pool_address": "0xPOOL_NEW", "pool_fee": 500, "dydx_perp": "ETH-USD",
        "token0_symbol": "WETH", "token1_symbol": "USDC",
    })

    lifecycle = await build_lifecycle(
        settings=mock_settings, hub=mock_hub, db=mock_db,
        exchange=mock_exchange,
        selected_vault_id="0xV1",
        w3=mock_w3, account=mock_account,
    )

    # Lifecycle's settings should reflect the pair (not the original)
    assert lifecycle._settings.token0_address == "0xWETH_NEW"
    assert lifecycle._settings.token1_address == "0xUSDC_NEW"
    assert lifecycle._settings.clm_vault_address == "0xV1"
    assert lifecycle._settings.clm_pool_address == "0xPOOL_NEW"


@pytest.mark.asyncio
async def test_build_lifecycle_raises_for_exotic_decimals(
    mock_settings, mock_hub, mock_db, mock_exchange, mock_w3, mock_account,
):
    """WBTC (8 dec) should be rejected at factory level."""
    mock_db.get_pair_from_cache = AsyncMock(return_value={
        "vault_id": "0xV3", "is_usd_pair": 1,
        "token0_address": "0xWBTC", "token1_address": "0xUSDC",
        "token0_decimals": 8, "token1_decimals": 6,
        "pool_address": "0xPOOL3", "pool_fee": 500, "dydx_perp": "BTC-USD",
        "token0_symbol": "WBTC", "token1_symbol": "USDC",
    })
    with pytest.raises(ValueError, match="decimals"):
        await build_lifecycle(
            settings=mock_settings, hub=mock_hub, db=mock_db,
            exchange=mock_exchange,
            selected_vault_id="0xV3",
            w3=mock_w3, account=mock_account,
        )
