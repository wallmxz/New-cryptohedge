import pytest
from unittest.mock import AsyncMock, MagicMock
from engine.pair_resolver import build_pair_list, format_pair_for_ui


@pytest.fixture
def mock_db():
    db = MagicMock()
    db.list_cached_pairs = AsyncMock(return_value=[])
    db.get_selected_vault_id = AsyncMock(return_value=None)
    return db


@pytest.mark.asyncio
async def test_build_pair_list_separates_usd_and_cross(mock_db):
    mock_db.list_cached_pairs = AsyncMock(return_value=[
        {
            "vault_id": "0xV1", "chain": "arbitrum", "pool_address": "0xP1",
            "token0_address": "0xWETH", "token0_symbol": "WETH", "token0_decimals": 18,
            "token1_address": "0xUSDC", "token1_symbol": "USDC", "token1_decimals": 6,
            "pool_fee": 500, "manager": "bell-curve",
            "tick_lower": -197310, "tick_upper": -195303,
            "tvl_usd": 5210000, "apy_30d": 0.2842,
            "is_usd_pair": 1, "dydx_perp": "ETH-USD",
            "token0_logo_url": "https://logo/weth", "token1_logo_url": "https://logo/usdc",
            "fetched_at": 1730000000,
        },
        {
            "vault_id": "0xV2", "chain": "arbitrum", "pool_address": "0xP2",
            "token0_address": "0xARB", "token0_symbol": "ARB", "token0_decimals": 18,
            "token1_address": "0xWETH", "token1_symbol": "WETH", "token1_decimals": 18,
            "pool_fee": 3000, "manager": "wide",
            "tick_lower": None, "tick_upper": None,
            "tvl_usd": 1900000, "apy_30d": 0.7835,
            "is_usd_pair": 0, "dydx_perp": "ARB-USD",
            "token0_logo_url": "https://logo/arb", "token1_logo_url": "https://logo/weth",
            "fetched_at": 1730000000,
        },
    ])

    result = await build_pair_list(db=mock_db)

    assert len(result["usd_pairs"]) == 1
    assert len(result["cross_pairs"]) == 1
    assert result["usd_pairs"][0]["pair"] == "WETH-USDC"
    assert result["cross_pairs"][0]["pair"] == "ARB-WETH"
    assert result["selected_vault_id"] is None


@pytest.mark.asyncio
async def test_build_pair_list_includes_selected_id(mock_db):
    mock_db.list_cached_pairs = AsyncMock(return_value=[])
    mock_db.get_selected_vault_id = AsyncMock(return_value="0xVCURRENT")

    result = await build_pair_list(db=mock_db)

    assert result["selected_vault_id"] == "0xVCURRENT"


def test_format_pair_for_ui_usd_pair():
    raw = {
        "vault_id": "0xV1",
        "token0_symbol": "WETH", "token1_symbol": "USDC",
        "token0_address": "0xWETH", "token1_address": "0xUSDC",
        "token0_decimals": 18, "token1_decimals": 6,
        "manager": "bell-curve", "pool_fee": 500,
        "tvl_usd": 5210000, "apy_30d": 0.2842,
        "is_usd_pair": 1, "dydx_perp": "ETH-USD",
        "tick_lower": -197310, "tick_upper": -195303,
        "token0_logo_url": "https://logo/weth", "token1_logo_url": "https://logo/usdc",
        "pool_address": "0xPOOL",
    }
    formatted = format_pair_for_ui(raw)
    assert formatted["pair"] == "WETH-USDC"
    assert formatted["selectable"] is True
    # Uniswap V3 fee param 500 = 0.05% = 0.0005 fraction (NOT 5%)
    assert formatted["pool_fee_pct"] == 0.0005
    assert formatted["pool_fee_label"] == "0.05%"
    assert formatted["dydx_perp"] == "ETH-USD"


def test_format_pair_for_ui_cross_pair_without_token1_perp_not_selectable():
    """Cross-pair without dydx_perp_token1 is not selectable: cannot dual-leg hedge."""
    raw = {
        "vault_id": "0xV2",
        "token0_symbol": "LDO", "token1_symbol": "WETH",
        "token0_address": "0xLDO", "token1_address": "0xWETH",
        "token0_decimals": 18, "token1_decimals": 18,
        "manager": "wide", "pool_fee": 3000,
        "tvl_usd": 1900000, "apy_30d": 0.7835,
        "is_usd_pair": 0, "dydx_perp": "LDO-USD",
        "dydx_perp_token1": None,
        "tick_lower": None, "tick_upper": None,
        "token0_logo_url": "https://logo/ldo", "token1_logo_url": "https://logo/weth",
        "pool_address": "0xPOOL2",
    }
    formatted = format_pair_for_ui(raw)
    assert formatted["selectable"] is False
    assert "token1 sem perp" in formatted["reason"]


def test_format_pair_for_ui_cross_pair_with_both_perps_selectable():
    """Cross-pair with both perps active (e.g. ARB/WETH) is now selectable."""
    raw = {
        "vault_id": "0xV2B",
        "token0_symbol": "ARB", "token1_symbol": "WETH",
        "token0_address": "0xARB", "token1_address": "0xWETH",
        "token0_decimals": 18, "token1_decimals": 18,
        "manager": "wide", "pool_fee": 3000,
        "tvl_usd": 1900000, "apy_30d": 0.7835,
        "is_usd_pair": 0, "dydx_perp": "ARB-USD",
        "dydx_perp_token1": "ETH-USD",
        "tick_lower": None, "tick_upper": None,
        "token0_logo_url": "https://logo/arb", "token1_logo_url": "https://logo/weth",
        "pool_address": "0xPOOL2",
    }
    formatted = format_pair_for_ui(raw)
    assert formatted["selectable"] is True
    assert formatted["reason"] is None
    assert formatted["dydx_perp_token1"] == "ETH-USD"


def test_format_pair_for_ui_filters_exotic_decimals():
    """MVP only supports decimals (18, 6) for USD pairs.
    WBTC-USDC (8, 6) is included in cache but flagged not selectable in UI."""
    raw = {
        "vault_id": "0xV3",
        "token0_symbol": "WBTC", "token1_symbol": "USDC",
        "token0_address": "0xWBTC", "token1_address": "0xUSDC",
        "token0_decimals": 8, "token1_decimals": 6,  # exotic
        "manager": "bell", "pool_fee": 500,
        "tvl_usd": 2800000, "apy_30d": 0.195,
        "is_usd_pair": 1, "dydx_perp": "BTC-USD",
        "tick_lower": -50000, "tick_upper": -45000,
        "token0_logo_url": "https://logo/wbtc", "token1_logo_url": "https://logo/usdc",
        "pool_address": "0xPOOL3",
    }
    formatted = format_pair_for_ui(raw)
    assert formatted["selectable"] is False
    assert "decimals" in formatted["reason"].lower()
