"""Probe a Beefy CLM strategy contract on Arbitrum to discover function shapes.

We try a wide list of candidate selectors (the names that the various
Beefy CLM strategy generations have used) and report which ones return
data that decodes plausibly as the position info we need.
"""
from __future__ import annotations
import asyncio
import os
import sys
from web3 import AsyncWeb3, AsyncHTTPProvider
from eth_utils import function_signature_to_4byte_selector


RPC = os.environ.get("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc")

CANDIDATES = [
    # (signature, return-type-hint)
    ("range()", "(int24,int24)"),
    ("positionMain()", "(int24,int24)"),
    ("positionAlt()", "(int24,int24)"),
    ("currentRange()", "(int24,int24)"),
    ("ticks()", "(int24,int24)"),
    ("getRange()", "(int24,int24)"),
    ("balances()", "(uint256,uint256)"),
    ("balanceOfPool()", "(uint256,uint256)"),
    ("balancesOfPool()", "(uint256,uint256)"),
    ("balancesOfThis()", "(uint256,uint256)"),
    ("balanceOfWant()", "uint256"),
    ("balanceOfThis()", "(uint256,uint256)"),
    ("totalSupply()", "uint256"),
    ("totalSupply0()", "uint256"),
    ("vault()", "address"),
    ("want()", "address"),
    ("pool()", "address"),
    ("native()", "address"),
    ("lpToken0()", "address"),
    ("lpToken1()", "address"),
    ("output()", "address"),
    ("strategy()", "address"),
    ("manager()", "address"),
    ("owner()", "address"),
    ("paused()", "bool"),
    ("twapInterval()", "uint32"),
    ("price()", "uint256"),
    # Secondary range/position helpers
    ("positionMain()()", "_"),
    ("positionAlt()()", "_"),
    ("getMainPosition()", "_"),
    ("getAlternatePosition()", "_"),
    ("positions()", "_"),
    # Uniswap V3 strategy variants
    ("tickLower()", "int24"),
    ("tickUpper()", "int24"),
    ("currentTickLower()", "int24"),
    ("currentTickUpper()", "int24"),
    ("limitPositionLower()", "int24"),
    ("limitPositionUpper()", "int24"),
    # Beefy CLM v2 (from research notes - just hopeful guesses)
    ("rangeMain()", "(int24,int24)"),
    ("rangeAlt()", "(int24,int24)"),
    ("rangeWidth()", "(int24,int24)"),
    ("positionMainBalances()", "(uint256,uint256)"),
    ("positionAltBalances()", "(uint256,uint256)"),
    ("getPositionsBalances()", "_"),
    # standard view functions
    ("token0()", "address"),
    ("token1()", "address"),
    ("twap()", "uint256"),
    ("currentPrice()", "uint256"),
    ("getMainPositionState()", "_"),
    ("isPaused()", "bool"),
]


async def probe(w3, address, sig):
    selector = "0x" + function_signature_to_4byte_selector(sig).hex()
    try:
        result = await w3.eth.call({"to": address, "data": selector})
        return result.hex()
    except Exception as e:
        return f"REVERT: {type(e).__name__}: {e}"[:180]


def decode_int24(b: bytes) -> int:
    """Decode a left-padded int24 from a 32-byte word, sign-extended."""
    if len(b) != 32:
        raise ValueError("not 32 bytes")
    val = int.from_bytes(b, "big", signed=False)
    # int24 sign extend
    if val >> 23 & 1 and val == val & 0xFFFFFF:
        val -= 1 << 24
    return val


def explain_hex(h: str) -> str:
    if h.startswith("REVERT") or h.startswith("0xrevert") or h.startswith("0x") and len(h) <= 4:
        return h
    if h.startswith("0x"):
        h = h[2:]
    raw = bytes.fromhex(h) if h else b""
    if not raw:
        return "(empty)"
    if len(raw) == 32:
        v_uint = int.from_bytes(raw, "big")
        try:
            return f"single 32B word: uint={v_uint}, hex=0x{h}"
        except Exception:
            return f"single 32B word raw=0x{h}"
    if len(raw) == 64:
        a = int.from_bytes(raw[:32], "big")
        b = int.from_bytes(raw[32:], "big")
        # Try to interpret as (int24, int24) — small magnitudes (<2^23 abs)
        # Or two large uint256 (balances)
        return (
            f"two 32B words:\n"
            f"  word0 dec={a} (lower 24b={a & 0xFFFFFF})\n"
            f"  word1 dec={b} (lower 24b={b & 0xFFFFFF})"
        )
    return f"len={len(raw)}: 0x{h[:200]}{'...' if len(h)>200 else ''}"


async def main():
    address = sys.argv[1] if len(sys.argv) > 1 else "0xC443a62c6E6410886aBd62ad7F4354B0429Ee6b5"
    w3 = AsyncWeb3(AsyncHTTPProvider(RPC))
    code = await w3.eth.get_code(w3.to_checksum_address(address))
    print(f"Probing {address} on Arbitrum")
    print(f"Code size: {len(code)} bytes")
    if len(code) == 0:
        print("NO CODE AT ADDRESS — maybe a proxy/EOA?")
        return
    print("=" * 80)
    for sig, hint in CANDIDATES:
        h = await probe(w3, w3.to_checksum_address(address), sig)
        if h.startswith("REVERT"):
            continue  # skip noise
        print(f"\n{sig} -> hint:{hint}")
        print(f"  {explain_hex(h)}")


if __name__ == "__main__":
    asyncio.run(main())
