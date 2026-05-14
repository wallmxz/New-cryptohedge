from engine.pnl import compute_operation_pnl
from engine.operation import Operation, OperationState


def test_single_leg_funding_reads_funding_paid_token0_not_legacy():
    """Regression 2026-05-14: funding poller writes per-leg fields
    (`funding_paid_token0`), not the legacy `funding_paid`. The single-leg
    branch of `compute_operation_pnl` was reading the legacy field and
    showing $0.00 on the dashboard despite real funding accrued.
    """
    op = Operation(
        id=1, started_at=1000.0, state=OperationState.ACTIVE,
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0,
        baseline_collateral=130.0,
        funding_paid=0.0,             # legacy field (not written by poller)
        funding_paid_token0=-0.50,    # poller writes here (single-leg too)
        funding_paid_token1=0.0,
    )
    breakdown = compute_operation_pnl(
        op,
        current_pool_value_usd=300.0, current_eth_price=3000.0,
        hedge_realized_since_baseline=0.0, hedge_unrealized_since_baseline=0.0,
    )
    # funding_paid_token0 = -0.50 (paid convention) → display = +0.50 (received)
    assert abs(breakdown["funding"] - 0.50) < 1e-9
    assert abs(breakdown["funding_token0"] - 0.50) < 1e-9


def test_operation_pnl_breakdown():
    op = Operation(
        id=1, started_at=1000.0, state=OperationState.ACTIVE,
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0,
        baseline_collateral=130.0,
        perp_fees_paid=0.5,
        funding_paid_token0=-1.0,  # bot received +$1 funding (single-leg writes to token0 col)
        lp_fees_earned=2.1,
        bootstrap_slippage=0.07,
    )
    breakdown = compute_operation_pnl(
        op,
        current_pool_value_usd=298.0,
        current_eth_price=2950.0,
        hedge_realized_since_baseline=10.0,
        hedge_unrealized_since_baseline=2.0,
    )
    # HODL = baseline_amount0 * current_eth_price + baseline_amount1
    #     = 0.05 * 2950 + 150 = 297.5
    # IL natural = current_pool - HODL = 298 - 297.5 = +0.5  (pool higher than HODL)
    assert abs(breakdown["lp_fees_earned"] - 2.1) < 1e-9
    assert abs(breakdown["beefy_perf_fee"] - (-0.21)) < 1e-9  # 10% of 2.1
    assert abs(breakdown["il_natural"] - 0.5) < 1e-9
    assert abs(breakdown["hedge_pnl"] - 12.0) < 1e-9
    assert abs(breakdown["funding"] - 1.0) < 1e-9  # negated paid → received
    assert abs(breakdown["perp_fees_paid"] - (-0.5)) < 1e-9
    assert abs(breakdown["bootstrap_slippage"] - (-0.07)) < 1e-9
    # net = 2.1 - 0.21 + 0.5 + 12.0 + 1.0 - 0.5 - 0.07 = 14.82
    assert abs(breakdown["net_pnl"] - 14.82) < 0.01
