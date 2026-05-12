"""Tests for /health/engine endpoint — Fly's loop watchdog."""
from __future__ import annotations

import time
import pytest
from starlette.testclient import TestClient
from app import create_app


def _build_client():
    """Build a test client with a hub instance whose last_update we can mutate."""
    app = create_app(start_engine=False)
    return TestClient(app), app


def test_health_engine_returns_200_when_loop_recent():
    """hub.last_update within last 30s → 200 + alive=true."""
    client, app = _build_client()
    app.state.hub.last_update = time.time()
    r = client.get("/health/engine")
    assert r.status_code == 200
    body = r.json()
    assert body["alive"] is True
    assert body["iter_age_s"] < 1.0


def test_health_engine_returns_503_when_loop_stale():
    """hub.last_update older than 30s → 503 + alive=false."""
    client, app = _build_client()
    app.state.hub.last_update = time.time() - 60
    r = client.get("/health/engine")
    assert r.status_code == 503
    body = r.json()
    assert body["alive"] is False
    assert body["iter_age_s"] > 30


def test_health_engine_works_without_auth():
    """Endpoint is in BasicAuthMiddleware exclude list (Fly probes don't auth)."""
    client, app = _build_client()
    app.state.hub.last_update = time.time()
    # No Authorization header
    r = client.get("/health/engine")
    assert r.status_code == 200
