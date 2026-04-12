import base64
import pytest
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


def test_health_no_auth(app):
    client = TestClient(app)
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_dashboard_requires_auth(app):
    client = TestClient(app)
    resp = client.get("/", follow_redirects=False)
    assert resp.status_code == 401


def test_dashboard_with_valid_auth(app):
    client = TestClient(app)
    creds = base64.b64encode(b"admin:secret").decode()
    resp = client.get("/", headers={"Authorization": f"Basic {creds}"})
    assert resp.status_code == 200
