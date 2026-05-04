"""compute_operation_pnl: per-leg fields + IL with two oracle prices."""
from engine.operation import Operation, OperationState
from engine.pnl import compute_operation_pnl


def _op(**overrides) -> Operation:
    base = dict(
        id=1, started_at=0,
        state=OperationState.ACTIVE,
        baseline_eth_price=4000.0,
        baseline_pool_value_usd=300.0,
        baseline_amount0=100.0,
        baseline_amount1=0.0375,
        baseline_collateral=130.0,
        baseline_token0_usd_price=1.50,
        baseline_token1_usd_price=4000.0,
        perp_fees_paid_token0=0.45,
        perp_fees_paid_token1=0.32,
        funding_paid_token0=-1.30,
        funding_paid_token1=-0.95,
    )
    base.update(overrides)
    return Operation(**base)


def test_breakdown_includes_per_leg_fields():
    op = _op()
    bd = compute_operation_pnl(
        op,
        current_pool_value_usd=326.20,
        current_token0_usd_price=1.75,
        current_token1_usd_price=4200.0,
        hedge_realized_per_symbol={"ARB-USD": 5.0, "ETH-USD": -2.5},
        hedge_unrealized_per_symbol={"ARB-USD": -20.0, "ETH-USD": -3.0},
    )

    assert "hedge_pnl_token0" in bd
    assert "hedge_pnl_token1" in bd
    assert "perp_fees_paid_token0" in bd
    assert "funding_token0" in bd

    # Aggregates equal sums (within float tolerance)
    assert abs(bd["hedge_pnl"] - (bd["hedge_pnl_token0"] + bd["hedge_pnl_token1"])) < 1e-9
    assert abs(bd["perp_fees_paid"] - (bd["perp_fees_paid_token0"] + bd["perp_fees_paid_token1"])) < 1e-9
    assert abs(bd["funding"] - (bd["funding_token0"] + bd["funding_token1"])) < 1e-9


def test_il_natural_uses_two_oracle_prices_for_cross_pair():
    op = _op()  # baseline 100 ARB at $1.50 + 0.0375 WETH at $4000 = $300
    bd = compute_operation_pnl(
        op,
        current_pool_value_usd=326.20,
        current_token0_usd_price=1.75,
        current_token1_usd_price=4200.0,
        hedge_realized_per_symbol={},
        hedge_unrealized_per_symbol={},
    )
    # HODL = 100 * 1.75 + 0.0375 * 4200 = 175 + 157.5 = 332.5
    # IL = 326.20 - 332.5 = -6.30
    assert abs(bd["il_natural"] - (-6.30)) < 0.01


def test_single_leg_backward_compat():
    """Single-leg call (legacy signature) still works after the refactor.

    Single-leg case: token1 is USDC (= $1). baseline_amount0 holds the
    volatile token (WETH), baseline_eth_price holds its USD price.
    """
    op = _op(baseline_token0_usd_price=None, baseline_token1_usd_price=None)
    bd = compute_operation_pnl(
        op,
        current_pool_value_usd=200.0,
        current_eth_price=4200.0,
        hedge_realized_since_baseline=0.0,
        hedge_unrealized_since_baseline=0.0,
    )
    assert "il_natural" in bd
    assert "hedge_pnl" in bd
    assert "net_pnl" in bd
