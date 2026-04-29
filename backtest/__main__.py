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
    return p.parse_args(argv)


async def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv if argv is not None else sys.argv[1:])

    start_ts = datetime.fromisoformat(args.start_iso).timestamp()
    end_ts = datetime.fromisoformat(args.end_iso).timestamp()

    cache = Cache(args.cache_path)
    await cache.initialize()
    fetcher = DataFetcher(cache=cache)

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

    await cache.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
