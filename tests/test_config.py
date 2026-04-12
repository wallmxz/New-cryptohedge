import os
import pytest
from config import Settings


def test_settings_loads_from_env(monkeypatch):
    monkeypatch.setenv("AUTH_USER", "testuser")
    monkeypatch.setenv("AUTH_PASS", "testpass")
    monkeypatch.setenv("WALLET_ADDRESS", "0xabc")
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "0xdef")
    monkeypatch.setenv("ARBITRUM_RPC_URL", "https://rpc.test")
    monkeypatch.setenv("CLM_VAULT_ADDRESS", "0xvault")
    monkeypatch.setenv("CLM_POOL_ADDRESS", "0xpool")
    monkeypatch.setenv("HEDGE_RATIO", "0.90")
    monkeypatch.setenv("MAX_EXPOSURE_PCT", "0.03")
    monkeypatch.setenv("REPOST_DEPTH", "2")
    monkeypatch.setenv("ACTIVE_EXCHANGE", "dydx")

    s = Settings.from_env()

    assert s.auth_user == "testuser"
    assert s.auth_pass == "testpass"
    assert s.wallet_address == "0xabc"
    assert s.arbitrum_rpc_url == "https://rpc.test"
    assert s.hedge_ratio == 0.90
    assert s.max_exposure_pct == 0.03
    assert s.repost_depth == 2
    assert s.active_exchange == "dydx"


def test_settings_defaults(monkeypatch):
    monkeypatch.setenv("AUTH_USER", "u")
    monkeypatch.setenv("AUTH_PASS", "p")
    monkeypatch.setenv("WALLET_ADDRESS", "0x1")
    monkeypatch.setenv("WALLET_PRIVATE_KEY", "0x2")
    monkeypatch.setenv("ARBITRUM_RPC_URL", "https://rpc")
    monkeypatch.setenv("CLM_VAULT_ADDRESS", "0x3")
    monkeypatch.setenv("CLM_POOL_ADDRESS", "0x4")

    s = Settings.from_env()

    assert s.hedge_ratio == 0.95
    assert s.max_exposure_pct == 0.05
    assert s.repost_depth == 3
    assert s.active_exchange == "hyperliquid"
