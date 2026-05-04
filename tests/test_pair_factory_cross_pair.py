"""pair_factory: build_lifecycle accepts cross-pair when both perps active."""
import pytest
import dataclasses
from unittest.mock import AsyncMock, MagicMock
from engine.pair_factory import build_lifecycle


@pytest.fixture
def mock_settings():
    from config import Settings
    return Settings(
        auth_user="a", auth_pass="p",
        wallet_address="0xW", wallet_private_key="0x" + "1" * 64,
        arbitrum_rpc_url="https://rpc", arbitrum_rpc_fallback="",
        clm_vault_address="0xV", clm_pool_address="0xP",
        dydx_mnemonic="m", dydx_address="d", dydx_network="mainnet",
        dydx_subaccount=0,
        dydx_symbol_token0="ETH-USD", dydx_symbol_token1="",
        alert_webhook_url="",
        max_open_orders=200, hedge_ratio=1.0,
        threshold_aggressive=0.01,
        active_exchange="dydx",
        pool_token0_symbol="WETH", pool_token1_symbol="USDC",
        uniswap_v3_router_address="0xROUTER",
        token0_address="0xWETH", token1_address="0xUSDC",
        token0_decimals=18, token1_decimals=6,
        slippage_bps=10, uniswap_v3_pool_fee=500,
    )


@pytest.fixture
def mock_db():
    db = MagicMock()
    return db


@pytest.fixture
def mock_others():
    return {"hub": MagicMock(), "exchange": MagicMock(), "w3": MagicMock(), "account": MagicMock()}


@pytest.mark.asyncio
async def test_build_lifecycle_accepts_cross_pair_with_both_perps(mock_settings, mock_db, mock_others):
    mock_db.get_pair_from_cache = AsyncMock(return_value={
        "vault_id": "0xV1", "is_usd_pair": 0,
        "token0_address": "0xARB", "token1_address": "0xWETH",
        "token0_decimals": 18, "token1_decimals": 18,
        "pool_address": "0xPOOL", "pool_fee": 3000,
        "dydx_perp": "ARB-USD",
        "dydx_perp_token1": "ETH-USD",  # cross-pair with token1 perp
        "token0_symbol": "ARB", "token1_symbol": "WETH",
    })
    mock_others["w3"].to_checksum_address = lambda a: a

    lifecycle = await build_lifecycle(
        settings=mock_settings, db=mock_db, selected_vault_id="0xV1",
        **mock_others,
    )
    # Lifecycle settings should reflect both perps
    assert lifecycle._settings.dydx_symbol_token0 == "ARB-USD"
    assert lifecycle._settings.dydx_symbol_token1 == "ETH-USD"


@pytest.mark.asyncio
async def test_build_lifecycle_rejects_cross_pair_when_token1_perp_missing(mock_settings, mock_db, mock_others):
    mock_db.get_pair_from_cache = AsyncMock(return_value={
        "vault_id": "0xV3", "is_usd_pair": 0,
        "token0_address": "0xLDO", "token1_address": "0xWETH",
        "token0_decimals": 18, "token1_decimals": 18,
        "pool_address": "0xPOOL3", "pool_fee": 3000,
        "dydx_perp": "LDO-USD",
        "dydx_perp_token1": None,  # token1 sem perp ativo
        "token0_symbol": "LDO", "token1_symbol": "WETH",
    })
    mock_others["w3"].to_checksum_address = lambda a: a

    with pytest.raises(ValueError, match="token1.*sem perp"):
        await build_lifecycle(
            settings=mock_settings, db=mock_db, selected_vault_id="0xV3",
            **mock_others,
        )


@pytest.mark.asyncio
async def test_build_lifecycle_accepts_18_18_decimals_in_cross_pair(mock_settings, mock_db, mock_others):
    """ARB/WETH (18, 18) is now in the allowlist along with (18, 6)."""
    mock_db.get_pair_from_cache = AsyncMock(return_value={
        "vault_id": "0xV4", "is_usd_pair": 0,
        "token0_address": "0xARB", "token1_address": "0xWETH",
        "token0_decimals": 18, "token1_decimals": 18,
        "pool_address": "0xPOOL4", "pool_fee": 3000,
        "dydx_perp": "ARB-USD", "dydx_perp_token1": "ETH-USD",
        "token0_symbol": "ARB", "token1_symbol": "WETH",
    })
    mock_others["w3"].to_checksum_address = lambda a: a

    lifecycle = await build_lifecycle(
        settings=mock_settings, db=mock_db, selected_vault_id="0xV4",
        **mock_others,
    )
    assert lifecycle._decimals0 == 18
    assert lifecycle._decimals1 == 18


@pytest.mark.asyncio
async def test_build_lifecycle_still_works_for_usd_pair(mock_settings, mock_db, mock_others):
    """USD-pair (token1 stable) continues to work — backwards compat."""
    mock_db.get_pair_from_cache = AsyncMock(return_value={
        "vault_id": "0xV5", "is_usd_pair": 1,
        "token0_address": "0xWETH", "token1_address": "0xUSDC",
        "token0_decimals": 18, "token1_decimals": 6,
        "pool_address": "0xPOOL5", "pool_fee": 500,
        "dydx_perp": "ETH-USD",
        "dydx_perp_token1": None,
        "token0_symbol": "WETH", "token1_symbol": "USDC",
    })
    mock_others["w3"].to_checksum_address = lambda a: a

    lifecycle = await build_lifecycle(
        settings=mock_settings, db=mock_db, selected_vault_id="0xV5",
        **mock_others,
    )
    assert lifecycle._settings.dydx_symbol_token0 == "ETH-USD"
    assert lifecycle._settings.dydx_symbol_token1 == ""  # single-leg
