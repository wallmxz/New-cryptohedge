"""Confirm token order on the Uniswap V3 pool. Beefy API may report
depositTokenAddresses in 'pretty' order (asset of interest first), not
the actual on-chain token0/token1 order. We need to know the real order
to interpret balances() correctly."""
import asyncio
import os
from web3 import AsyncWeb3, AsyncHTTPProvider
from eth_utils import function_signature_to_4byte_selector


async def call_addr(w3, address, sig):
    sel = function_signature_to_4byte_selector(sig)
    raw = await w3.eth.call({"to": address, "data": "0x" + sel.hex()})
    return "0x" + raw[12:].hex()


async def main():
    rpc = os.environ.get("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc")
    w3 = AsyncWeb3(AsyncHTTPProvider(rpc))
    pool = w3.to_checksum_address("0xC6F780497A95e246EB9449f5e4770916DCd6396A")
    strategy = w3.to_checksum_address("0xC443a62c6E6410886aBd62ad7F4354B0429Ee6b5")

    pool_t0 = await call_addr(w3, pool, "token0()")
    pool_t1 = await call_addr(w3, pool, "token1()")
    print(f"Pool {pool}:")
    print(f"  token0() = {pool_t0}")
    print(f"  token1() = {pool_t1}")

    strat_lp0 = await call_addr(w3, strategy, "lpToken0()")
    strat_lp1 = await call_addr(w3, strategy, "lpToken1()")
    print(f"\nStrategy {strategy}:")
    print(f"  lpToken0() = {strat_lp0}")
    print(f"  lpToken1() = {strat_lp1}")

    print("\nKnown:")
    print("  WETH = 0x82af49447d8a07e3bd95bd0d56f35241523fbab1")
    print("  ARB  = 0x912ce59144191c1204e64559fe8253a0e49e6548")


if __name__ == "__main__":
    asyncio.run(main())
