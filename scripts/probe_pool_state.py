"""Verify positionMain ticks against Uniswap V3 pool state."""
import asyncio
import os
from web3 import AsyncWeb3, AsyncHTTPProvider
from eth_utils import function_signature_to_4byte_selector


async def call_signed(w3, address, sig, ret_words: int = 1):
    """Generic raw call returning raw hex bytes."""
    sel = function_signature_to_4byte_selector(sig)
    result = await w3.eth.call({"to": address, "data": sel})
    return result


def decode_int24(word32: bytes) -> int:
    """Decode lower 24-bit signed int24 from a 32-byte ABI word."""
    val = int.from_bytes(word32, "big") & 0xFFFFFF
    if val & (1 << 23):
        val -= 1 << 24
    return val


async def main():
    rpc = os.environ.get("ARBITRUM_RPC_URL", "https://arb1.arbitrum.io/rpc")
    w3 = AsyncWeb3(AsyncHTTPProvider(rpc))

    strategy = w3.to_checksum_address("0xC443a62c6E6410886aBd62ad7F4354B0429Ee6b5")
    pool = w3.to_checksum_address("0xC6F780497A95e246EB9449f5e4770916DCd6396A")  # ARB-WETH 0.05% pool

    # positionMain()
    raw = await call_signed(w3, strategy, "positionMain()")
    print(f"positionMain() raw len={len(raw)}")
    pm_lower = decode_int24(raw[0:32])
    pm_upper = decode_int24(raw[32:64])
    print(f"  main range: [{pm_lower}, {pm_upper}]")

    raw = await call_signed(w3, strategy, "positionAlt()")
    pa_lower = decode_int24(raw[0:32])
    pa_upper = decode_int24(raw[32:64])
    print(f"  alt range:  [{pa_lower}, {pa_upper}]")

    # Pool slot0 - returns (sqrtPriceX96, tick, ...). Tick is at offset 1 word.
    raw = await call_signed(w3, pool, "slot0()")
    print(f"\nUniswap pool slot0() raw len={len(raw)}")
    # struct: (uint160 sqrtPriceX96, int24 tick, uint16 obs index, uint16 obs cardinality, uint16 obs cardinalityNext, uint8 feeProtocol, bool unlocked)
    sqrt_price_x96 = int.from_bytes(raw[0:32], "big")
    current_tick = decode_int24(raw[32:64])
    print(f"  sqrtPriceX96: {sqrt_price_x96}")
    print(f"  current tick: {current_tick}")

    # Are positionMain ticks straddling the current tick?
    in_range = pm_lower <= current_tick <= pm_upper
    print(f"\ncurrent tick {current_tick} in main range [{pm_lower}, {pm_upper}]: {in_range}")

    # tick to price (Uniswap V3 formula)
    # price = 1.0001 ** tick
    price_token0_in_token1 = 1.0001 ** current_tick
    print(f"  price (token1 per token0, raw decimals): {price_token0_in_token1:e}")

    # ARB-WETH pool: token0 = WETH (18), token1 = ARB (18). Same decimals so display price = raw price
    # 1 WETH = X ARB. ETH ~ $4000, ARB ~ $0.40 → 1 WETH ~ 10000 ARB
    # 1.0001 ** 73000 ≈ 1500 (rough)
    # Actual current_tick should be around... we'll see.


if __name__ == "__main__":
    asyncio.run(main())
