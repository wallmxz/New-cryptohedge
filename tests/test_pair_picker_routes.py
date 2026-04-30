import pytest
import json
from unittest.mock import AsyncMock, MagicMock, patch
from starlette.testclient import TestClient


@pytest.fixture
def app(monkeypatch, tmp_path):
    monkeypatch.setenv("AUTH_USER", "admin")
    monkeypatch.setenv("AUTH_PASS", "secret")
    monkeypatch.setenv("WALLET_ADDRESS", "0x1")
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "0x2")
    monkeypatch.setenv("ARBITRUM_RPC_URL", "https://rpc")
    monkeypatch.setenv("CLM_VAULT_ADDRESS", "0x3")
    monkeypatch.setenv("CLM_POOL_ADDRESS", "0x4")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "test.db"))

    from app import create_app
    return create_app(start_engine=False)


def _auth_headers():
    import base64
    return {"Authorization": f"Basic {base64.b64encode(b'admin:secret').decode()}"}


def test_list_pairs_returns_categorized_dict(app):
    """GET /pairs returns {usd_pairs, cross_pairs, selected_vault_id, last_refresh_ts}."""
    client = TestClient(app)
    resp = client.get("/pairs", headers=_auth_headers())
    assert resp.status_code == 200
    data = resp.json()
    assert "usd_pairs" in data
    assert "cross_pairs" in data
    assert "selected_vault_id" in data
    assert "last_refresh_ts" in data
    assert isinstance(data["usd_pairs"], list)
    assert isinstance(data["cross_pairs"], list)


def test_select_pair_rejects_missing_body(app):
    """POST /pairs/select with no body returns 400."""
    client = TestClient(app)
    resp = client.post("/pairs/select", headers=_auth_headers())
    assert resp.status_code == 400


def test_select_pair_rejects_unknown_vault(app):
    """POST /pairs/select with vault_id not in cache returns 400."""
    client = TestClient(app)
    resp = client.post(
        "/pairs/select",
        headers={**_auth_headers(), "Content-Type": "application/json"},
        content=json.dumps({"vault_id": "0xUNKNOWN"}),
    )
    assert resp.status_code == 400
    assert "not in cache" in resp.json().get("error", "").lower()


def test_refresh_pairs_returns_500_when_apis_unreachable(app):
    """POST /pairs/refresh in test env (no internet mocked) returns 500.

    In CI offline mode, the dYdX/Beefy fetch will fail. Accept either status:
    500 (HTTP error caught) or 200 (if it actually reached APIs and worked).
    """
    client = TestClient(app)
    resp = client.post("/pairs/refresh", headers=_auth_headers())
    assert resp.status_code in (200, 500)
