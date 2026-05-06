"""Probe the EARN contract specifically — focused on ERC20-like methods."""
from __future__ import annotations
import asyncio
import os
import sys
from web3 import AsyncWeb3, AsyncHTTPProvider
from eth_utils import function_signature_to_4byte_selector
from eth_abi import encode as abi_encode

RPC = os.environ.get("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc")


def selector(sig: str) -> bytes:
    return function_signature_to_4byte_selector(sig)


async def call(w3, address, sig, args=None, types=None):
    sel = selector(sig)
    data = sel
    if args:
        data = sel + abi_encode(types, args)
    try:
        result = await w3.eth.call({"to": address, "data": data})
        return result.hex()
    except Exception as e:
        return f"REVERT: {type(e).__name__}: {e}"[:200]


async def main():
    earn = w3_addr = sys.argv[1] if len(sys.argv) > 1 else "0xBfaDfcbeF611D958c8998ea29FAce6bb155302fd"
    holder = sys.argv[2] if len(sys.argv) > 2 else "0x0000000000000000000000000000000000000000"
    w3 = AsyncWeb3(AsyncHTTPProvider(RPC))
    earn_cs = w3.to_checksum_address(earn)
    holder_cs = w3.to_checksum_address(holder)

    print(f"Earn contract: {earn_cs}")
    print(f"Holder address (for balanceOf probe): {holder_cs}\n")

    sigs = [
        ("name()", None, None),
        ("symbol()", None, None),
        ("decimals()", None, None),
        ("totalSupply()", None, None),
        ("balanceOf(address)", [holder_cs], ["address"]),
        ("balances()", None, None),
        ("strategy()", None, None),
        ("want()", None, None),
        ("getPricePerFullShare()", None, None),
        ("pricePerFullShare()", None, None),
        ("token0()", None, None),
        ("token1()", None, None),
        ("paused()", None, None),
        ("upgradeStrat()", None, None),
        ("balance()", None, None),
        ("available()", None, None),
        ("rewardPool()", None, None),
        ("output()", None, None),
        ("nativeToken()", None, None),
        ("native()", None, None),
        # Beefy CLM Earn-specific
        ("deposit(uint256,uint256,uint256)", None, None),
        ("withdraw(uint256,uint256,uint256)", None, None),
        ("previewDeposit(uint256,uint256)", None, None),
        ("previewWithdraw(uint256)", None, None),
    ]

    for sig, args, types in sigs:
        h = await call(w3, earn_cs, sig, args, types)
        if h.startswith("REVERT"):
            continue
        if h.startswith("0x"):
            h = h[2:]
        # Pretty-print best-effort
        if not h:
            print(f"{sig} -> empty"); continue
        if len(h) == 64:
            v = int(h, 16)
            # Try to interpret as ascii
            print(f"{sig} -> uint={v}  hex=0x{h}")
        elif len(h) == 128:
            a = int(h[:64], 16)
            b = int(h[64:], 16)
            print(f"{sig} -> ({a}, {b})")
        else:
            # decode dynamic strings
            print(f"{sig} -> len={len(h)//2}: 0x{h[:200]}{'...' if len(h)>200 else ''}")


if __name__ == "__main__":
    asyncio.run(main())
