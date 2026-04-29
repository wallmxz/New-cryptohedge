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
    dydx_mnemonic: str
    dydx_address: str
    dydx_network: str
    dydx_subaccount: int
    dydx_symbol: str
    alert_webhook_url: str
    hedge_ratio: float
    # max grid orders open at once on dYdX (caps grid density)
    max_open_orders: int
    # Safety net for execution failures (bot offline, exchange congestion, price gaps).
    # In healthy operation the predictive grid drives exposure to ~0% and this never fires.
    threshold_aggressive: float
    active_exchange: str
    pool_token0_symbol: str
    pool_token1_symbol: str
    pool_token1_is_stable: bool
    pool_token1_usd_price: float

    # Phase 2.0 on-chain execution
    uniswap_v3_router_address: str
    usdc_token_address: str
    weth_token_address: str
    slippage_bps: int  # default 30 = 0.3%
    uniswap_v3_pool_fee: int  # 500 = 0.05%, 3000 = 0.30%

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
            dydx_mnemonic=os.environ.get("DYDX_MNEMONIC", ""),
            dydx_address=os.environ.get("DYDX_ADDRESS", ""),
            dydx_network=os.environ.get("DYDX_NETWORK", "mainnet"),
            dydx_subaccount=int(os.environ.get("DYDX_SUBACCOUNT", "0")),
            dydx_symbol=os.environ.get("DYDX_SYMBOL", "ETH-USD"),
            alert_webhook_url=os.environ.get("ALERT_WEBHOOK_URL", ""),
            hedge_ratio=float(os.environ.get("HEDGE_RATIO", "1.0")),
            max_open_orders=int(os.environ.get("MAX_OPEN_ORDERS", "200")),
            threshold_aggressive=float(os.environ.get("THRESHOLD_AGGRESSIVE", "0.01")),
            active_exchange=os.environ.get("ACTIVE_EXCHANGE", "hyperliquid"),
            pool_token0_symbol=os.environ.get("POOL_TOKEN0_SYMBOL", "ARB"),
            pool_token1_symbol=os.environ.get("POOL_TOKEN1_SYMBOL", "USDC"),
            pool_token1_is_stable=os.environ.get("POOL_TOKEN1_IS_STABLE", "true").lower() == "true",
            pool_token1_usd_price=float(os.environ.get("POOL_TOKEN1_USD_PRICE", "1.0")),
            uniswap_v3_router_address=os.environ.get(
                "UNISWAP_V3_ROUTER_ADDRESS",
                "0xE592427A0AEce92De3Edee1F18E0157C05861564",  # Arbitrum SwapRouter
            ),
            usdc_token_address=os.environ.get(
                "USDC_TOKEN_ADDRESS",
                "0xaf88d065e77c8cC2239327C5EDb3A432268e5831",  # Native USDC Arbitrum
            ),
            weth_token_address=os.environ.get(
                "WETH_TOKEN_ADDRESS",
                "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",  # WETH Arbitrum
            ),
            slippage_bps=int(os.environ.get("SLIPPAGE_BPS", "30")),
            uniswap_v3_pool_fee=int(os.environ.get("UNISWAP_V3_POOL_FEE", "500")),
        )
