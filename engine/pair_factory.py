"""Per-vault factory for OperationLifecycle.

Replaces the singleton lifecycle pattern. When user picks a pair,
DB stores selected_vault_id. At start_operation time, build_lifecycle()
reads pair info from cache and constructs the OperationLifecycle with
the right token addresses, decimals, fee tier, dYdX symbol.
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


async def build_lifecycle(
    *, settings, hub, db, exchange,
    selected_vault_id: str,
    w3, account,
):
    """Build a fresh OperationLifecycle for the given vault_id.

    Reads pair metadata from beefy_pairs_cache. Constructs UniswapExecutor,
    BeefyExecutor, pool_reader, beefy_reader with the pair's addresses
    and decimals. Returns an OperationLifecycle ready to bootstrap().

    Raises ValueError if:
    - vault_id not in cache (need refresh first)
    - pair is cross-pair (Phase 3.x scope)
    - pair has unsupported decimals
    """
    # Lazy import to avoid circular
    from engine.lifecycle import OperationLifecycle

    pair = await db.get_pair_from_cache(selected_vault_id)
    if pair is None:
        raise ValueError(
            f"Vault {selected_vault_id} not in cache. "
            f"Refresh pair list (POST /pairs/refresh)."
        )
    if not pair.get("is_usd_pair"):
        raise ValueError(
            f"Vault {selected_vault_id} is cross-pair (token1 not stable); "
            f"requires Phase 3.x dual-leg hedge."
        )

    decimals0 = int(pair["token0_decimals"])
    decimals1 = int(pair["token1_decimals"])
    if (decimals0, decimals1) != (18, 6):
        raise ValueError(
            f"Vault {selected_vault_id} has unsupported decimals "
            f"({decimals0}, {decimals1}); MVP supports (18, 6) only."
        )

    # Patch settings with pair-specific overrides
    pair_settings = dataclasses.replace(
        settings,
        token0_address=pair["token0_address"],
        token1_address=pair["token1_address"],
        token0_decimals=decimals0,
        token1_decimals=decimals1,
        clm_vault_address=pair["vault_id"],
        clm_pool_address=pair["pool_address"],
        uniswap_v3_pool_fee=int(pair["pool_fee"]),
        dydx_symbol=pair["dydx_perp"],
    )

    pool_reader = UniswapV3PoolReader(
        w3=w3, pool_address=pair["pool_address"],
        decimals0=decimals0, decimals1=decimals1,
    )
    beefy_reader = BeefyClmReader(
        w3=w3, strategy_address=pair["vault_id"],
        wallet_address=settings.wallet_address,
        decimals0=decimals0, decimals1=decimals1,
    )
    uniswap_exec = UniswapExecutor(
        w3=w3, account=account,
        router_address=settings.uniswap_v3_router_address,
    )
    beefy_exec = BeefyExecutor(
        w3=w3, account=account,
        strategy_address=pair["vault_id"],
    )

    lifecycle = OperationLifecycle(
        settings=pair_settings, hub=hub, db=db,
        exchange=exchange, uniswap=uniswap_exec, beefy=beefy_exec,
        pool_reader=pool_reader, beefy_reader=beefy_reader,
        decimals0=decimals0, decimals1=decimals1,
    )
    logger.info(
        f"Built lifecycle for vault {selected_vault_id} "
        f"({pair.get('token0_symbol')}/{pair.get('token1_symbol')})"
    )
    return lifecycle
