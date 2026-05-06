"""List Arbitrum cross-pair Beefy CLMs that are dual-leg-hedgeable on dYdX.

Cross-pair criterion: token1 is NOT a stablecoin AND both token0 and token1
have ACTIVE perp markets on dYdX (so we can hedge each leg independently).

Sources:
  - Beefy /cows         → CLM list with token info + lpAddress
  - Beefy /tvl          → TVL per chain per vault id
  - Beefy /apy/breakdown → APR per vault id
  - dYdX /v4/perpetualMarkets → active perp markets
  - Pool contract .fee() on Arbitrum → fee tier (500=0.05%, 3000=0.30%, 10000=1%)

Usage:
  python scripts/list_cross_pairs.py [min_tvl_usd]

Sorts by 30-day vault APR descending. Defaults min TVL to $50K to filter noise.
"""
from __future__ import annotations
import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

# Ensure project root + tests/ in path so we can reuse stables.py and the dydx stub
PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
sys.path.insert(0, str(PROJECT_ROOT / "tests"))
import conftest  # noqa: F401  injects dydx-v4-client stub for any imports

from stables import is_stable, DYDX_TOKEN_TO_PERP


BEEFY_API = "https://api.beefy.finance"
DYDX_API = "https://indexer.dydx.trade/v4"
ARBITRUM_RPC = os.environ.get("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc")
TARGET_CHAIN = "arbitrum"        # Beefy /cow-vaults: clm["chain"] is the lowercase name
TARGET_CHAIN_ID = "42161"        # Beefy /tvl: top-level keyed by chainId-as-string

# Minimal pool ABI: just `fee()` returning uint24.
POOL_FEE_ABI = [
    {
        "inputs": [],
        "name": "fee",
        "outputs": [{"internalType": "uint24", "name": "", "type": "uint24"}],
        "stateMutability": "view",
        "type": "function",
    }
]


async def fetch_beefy(client: httpx.AsyncClient) -> tuple[list, dict, dict]:
    """Returns (clm_list, tvl_dict, apy_dict).

    Beefy renamed /cows to /cow-vaults at some point — the older endpoint now
    404s. The shape is the same: a list of CLM objects with id, chain, tokens,
    earnContractAddress, lpAddress, etc.
    """
    cows_resp = await client.get(f"{BEEFY_API}/cow-vaults")
    cows_resp.raise_for_status()
    tvl_resp = await client.get(f"{BEEFY_API}/tvl")
    tvl_resp.raise_for_status()
    apy_resp = await client.get(f"{BEEFY_API}/apy/breakdown")
    apy_resp.raise_for_status()
    return cows_resp.json(), tvl_resp.json(), apy_resp.json()


async def fetch_dydx_active(client: httpx.AsyncClient) -> set[str]:
    resp = await client.get(f"{DYDX_API}/perpetualMarkets")
    resp.raise_for_status()
    payload = resp.json()
    markets = payload.get("markets", {})
    return {ticker for ticker, m in markets.items() if m.get("status") == "ACTIVE"}


async def fetch_pool_fees(pool_addresses: list[str]) -> dict[str, int | None]:
    """Query the .fee() function of each Uniswap V3 pool on Arbitrum."""
    from web3 import AsyncWeb3, AsyncHTTPProvider

    w3 = AsyncWeb3(AsyncHTTPProvider(ARBITRUM_RPC))
    results: dict[str, int | None] = {}

    async def _fetch_one(addr: str):
        try:
            cs_addr = w3.to_checksum_address(addr)
            contract = w3.eth.contract(address=cs_addr, abi=POOL_FEE_ABI)
            fee = await contract.functions.fee().call()
            return addr, fee
        except Exception as e:
            return addr, None

    coros = [_fetch_one(a) for a in pool_addresses]
    for coro in asyncio.as_completed(coros):
        addr, fee = await coro
        results[addr] = fee
    return results


def _is_cross_pair_candidate(
    clm: dict, active_tickers: set[str],
) -> dict | None:
    """Returns the pair info if it qualifies as a dual-leg-hedgeable cross-pair.

    Beefy /cow-vaults shape (as of 2026-05): symbols in `assets`, addresses in
    parallel array `depositTokenAddresses`. Fee tier is a string like "0.05"
    representing percent (so 0.05% = 5 bps).
    """
    if clm.get("chain") != TARGET_CHAIN:
        return None
    if clm.get("status") != "active":
        return None
    assets = clm.get("assets") or []
    addrs = clm.get("depositTokenAddresses") or []
    if len(assets) < 2 or len(addrs) < 2:
        return None

    sym0 = (assets[0] or "").upper()
    sym1 = (assets[1] or "").upper()
    addr0 = addrs[0] or ""
    addr1 = addrs[1] or ""

    # Cross-pair: neither leg is a stable
    if is_stable(addr0) or is_stable(addr1):
        return None

    perp0 = DYDX_TOKEN_TO_PERP.get(sym0)
    perp1 = DYDX_TOKEN_TO_PERP.get(sym1)
    if perp0 is None or perp0 not in active_tickers:
        return None
    if perp1 is None or perp1 not in active_tickers:
        return None

    fee_pct: float = 0.0
    fee_raw = clm.get("feeTier")
    if fee_raw is not None:
        try:
            fee_pct = float(fee_raw)
        except (ValueError, TypeError):
            pass

    return {
        "vault_id": clm.get("earnContractAddress") or "",
        "id": clm.get("id"),
        "lp_token_address": clm.get("tokenAddress") or "",
        "token0_symbol": sym0,
        "token1_symbol": sym1,
        "token0_address": addr0,
        "token1_address": addr1,
        "perp0": perp0,
        "perp1": perp1,
        "manager": clm.get("strategyTypeId") or clm.get("tokenProviderId") or "—",
        "fee_pct": fee_pct,
    }


def _bps_to_pct(fee_bps: int | None) -> str:
    if fee_bps is None:
        return "?"
    return f"{fee_bps / 10000:.2f}%"


async def main():
    min_tvl = float(sys.argv[1]) if len(sys.argv) > 1 else 50_000.0

    async with httpx.AsyncClient(timeout=60.0) as client:
        print("Fetching Beefy /cows + /tvl + /apy/breakdown ...", flush=True)
        cows, tvl_data, apy_data = await fetch_beefy(client)
        print(f"  -> {len(cows)} CLMs total across all chains", flush=True)

        print("Fetching dYdX active markets ...", flush=True)
        active = await fetch_dydx_active(client)
        print(f"  -> {len(active)} active perp markets", flush=True)

    # Filter cross-pair candidates
    candidates = []
    for clm in cows:
        info = _is_cross_pair_candidate(clm, active)
        if info is None:
            continue
        # Attach TVL + APR
        chain_tvl = tvl_data.get(TARGET_CHAIN_ID) or tvl_data.get(TARGET_CHAIN) or {}
        info["tvl_usd"] = chain_tvl.get(info["id"])
        apy_block = apy_data.get(info["id"]) or {}
        info["apy_30d"] = apy_block.get("vaultAprDaily30d") or apy_block.get("vaultApr")
        candidates.append(info)

    print(f"\nCross-pair candidates after filtering: {len(candidates)}", flush=True)

    # Apply TVL floor
    candidates = [c for c in candidates if (c.get("tvl_usd") or 0) >= min_tvl]
    print(f"After TVL >= ${min_tvl:,.0f}: {len(candidates)}", flush=True)

    # Sort by APR descending (NULLs last)
    candidates.sort(
        key=lambda c: (c.get("apy_30d") is None, -(c.get("apy_30d") or 0))
    )

    print()
    print("=" * 130)
    print(
        f"{'PAIR':<14} {'FEE':>6} {'TVL':>12} {'APR_30d':>9} "
        f"{'PERPS':<22} {'MANAGER':<14} {'VAULT':<44}"
    )
    print("-" * 130)
    for c in candidates:
        pair = f"{c['token0_symbol']}/{c['token1_symbol']}"
        fee = f"{c['fee_pct']:.2f}%" if c.get("fee_pct") else "?"
        tvl = f"${c['tvl_usd']:,.0f}" if c.get("tvl_usd") else "?"
        apr = f"{c['apy_30d'] * 100:.1f}%" if c.get("apy_30d") is not None else "?"
        perps = f"{c['perp0']}+{c['perp1']}"
        mgr = (c.get("manager") or "")[:12]
        vault = c["vault_id"][:42]
        print(f"{pair:<14} {fee:>6} {tvl:>12} {apr:>9} {perps:<22} {mgr:<14} {vault:<44}")

    print(f"\n{len(candidates)} cross-pair CLMs listed above with TVL >= ${min_tvl:,.0f}.")
    print("Suggested filter: focus on pairs where APR > 30%, TVL > $100K, and fee_tier 0.30%+.")


if __name__ == "__main__":
    asyncio.run(main())
