"""Probe deposit/withdraw selectors on strategy vs earn contracts.

Beefy CLM users typically deposit to the EARN contract (not the strategy).
"""
import asyncio
import os
from web3 import AsyncWeb3, AsyncHTTPProvider
from eth_utils import function_signature_to_4byte_selector


RPC = os.environ.get("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc")


async def probe(w3, address, sig):
    sel = function_signature_to_4byte_selector(sig)
    # Use call() with empty / minimum args; we just want to confirm selector exists.
    # If the function exists but needs params, call() will revert with a
    # descriptive error or success. If selector doesn't exist, EVM returns
    # empty data (no revert reason), or the contract's fallback runs.
    try:
        result = await w3.eth.call({"to": address, "data": "0x" + sel.hex()})
        return f"EXISTS (returned {len(result)} bytes)"
    except Exception as e:
        s = str(e)
        if "revert" in s.lower():
            return f"EXISTS but reverted: {s[:120]}"
        return f"err: {s[:120]}"


async def main():
    w3 = AsyncWeb3(AsyncHTTPProvider(RPC))
    strategy = w3.to_checksum_address("0xC443a62c6E6410886aBd62ad7F4354B0429Ee6b5")
    earn = w3.to_checksum_address("0xBfaDfcbeF611D958c8998ea29FAce6bb155302fd")

    sigs = [
        "deposit(uint256,uint256,uint256)",
        "deposit()",
        "deposit(uint256)",
        "withdraw(uint256,uint256,uint256)",
        "withdraw(uint256)",
        "withdrawAll()",
        "previewDeposit(uint256,uint256)",
        "harvest()",
        "earn()",
        "moveTicks()",
        "rebalance()",
    ]

    for label, addr in [("STRATEGY", strategy), ("EARN", earn)]:
        print(f"\n=== {label} {addr} ===")
        for sig in sigs:
            status = await probe(w3, addr, sig)
            print(f"  {sig:50} -> {status}")


if __name__ == "__main__":
    asyncio.run(main())
