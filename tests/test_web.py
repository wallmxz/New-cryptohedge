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


def test_operations_endpoints_exist(app):
    import base64
    from starlette.testclient import TestClient
    creds = base64.b64encode(b"admin:secret").decode()
    headers = {"Authorization": f"Basic {creds}"}

    client = TestClient(app)
    # GET /operations should return 200 with empty list
    resp = client.get("/operations", headers=headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)

    # GET /operations/current should return 204 when none active
    resp = client.get("/operations/current", headers=headers)
    assert resp.status_code == 204


def test_metrics_endpoint_no_auth(app):
    """GET /metrics returns 200 with Prometheus content-type, NO auth required."""
    from starlette.testclient import TestClient
    client = TestClient(app)
    resp = client.get("/metrics")
    assert resp.status_code == 200
    assert "text/plain" in resp.headers["content-type"]
    # Note: prometheus-client 0.25+ uses version=1.0.0 by default (OpenMetrics).
    # Just check we got Prometheus exposition format (any version).
    assert "version=" in resp.headers["content-type"]
    # The body should contain at least one of the registered metric names
    body = resp.text
    assert "bot_loop_duration_seconds" in body or "bot_margin_ratio" in body
