"""Fetcher for Beefy CLM data + APY + TVL with DB cache.

Uses these public endpoints:
- https://api.beefy.finance/cow-vaults    (CLM list with assets + addresses)
- https://api.beefy.finance/tvl           (per-chain-id per-vault TVL)
- https://api.beefy.finance/apy/breakdown (per-vault APY breakdown)

Token symbols/decimals are fetched on-chain from the ERC20 contract (NOT
hardcoded), so support for new tokens never requires a code change. Results
are cached in the DB indefinitely since ERC20 metadata is immutable per
address.
"""
from __future__ import annotations
import asyncio
import json
import logging
import math
import os
import time
import httpx
from stables import is_stable, dydx_perp_for

logger = logging.getLogger(__name__)

BEEFY_API_BASE = "https://api.beefy.finance"
TARGET_CHAIN = "arbitrum"
CHAIN_ID_ARB = "42161"

# Canonical ERC20 ABI (shared with chains/uniswap_executor.py and
# chains/beefy_executor.py via abi/erc20.json — `symbol()` was added there
# in the same change that introduced this fetcher's on-chain reads).
_ERC20_ABI_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "abi", "erc20.json",
)
with open(_ERC20_ABI_PATH) as _f:
    _ERC20_ABI = json.load(_f)


def _fee_tier_to_pool_fee(value) -> int:
    """Convert Beefy's `feeTier` (string like '0.05', '0.30', 'Dynamic')
    to a Uniswap V3 `pool_fee` parameter (fee × 1_000_000 / 100): "0.05" → 500,
    "0.30" → 3000. Returns 0 for non-numeric, non-finite, or negative values
    so malformed entries fall through to a default-skipped state downstream
    rather than poisoning the swap route."""
    if value is None:
        return 0
    try:
        pct = float(value)  # "0.05" → 0.05
    except (TypeError, ValueError):
        return 0  # 'Dynamic' or other non-numeric strings
    if not math.isfinite(pct) or pct < 0:
        return 0
    return int(round(pct * 10000))


class BeefyApiFetcher:
    """Fetches Beefy CLMs + APY + TVL, joins them, classifies, caches in DB.

    `w3` (optional async Web3) is used to resolve ERC20 token symbol/decimals
    for any address not yet in the token_metadata_cache. If `w3` is None,
    the fetcher relies entirely on cached metadata; vaults referencing
    uncached tokens are skipped.
    """

    def __init__(self, *, db, w3=None):
        self._db = db
        self._w3 = w3
        # Circuit breaker: a dead RPC would otherwise burn 60×timeout per
        # refresh. After this many consecutive on-chain failures, disable
        # further attempts for the rest of the refresh and proceed with
        # whatever's already cached.
        self._consecutive_rpc_failures = 0
        self._rpc_failure_threshold = 3

    async def refresh(self, *, active_dydx_tickers: set[str]) -> int:
        """Force re-fetch + classify + cache. Returns number of CLMs cached.

        Filters:
        - chain == 'arbitrum'
        - token0 symbol has dYdX perp AND that perp is in active_dydx_tickers

        Note: cross-pairs (token1 not stable) ARE included in cache; UI
        filter handles selectability.
        """
        async with httpx.AsyncClient(timeout=60.0) as client:
            cows_resp, tvl_resp, apy_resp = await self._fetch_all(client)

        cows_data = cows_resp.json()
        cows = cows_data if isinstance(cows_data, list) else []
        tvl_data = tvl_resp.json()
        tvl = tvl_data if isinstance(tvl_data, dict) else {}
        apy_data = apy_resp.json()
        apy = apy_data if isinstance(apy_data, dict) else {}

        await self._db.clear_beefy_cache()
        now = time.time()
        cached_count = 0

        for clm in cows:
            if clm.get("chain") != TARGET_CHAIN:
                continue
            # Skip retired/paused vaults — Beefy's own UI hides these by default.
            # Statuses observed in /cow-vaults: "active" (~78 on Arbitrum) and
            # "eol" (~65 retired). We only consider active.
            if clm.get("status") != "active":
                continue
            try:
                pair = await self._extract_pair(clm, tvl, apy, active_dydx_tickers, now)
            except (KeyError, IndexError, ValueError) as e:
                logger.debug(f"Skipping malformed CLM {clm.get('id')}: {e}")
                continue
            if pair is None:
                continue
            await self._db.upsert_beefy_pair(pair=pair)
            cached_count += 1

        logger.info(f"Beefy refresh: cached {cached_count} CLMs (chain={TARGET_CHAIN})")
        return cached_count

    async def _fetch_all(self, client):
        cows_resp = await client.get(f"{BEEFY_API_BASE}/cow-vaults")
        cows_resp.raise_for_status()
        tvl_resp = await client.get(f"{BEEFY_API_BASE}/tvl")
        tvl_resp.raise_for_status()
        apy_resp = await client.get(f"{BEEFY_API_BASE}/apy/breakdown")
        apy_resp.raise_for_status()
        return cows_resp, tvl_resp, apy_resp

    # Per-call deadline for on-chain ERC20 metadata reads. Short enough that a
    # dead RPC endpoint can't stall a refresh covering 60+ vaults.
    _ERC20_READ_TIMEOUT_S = 5.0

    async def _resolve_token(self, address: str) -> dict | None:
        """Get {symbol, decimals} for an address. Cache-first; on miss, read
        on-chain (if w3 available) and persist; return None if both fail."""
        if not address:
            return None
        cached = await self._db.get_token_metadata(address)
        if cached is not None:
            return cached
        if self._w3 is None:
            return None
        try:
            checksum = self._w3.to_checksum_address(address)
            contract = self._w3.eth.contract(address=checksum, abi=_ERC20_ABI)
            symbol, decimals = await asyncio.wait_for(
                asyncio.gather(
                    contract.functions.symbol().call(),
                    contract.functions.decimals().call(),
                ),
                timeout=self._ERC20_READ_TIMEOUT_S,
            )
        except (asyncio.TimeoutError, Exception) as e:
            logger.debug(f"ERC20 metadata read failed for {address}: {e}")
            self._consecutive_rpc_failures += 1
            if self._consecutive_rpc_failures >= self._rpc_failure_threshold:
                logger.warning(
                    f"Disabling further on-chain reads after "
                    f"{self._consecutive_rpc_failures} consecutive RPC failures "
                    f"this refresh; remaining vaults will use cache only."
                )
                self._w3 = None
            return None
        # Successful read resets the failure counter
        self._consecutive_rpc_failures = 0
        meta = {"symbol": str(symbol).upper(), "decimals": int(decimals)}
        await self._db.upsert_token_metadata(
            address=address, symbol=meta["symbol"], decimals=meta["decimals"],
        )
        return meta

    async def _extract_pair(
        self, clm: dict, tvl_data: dict, apy_data: dict,
        active_dydx_tickers: set[str], now: float,
    ) -> dict | None:
        """Build a pair dict from raw CLM data. Returns None if should skip."""
        vault_id = clm.get("earnContractAddress") or ""
        if not vault_id:
            return None

        addresses = clm.get("depositTokenAddresses") or []
        if len(addresses) < 2:
            return None
        token0_address = addresses[0]
        token1_address = addresses[1]

        # Resolve symbols + decimals on-chain (cached). If either token is
        # uncached AND no w3 is wired (e.g. UI-only mode), skip the vault.
        token0_meta, token1_meta = await asyncio.gather(
            self._resolve_token(token0_address),
            self._resolve_token(token1_address),
        )
        if token0_meta is None or token1_meta is None:
            return None
        token0_symbol = token0_meta["symbol"]
        token1_symbol = token1_meta["symbol"]

        # Filter: token0 must have dYdX perp AND it must be active
        dydx_perp = dydx_perp_for(token0_symbol)
        if dydx_perp is None or dydx_perp not in active_dydx_tickers:
            return None

        is_usd = is_stable(token1_address)

        # In cross-pair (token1 not stable), check whether token1 has a dYdX perp too.
        token1_perp = None
        if not is_usd:
            candidate = dydx_perp_for(token1_symbol)
            if candidate is not None and candidate in active_dydx_tickers:
                token1_perp = candidate

        # Resolve TVL. Each Beefy CLM has TWO entries in /tvl:
        #   <id>        = strategy contract holdings (always tiny — just dust)
        #   <id>-rp     = the Reward Pool / CLM Pool, where users actually deposit
        # Beefy's own UI shows the -rp value (this is the real user-facing TVL).
        # We prefer -rp; fall back to base if -rp doesn't exist (rare).
        chain_tvls = tvl_data.get(CHAIN_ID_ARB) or {}
        clm_id = clm.get("id") or ""
        tvl_usd = chain_tvls.get(f"{clm_id}-rp")
        if tvl_usd is None:
            tvl_usd = chain_tvls.get(clm_id)

        # Resolve APY. Beefy renamed fields: legacy vaultApr/vaultAprDaily30d
        # were replaced by totalApy (compounded annual yield) and clmApr (raw
        # underlying APR). Beefy's own UI displays totalApy under the "Current
        # APY" column, so we match that. clmApr is the un-compounded fallback
        # for vaults that only have it; legacy fields kept for back-compat.
        # Check both base and -rp entries (the -rp variant has its own block).
        apy_block = apy_data.get(f"{clm_id}-rp") or apy_data.get(clm_id) or {}
        apy_30d = (
            apy_block.get("totalApy")
            or apy_block.get("clmApr")
            or apy_block.get("vaultAprDaily30d")
            or apy_block.get("vaultApr")
        )

        # Beefy CLM v2 splits state between two contracts:
        #   - earnContractAddress (= vault_id) is the user-facing ERC20 vault
        #     that holds totalSupply/balanceOf for share accounting.
        #   - "strategy" is the contract holding the V3 NFT, position ranges
        #     (positionMain/Alt), and current token balances.
        # Both addresses are needed by BeefyClmReader to read full CLM state.
        # Older cache rows may have NULL strategy_address; lifecycle factory
        # falls back to vault_id in that case.
        strategy_address = clm.get("strategy") or ""

        return {
            "vault_id": vault_id,
            "chain": TARGET_CHAIN,
            "pool_address": clm.get("tokenAddress") or "",
            "token0_address": token0_address,
            "token0_symbol": token0_symbol,
            "token0_decimals": token0_meta["decimals"],
            "token1_address": token1_address,
            "token1_symbol": token1_symbol,
            "token1_decimals": token1_meta["decimals"],
            "pool_fee": _fee_tier_to_pool_fee(clm.get("feeTier")),
            "manager": clm.get("tokenProviderId") or clm.get("platformId"),
            "tick_lower": None,  # not in /cow-vaults; would need on-chain pool read
            "tick_upper": None,
            "tvl_usd": tvl_usd,
            "apy_30d": apy_30d,
            "is_usd_pair": is_usd,
            "dydx_perp": dydx_perp,
            "dydx_perp_token1": token1_perp,
            "token0_logo_url": f"{BEEFY_API_BASE}/token/{TARGET_CHAIN}/{token0_symbol}",
            "token1_logo_url": f"{BEEFY_API_BASE}/token/{TARGET_CHAIN}/{token1_symbol}",
            "fetched_at": now,
            "strategy_address": strategy_address,
        }
