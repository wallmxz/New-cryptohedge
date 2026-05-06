"""Per-vault factory for OperationLifecycle.

Replaces the singleton lifecycle pattern. When user picks a pair,
DB stores selected_vault_id. At start_operation time, build_lifecycle()
reads pair info from cache and constructs the OperationLifecycle with
the right token addresses, decimals, fee tier, and dYdX symbol(s).
"""
from __future__ import annotations
import dataclasses
import logging
from typing import TYPE_CHECKING

from chains.uniswap import UniswapV3PoolReader
from chains.beefy import BeefyClmReader
from chains.uniswap_executor import UniswapExecutor
from chains.beefy_executor import BeefyExecutor

if TYPE_CHECKING:
    from engine.lifecycle import OperationLifecycle

logger = logging.getLogger(__name__)

# Decimals combos supported in MVP. WBTC (8 dec) and exotic combos are
# rejected at the factory; broaden as the curve math gets generalized.
SUPPORTED_DECIMALS_PAIR = {(18, 6), (18, 18)}


async def build_lifecycle(
    *, settings, hub, db, exchange,
    selected_vault_id: str,
    w3, account,
):
    """Build a fresh OperationLifecycle for the given vault_id.

    Reads pair metadata from beefy_pairs_cache. Validates token0 perp
    (always required), token1 perp (cross-pair only), and decimals combo.
    Returns an OperationLifecycle ready to bootstrap().

    Raises ValueError on:
    - vault_id not in cache (need refresh first)
    - cross-pair with no token1 perp (cannot dual-leg-hedge)
    - unsupported decimals combo
    """
    from engine.lifecycle import OperationLifecycle

    pair = await db.get_pair_from_cache(selected_vault_id)
    if pair is None:
        raise ValueError(
            f"Vault {selected_vault_id} not in cache. "
            f"Refresh pair list (POST /pairs/refresh)."
        )

    is_usd = bool(pair.get("is_usd_pair"))
    perp0 = pair["dydx_perp"]
    perp1 = pair.get("dydx_perp_token1") or ""

    if not is_usd and not perp1:
        raise ValueError(
            f"Cross-pair {selected_vault_id}: token1 "
            f"{pair.get('token1_symbol')} sem perp dYdX ativo, "
            f"não suporta dual-leg hedge."
        )

    decimals0 = int(pair["token0_decimals"])
    decimals1 = int(pair["token1_decimals"])
    if (decimals0, decimals1) not in SUPPORTED_DECIMALS_PAIR:
        raise ValueError(
            f"Vault {selected_vault_id} has unsupported decimals "
            f"({decimals0}, {decimals1}); MVP supports "
            f"{sorted(SUPPORTED_DECIMALS_PAIR)} only."
        )

    # Beefy CLM v2 splits state between two contracts:
    # - "vault_id" (= earnContractAddress from Beefy API) is the user-facing
    #   ERC20 vault that holds totalSupply/balanceOf for share accounting.
    # - "strategy_address" (= strategy field from Beefy API) holds the V3
    #   NFT, ranges (positionMain), and current balances.
    # Older entries in the cache may not have strategy_address yet (refresh
    # not yet run); fall back to vault_id in that case so the system at
    # least makes a single-contract attempt instead of crashing in the
    # factory. The fallback will fail at first read and surface a clear
    # error to the operator, who can then run a refresh.
    strategy_address = pair.get("strategy_address") or pair["vault_id"]

    pair_settings = dataclasses.replace(
        settings,
        dydx_symbol_token0=perp0,
        dydx_symbol_token1=perp1,
        token0_address=pair["token0_address"],
        token1_address=pair["token1_address"],
        token0_decimals=decimals0,
        token1_decimals=decimals1,
        pool_token0_symbol=pair["token0_symbol"],
        pool_token1_symbol=pair["token1_symbol"],
        clm_vault_address=pair["vault_id"],
        clm_pool_address=pair["pool_address"],
        uniswap_v3_pool_fee=int(pair["pool_fee"]),
    )

    pool_reader = UniswapV3PoolReader(
        w3=w3, pool_address=pair["pool_address"],
        decimals0=decimals0, decimals1=decimals1,
    )
    beefy_reader = BeefyClmReader(
        w3=w3, strategy_address=strategy_address,
        earn_address=pair["vault_id"],
        wallet_address=settings.wallet_address,
        decimals0=decimals0, decimals1=decimals1,
    )
    uniswap_exec = UniswapExecutor(
        w3=w3, account=account,
        router_address=settings.uniswap_v3_router_address,
    )
    beefy_exec = BeefyExecutor(
        w3=w3, account=account,
        strategy_address=strategy_address,
        earn_address=pair["vault_id"],
    )

    lifecycle = OperationLifecycle(
        settings=pair_settings, hub=hub, db=db,
        exchange=exchange, uniswap=uniswap_exec, beefy=beefy_exec,
        pool_reader=pool_reader, beefy_reader=beefy_reader,
        decimals0=decimals0, decimals1=decimals1,
    )
    logger.info(
        f"Built lifecycle for vault {selected_vault_id} "
        f"({pair['token0_symbol']}/{pair['token1_symbol']}, "
        f"{'USD-pair' if is_usd else 'cross-pair (dual-leg)'})"
    )
    return lifecycle


async def build_readers_for_vault(*, settings, db, w3, selected_vault_id: str):
    """Lightweight version of build_lifecycle that returns just the chain
    readers (UniswapV3PoolReader + BeefyClmReader) plus the per-pair
    settings overrides (decimals, perp symbols, etc).

    Used by the engine main loop so it can read the SAME pool/vault that
    `start_operation` would target — without rebuilding the whole
    lifecycle every iteration. Returns None if vault is missing from the
    cache (so the caller can keep idling).

    Returns: (pair_settings, pool_reader, beefy_reader, decimals0, decimals1)
    """
    pair = await db.get_pair_from_cache(selected_vault_id)
    if pair is None:
        return None

    decimals0 = int(pair["token0_decimals"])
    decimals1 = int(pair["token1_decimals"])
    if (decimals0, decimals1) not in SUPPORTED_DECIMALS_PAIR:
        return None

    strategy_address = pair.get("strategy_address") or pair["vault_id"]
    perp0 = pair["dydx_perp"]
    perp1 = pair.get("dydx_perp_token1") or ""

    pair_settings = dataclasses.replace(
        settings,
        dydx_symbol_token0=perp0,
        dydx_symbol_token1=perp1,
        token0_address=pair["token0_address"],
        token1_address=pair["token1_address"],
        token0_decimals=decimals0,
        token1_decimals=decimals1,
        pool_token0_symbol=pair["token0_symbol"],
        pool_token1_symbol=pair["token1_symbol"],
        clm_vault_address=pair["vault_id"],
        clm_pool_address=pair["pool_address"],
        uniswap_v3_pool_fee=int(pair["pool_fee"]),
    )
    pool_reader = UniswapV3PoolReader(
        w3=w3, pool_address=pair["pool_address"],
        decimals0=decimals0, decimals1=decimals1,
    )
    beefy_reader = BeefyClmReader(
        w3=w3, strategy_address=strategy_address,
        earn_address=pair["vault_id"],
        wallet_address=settings.wallet_address,
        decimals0=decimals0, decimals1=decimals1,
    )
    return (pair_settings, pool_reader, beefy_reader, decimals0, decimals1)
