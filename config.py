from __future__ import annotations
import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    auth_user: str
    auth_pass: str
    wallet_address: str
    wallet_private_key: str
    arbitrum_rpc_url: str
    arbitrum_rpc_fallback: str
    clm_vault_address: str
    clm_pool_address: str
    hyperliquid_api_key: str
    hyperliquid_api_secret: str
    hyperliquid_symbol: str
    dydx_mnemonic: str
    dydx_symbol: str
    alert_webhook_url: str
    hedge_ratio: float
    max_exposure_pct: float
    repost_depth: int
    active_exchange: str
    pool_token0_symbol: str
    pool_token1_symbol: str
    pool_token1_is_stable: bool
    pool_token1_usd_price: float

    @classmethod
    def from_env(cls) -> Settings:
        return cls(
            auth_user=os.environ["AUTH_USER"],
            auth_pass=os.environ["AUTH_PASS"],
            wallet_address=os.environ["WALLET_ADDRESS"],
            wallet_private_key=os.environ["WALLET_PRIVATE_KEY"],
            arbitrum_rpc_url=os.environ["ARBITRUM_RPC_URL"],
            arbitrum_rpc_fallback=os.environ.get("ARBITRUM_RPC_FALLBACK", ""),
            clm_vault_address=os.environ["CLM_VAULT_ADDRESS"],
            clm_pool_address=os.environ["CLM_POOL_ADDRESS"],
            hyperliquid_api_key=os.environ.get("HYPERLIQUID_API_KEY", ""),
            hyperliquid_api_secret=os.environ.get("HYPERLIQUID_API_SECRET", ""),
            hyperliquid_symbol=os.environ.get("HYPERLIQUID_SYMBOL", "ARB"),
            dydx_mnemonic=os.environ.get("DYDX_MNEMONIC", ""),
            dydx_symbol=os.environ.get("DYDX_SYMBOL", "ARB-USD"),
            alert_webhook_url=os.environ.get("ALERT_WEBHOOK_URL", ""),
            hedge_ratio=float(os.environ.get("HEDGE_RATIO", "0.95")),
            max_exposure_pct=float(os.environ.get("MAX_EXPOSURE_PCT", "0.05")),
            repost_depth=int(os.environ.get("REPOST_DEPTH", "3")),
            active_exchange=os.environ.get("ACTIVE_EXCHANGE", "hyperliquid"),
            pool_token0_symbol=os.environ.get("POOL_TOKEN0_SYMBOL", os.environ.get("HYPERLIQUID_SYMBOL", "ARB")),
            pool_token1_symbol=os.environ.get("POOL_TOKEN1_SYMBOL", "USDC"),
            pool_token1_is_stable=os.environ.get("POOL_TOKEN1_IS_STABLE", "true").lower() == "true",
            pool_token1_usd_price=float(os.environ.get("POOL_TOKEN1_USD_PRICE", "1.0")),
        )
