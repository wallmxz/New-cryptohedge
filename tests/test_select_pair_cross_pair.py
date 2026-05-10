"""POST /pairs/select must accept cross-pair vaults with both perps active."""
import pytest
from unittest.mock import AsyncMock, MagicMock
from starlette.testclient import TestClient
from starlette.applications import Starlette
from starlette.routing import Route
from web.routes import select_pair


def _make_app(db):
    app = Starlette(routes=[Route("/pairs/select", select_pair, methods=["POST"])])
    app.state.db = db
    return app


def test_select_pair_accepts_cross_pair_with_both_perps():
    """Cross-pair (is_usd_pair=False) with dydx_perp_token1 set must be selectable."""
    db = MagicMock()
    db.get_pair_from_cache = AsyncMock(return_value={
        "vault_id": "0xV1",
        "is_usd_pair": 0,
        "token0_decimals": 18,
        "token1_decimals": 18,
        "dydx_perp": "ARB-USD",
        "dydx_perp_token1": "ETH-USD",
    })
    db.set_selected_vault_id = AsyncMock()

    app = _make_app(db)
    with TestClient(app) as client:
        r = client.post("/pairs/select", json={"vault_id": "0xV1"})
    assert r.status_code == 200
    body = r.json()
    assert body["selected_vault_id"] == "0xV1"


def test_select_pair_rejects_cross_pair_without_token1_perp():
    """Cross-pair without dydx_perp_token1 cannot dual-leg hedge."""
    db = MagicMock()
    db.get_pair_from_cache = AsyncMock(return_value={
        "vault_id": "0xV2",
        "is_usd_pair": 0,
        "token0_decimals": 18,
        "token1_decimals": 18,
        "dydx_perp": "ARB-USD",
        "dydx_perp_token1": None,
    })
    db.set_selected_vault_id = AsyncMock()

    app = _make_app(db)
    with TestClient(app) as client:
        r = client.post("/pairs/select", json={"vault_id": "0xV2"})
    assert r.status_code == 400
    assert "perp" in r.json()["error"].lower()


def test_select_pair_still_accepts_usd_pair_18_6():
    """Existing USD-pair selection (WETH/USDC) must still work."""
    db = MagicMock()
    db.get_pair_from_cache = AsyncMock(return_value={
        "vault_id": "0xV3",
        "is_usd_pair": 1,
        "token0_decimals": 18,
        "token1_decimals": 6,
        "dydx_perp": "ETH-USD",
        "dydx_perp_token1": None,
    })
    db.set_selected_vault_id = AsyncMock()

    app = _make_app(db)
    with TestClient(app) as client:
        r = client.post("/pairs/select", json={"vault_id": "0xV3"})
    assert r.status_code == 200


def test_select_pair_rejects_unsupported_decimals():
    """WBTC pairs (8 decimals) still rejected."""
    db = MagicMock()
    db.get_pair_from_cache = AsyncMock(return_value={
        "vault_id": "0xV4",
        "is_usd_pair": 1,
        "token0_decimals": 8,
        "token1_decimals": 6,
        "dydx_perp": "BTC-USD",
        "dydx_perp_token1": None,
    })
    db.set_selected_vault_id = AsyncMock()

    app = _make_app(db)
    with TestClient(app) as client:
        r = client.post("/pairs/select", json={"vault_id": "0xV4"})
    assert r.status_code == 400
    assert "decimals" in r.json()["error"].lower()
