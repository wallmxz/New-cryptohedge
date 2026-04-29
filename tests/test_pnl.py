from engine.pnl import calc_pnl, PnLBreakdown, compute_operation_pnl
from engine.operation import Operation, OperationState


def test_basic_pnl():
    result = calc_pnl(
        pool_value_usd=204.0, pool_deposited_usd=200.0,
        hedge_realized_pnl=0.0, hedge_unrealized_pnl=-3.80,
        funding_total=0.15, total_fees_paid=0.30,
    )
    assert isinstance(result, PnLBreakdown)
    assert result.pool_pnl == 4.0
    assert result.hedge_pnl == -3.80
    assert result.funding_pnl == 0.15
    assert result.fees_paid == 0.30
    assert abs(result.net_pnl - 0.05) < 0.001


def test_pnl_with_realized_hedge():
    result = calc_pnl(
        pool_value_usd=210.0, pool_deposited_usd=200.0,
        hedge_realized_pnl=-8.0, hedge_unrealized_pnl=-1.50,
        funding_total=0.50, total_fees_paid=0.40,
    )
    assert result.pool_pnl == 10.0
    assert result.hedge_pnl == -9.50
    assert abs(result.net_pnl - 0.60) < 0.001


def test_pnl_negative_pool():
    result = calc_pnl(
        pool_value_usd=195.0, pool_deposited_usd=200.0,
        hedge_realized_pnl=4.5, hedge_unrealized_pnl=0.0,
        funding_total=-0.10, total_fees_paid=0.20,
    )
    assert result.pool_pnl == -5.0
    assert result.hedge_pnl == 4.5
    assert abs(result.net_pnl - (-0.80)) < 0.001


def test_pnl_to_dict():
    result = calc_pnl(
        pool_value_usd=204.0, pool_deposited_usd=200.0,
        hedge_realized_pnl=0.0, hedge_unrealized_pnl=-3.80,
        funding_total=0.15, total_fees_paid=0.30,
    )
    d = result.to_dict()
    assert "pool_pnl" in d
    assert "hedge_pnl" in d
    assert "net_pnl" in d


def test_operation_pnl_breakdown():
    op = Operation(
        id=1, started_at=1000.0, state=OperationState.ACTIVE,
        baseline_eth_price=3000.0, baseline_pool_value_usd=300.0,
        baseline_amount0=0.05, baseline_amount1=150.0,
        baseline_collateral=130.0,
        perp_fees_paid=0.5,
        funding_paid=-1.0,    # bot received +$1 funding
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
