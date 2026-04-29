"""Format backtest results to text and JSON."""
from __future__ import annotations
import json


def annualized_apr(*, net: float, capital: float, duration_seconds: float) -> float:
    if capital <= 0 or duration_seconds <= 0:
        return 0.0
    year_seconds = 365.0 * 86400
    return (net / capital) * (year_seconds / duration_seconds)


def format_text_report(
    result: dict, *,
    capital_lp: float, capital_dydx: float,
    symbol: str, start_iso: str, end_iso: str,
) -> str:
    duration = result["duration_seconds"]
    days = duration / 86400
    apr_lp = annualized_apr(
        net=result["net_pnl"], capital=capital_lp, duration_seconds=duration,
    )
    apr_total = annualized_apr(
        net=result["net_pnl"], capital=capital_lp + capital_dydx,
        duration_seconds=duration,
    )

    out_of_range_hours = result["out_of_range_seconds"] / 3600

    # Display the period return as well as the annualized APR so both
    # the raw 6-month figure and the annualized projection are visible.
    period_return_lp = (
        result["net_pnl"] / capital_lp if capital_lp > 0 else 0.0
    )

    lines = [
        f"Backtest {symbol} | {start_iso} -> {end_iso} ({days:.1f} days)",
        f"Capital: ${capital_lp:.0f} LP + ${capital_dydx:.0f} dYdX margin",
        "",
        f"Fills:          {result['fills_maker']} maker, {result['fills_taker']} taker",
        f"Range resets:   {result['range_resets']} (Beefy)",
        f"Out-of-range:   {out_of_range_hours:.1f} hours total",
        "",
        f"LP fees earned: ${result['lp_fees_earned']:.2f}",
        f"Net PnL:        ${result['net_pnl']:.2f} ({period_return_lp:.1%} on LP, {apr_lp:.1%} APR on LP, {apr_total:.1%} APR on total)",
        f"Max drawdown:   ${result['max_drawdown']:.2f}",
        "",
        "Note: best-case simulation; real-world may be 5-15% worse due to latency/slippage.",
    ]
    return "\n".join(lines)


def format_json_report(result: dict, *, capital_lp: float, capital_dydx: float) -> str:
    duration = result["duration_seconds"]
    enriched = dict(result)
    enriched["apr_lp"] = annualized_apr(
        net=result["net_pnl"], capital=capital_lp, duration_seconds=duration,
    )
    enriched["apr_total"] = annualized_apr(
        net=result["net_pnl"], capital=capital_lp + capital_dydx,
        duration_seconds=duration,
    )
    enriched["capital_lp"] = capital_lp
    enriched["capital_dydx"] = capital_dydx
    return json.dumps(enriched, indent=2)
