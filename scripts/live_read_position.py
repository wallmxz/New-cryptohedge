"""Live test: drive BeefyClmReader against the real WETH/ARB CLM and dump
the BeefyPosition it returns."""
import asyncio
import os
import sys
from pathlib import Path

# Make project root importable when run as a script
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.stdout.reconfigure(encoding="utf-8")

from web3 import AsyncWeb3, AsyncHTTPProvider
from chains.beefy import BeefyClmReader


STRATEGY = "0xC443a62c6E6410886aBd62ad7F4354B0429Ee6b5"
EARN     = "0xBfaDfcbeF611D958c8998ea29FAce6bb155302fd"
WALLET   = "0x7cb0e1c2C9699E7023Ce13205A0C3E0E4320873c"  # bot wallet from .env

# Pool token0 = WETH (18), token1 = ARB (18)
DEC0 = 18
DEC1 = 18


async def main():
    rpc = os.environ.get("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc")
    w3 = AsyncWeb3(AsyncHTTPProvider(rpc))
    reader = BeefyClmReader(
        w3=w3, strategy_address=STRATEGY, earn_address=EARN,
        wallet_address=WALLET, decimals0=DEC0, decimals1=DEC1,
    )
    pos = await reader.read_position()
    print("Live BeefyPosition:")
    print(f"  tick_lower:  {pos.tick_lower}")
    print(f"  tick_upper:  {pos.tick_upper}")
    print(f"  amount0 (WETH): {pos.amount0:,.6f}")
    print(f"  amount1 (ARB):  {pos.amount1:,.6f}")
    print(f"  share:       {pos.share:.6f}")
    print(f"  raw_balance: {pos.raw_balance}")

    # Sanity checks
    assert -887272 <= pos.tick_lower <= 887272, "tick_lower out of V3 range"
    assert -887272 <= pos.tick_upper <= 887272, "tick_upper out of V3 range"
    assert pos.tick_lower < pos.tick_upper, "tick_lower must be < tick_upper"
    assert pos.amount0 >= 0 and pos.amount1 >= 0, "amounts must be non-negative"
    print("\nAll sanity checks passed.")


if __name__ == "__main__":
    asyncio.run(main())
