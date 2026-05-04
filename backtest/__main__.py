"""CLI entry point: python -m backtest --vault X --pool Y --from ... --to ..."""
from __future__ import annotations
import argparse
import asyncio
import sys
from datetime import datetime

from backtest.cache import Cache
from backtest.data import DataFetcher
from backtest.simulator import Simulator, SimConfig
from backtest.report import format_text_report, format_json_report


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="backtest")
    p.add_argument("--vault", required=True, help="Beefy vault/strategy address")
    p.add_argument("--pool", required=True, help="Uniswap V3 pool address")
    p.add_argument("--from", dest="start_iso", required=True, help="ISO start date YYYY-MM-DD")
    p.add_argument("--to", dest="end_iso", required=True, help="ISO end date YYYY-MM-DD")
    p.add_argument("--capital", type=float, default=300.0, help="LP capital USD")
    p.add_argument("--margin", type=float, default=130.0, help="dYdX margin USD")
    p.add_argument("--hedge-ratio", type=float, default=1.0)
    p.add_argument("--threshold-aggressive", type=float, default=0.01)
    p.add_argument("--max-open-orders", type=int, default=200)
    p.add_argument("--symbol", default="ETH-USD")
    p.add_argument("--token0-amount", type=float, default=0.5,
                   help="Static fallback: token0 amount in pool (used when range events missing)")
    p.add_argument("--token1-amount", type=float, default=1500.0,
                   help="Static fallback: token1 amount in pool")
    p.add_argument("--share", type=float, default=0.01,
                   help="Static fallback: user share of vault")
    p.add_argument("--tick-lower", type=int, default=-197310)
    p.add_argument("--tick-upper", type=int, default=-195303)
    p.add_argument("--cache-path", default="backtest_cache.db")
    p.add_argument("--output", default=None, help="JSON output path (optional)")
    p.add_argument("--symbol-token0", default=None,
                   help="Cross-pair only: dYdX perp for token0 (e.g. ARB-USD).")
    p.add_argument("--symbol-token1", default=None,
                   help="Cross-pair only: dYdX perp for token1 (e.g. ETH-USD).")
    p.add_argument("--p-a", type=float, default=None,
                   help="Dual-leg: lower bound of LP range in p (token1/token0).")
    p.add_argument("--p-b", type=float, default=None,
                   help="Dual-leg: upper bound.")
    p.add_argument("--liquidity-l", type=float, default=None,
                   help="Dual-leg: V3 liquidity L for the static_range.")
    return p.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    start_ts = datetime.fromisoformat(args.start_iso).timestamp()
    end_ts = datetime.fromisoformat(args.end_iso).timestamp()

    cache = Cache(args.cache_path)
    await cache.initialize()

    try:
        fetcher = DataFetcher(cache=cache)

        is_dual_leg = bool(args.symbol_token0 and args.symbol_token1)

        if is_dual_leg:
            # Validate required dual-leg args
            if args.p_a is None or args.p_b is None or args.liquidity_l is None:
                print("ERROR: --p-a, --p-b, --liquidity-l are required for cross-pair mode", file=sys.stderr)
                return 2

            print(f"Fetching token0 prices ({args.symbol_token0})...", flush=True)
            token0_prices = await fetcher.fetch_token_prices(
                symbol=args.symbol_token0, start=start_ts, end=end_ts,
            )
            print(f"  -> {len(token0_prices)} samples", flush=True)

            print(f"Fetching token1 prices ({args.symbol_token1})...", flush=True)
            token1_prices = await fetcher.fetch_token_prices(
                symbol=args.symbol_token1, start=start_ts, end=end_ts,
            )
            print(f"  -> {len(token1_prices)} samples", flush=True)

            print("Fetching funding for both legs...", flush=True)
            funding_t0 = await fetcher.fetch_dydx_funding(
                symbol=args.symbol_token0, start=start_ts, end=end_ts,
            )
            funding_t1 = await fetcher.fetch_dydx_funding(
                symbol=args.symbol_token1, start=start_ts, end=end_ts,
            )
            print(f"  -> token0={len(funding_t0)}, token1={len(funding_t1)} samples", flush=True)

            print("Fetching Beefy APR history...", flush=True)
            apr_history = await fetcher.fetch_beefy_apr_history(
                vault=args.vault, start=start_ts, end=end_ts,
            )
            print(f"  -> {len(apr_history)} samples", flush=True)

            config = SimConfig(
                vault_address=args.vault, pool_address=args.pool,
                start_ts=start_ts, end_ts=end_ts,
                capital_lp=args.capital, capital_dydx=args.margin,
                hedge_ratio=args.hedge_ratio,
                threshold_aggressive=args.threshold_aggressive,
                max_open_orders=args.max_open_orders,
                dydx_symbol_token0=args.symbol_token0,
                dydx_symbol_token1=args.symbol_token1,
            )
            static_range = {
                "p_a": args.p_a, "p_b": args.p_b, "L": args.liquidity_l,
                "share": args.share,
                "tick_lower": args.tick_lower, "tick_upper": args.tick_upper,
            }

            print("Running simulator...", flush=True)
            sim = Simulator(
                config=config,
                token0_prices=token0_prices,
                token1_prices=token1_prices,
                funding_token0=funding_t0,
                funding_token1=funding_t1,
                apr_history=apr_history,
                range_events=[], static_range=static_range,
            )
            result = await sim.run()

            print()
            print(format_text_report(
                result,
                capital_lp=args.capital, capital_dydx=args.margin,
                symbol=f"{args.symbol_token0}/{args.symbol_token1}",
                start_iso=args.start_iso, end_iso=args.end_iso,
            ))

            if args.output:
                with open(args.output, "w") as f:
                    f.write(format_json_report(
                        result, capital_lp=args.capital, capital_dydx=args.margin,
                    ))
                print(f"\nJSON written to {args.output}")

            return 0
        else:
            print("Fetching ETH prices...", flush=True)
            eth_prices = await fetcher.fetch_eth_prices(start=start_ts, end=end_ts)
            print(f"  -> {len(eth_prices)} samples", flush=True)

            print("Fetching dYdX funding...", flush=True)
            funding = await fetcher.fetch_dydx_funding(symbol=args.symbol, start=start_ts, end=end_ts)
            print(f"  -> {len(funding)} samples", flush=True)

            print("Fetching Beefy APR history...", flush=True)
            apr_history = await fetcher.fetch_beefy_apr_history(
                vault=args.vault, start=start_ts, end=end_ts,
            )
            print(f"  -> {len(apr_history)} samples", flush=True)

            config = SimConfig(
                vault_address=args.vault,
                pool_address=args.pool,
                start_ts=start_ts,
                end_ts=end_ts,
                capital_lp=args.capital,
                capital_dydx=args.margin,
                hedge_ratio=args.hedge_ratio,
                threshold_aggressive=args.threshold_aggressive,
                max_open_orders=args.max_open_orders,
            )

            static_range = {
                "tick_lower": args.tick_lower, "tick_upper": args.tick_upper,
                "amount0": args.token0_amount, "amount1": args.token1_amount,
                "share": args.share, "raw_balance": int(args.share * 10**18),
            }

            print("Running simulator...", flush=True)
            sim = Simulator(
                config=config,
                eth_prices=eth_prices,
                funding=funding,
                apr_history=apr_history,
                range_events=[],
                static_range=static_range,
            )
            result = await sim.run()

            print()
            print(format_text_report(
                result,
                capital_lp=args.capital, capital_dydx=args.margin,
                symbol=args.symbol, start_iso=args.start_iso, end_iso=args.end_iso,
            ))

            if args.output:
                with open(args.output, "w") as f:
                    f.write(format_json_report(
                        result, capital_lp=args.capital, capital_dydx=args.margin,
                    ))
                print(f"\nJSON written to {args.output}")

            return 0
    finally:
        await cache.close()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
