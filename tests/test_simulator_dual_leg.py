"""Simulator dual-leg: dual-feed loop, dynamic Beefy, multi-symbol exchange."""
import pytest
from backtest.simulator import Simulator, SimConfig


@pytest.mark.asyncio
async def test_dual_leg_simulator_runs_to_completion():
    """Smoke test: dual-leg sim runs over fake price data without crashing."""
    eth_prices = [(1700000000 + i * 300, 4000.0 + i * 5) for i in range(20)]
    arb_prices = [(1700000000 + i * 300, 1.50 + i * 0.01) for i in range(20)]
    funding = []
    apr_history = [(1700000000, 0.30)]

    config = SimConfig(
        vault_address="0xV", pool_address="0xP",
        start_ts=1700000000, end_ts=1700006000,
        capital_lp=300.0, capital_dydx=130.0,
        hedge_ratio=1.0, threshold_aggressive=0.01,
        max_open_orders=200,
        dydx_symbol_token0="ARB-USD",
        dydx_symbol_token1="ETH-USD",
    )
    static_range = {
        "p_a": 0.0003, "p_b": 0.0005,
        "L": 10000.0, "share": 1.0,
        "tick_lower": -201386, "tick_upper": -198363,
    }

    sim = Simulator(
        config=config,
        token0_prices=arb_prices,
        token1_prices=eth_prices,
        funding_token0=funding, funding_token1=funding,
        apr_history=apr_history,
        range_events=[], static_range=static_range,
    )
    result = await sim.run()
    assert "net_pnl" in result
    assert "exchange_stats" in result
