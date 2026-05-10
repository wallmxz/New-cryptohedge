"""Settings: dydx_symbol_token0 + dydx_symbol_token1 backward-compat."""
import os
from unittest.mock import patch
from config import Settings


def _base_env() -> dict:
    return {
        "AUTH_USER": "admin", "AUTH_PASS": "p",
        "WALLET_ADDRESS": "0x1", "WALLET_PRIVATE_KEY": "0x" + "1" * 64,
        "ARBITRUM_RPC_URL": "https://rpc",
        "CLM_VAULT_ADDRESS": "0x2", "CLM_POOL_ADDRESS": "0x3",
    }


def test_dydx_symbol_token0_aliases_legacy_dydx_symbol():
    """When DYDX_SYMBOL_TOKEN0 not set but DYDX_SYMBOL is, falls back."""
    env = _base_env() | {"DYDX_SYMBOL": "ETH-USD"}
    with patch.dict(os.environ, env, clear=True):
        s = Settings.from_env()
    assert s.dydx_symbol_token0 == "ETH-USD"
    assert s.dydx_symbol_token1 == ""  # single-leg default


def test_dydx_symbol_token0_explicit_overrides_legacy():
    env = _base_env() | {
        "DYDX_SYMBOL": "ETH-USD",
        "DYDX_SYMBOL_TOKEN0": "ARB-USD",
    }
    with patch.dict(os.environ, env, clear=True):
        s = Settings.from_env()
    assert s.dydx_symbol_token0 == "ARB-USD"


def test_dydx_symbol_token1_set_for_cross_pair():
    env = _base_env() | {
        "DYDX_SYMBOL_TOKEN0": "ARB-USD",
        "DYDX_SYMBOL_TOKEN1": "ETH-USD",
    }
    with patch.dict(os.environ, env, clear=True):
        s = Settings.from_env()
    assert s.dydx_symbol_token0 == "ARB-USD"
    assert s.dydx_symbol_token1 == "ETH-USD"


def test_legacy_dydx_symbol_attr_still_works():
    """Backwards compat: existing code reads `settings.dydx_symbol`."""
    env = _base_env() | {"DYDX_SYMBOL": "ETH-USD"}
    with patch.dict(os.environ, env, clear=True):
        s = Settings.from_env()
    assert s.dydx_symbol == "ETH-USD"
