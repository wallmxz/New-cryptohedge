"""compute_operation_pnl: per-leg fields + IL with two oracle prices."""
import pytest

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


def test_compute_operation_pnl_uses_override_when_provided():
    """When hedge_pnl_aggregate_override is provided (e.g., from a
    venue-side cumulative pnl query), it replaces the per-leg sum and
    becomes the authoritative hedge_pnl. Per-leg fields collapse: the
    full value goes on token0 for display consistency, token1 = 0."""
    from engine.pnl import compute_operation_pnl
    from engine.operation import Operation, OperationState
    op = Operation(
        id=1, started_at=1700000000.0, state=OperationState.ACTIVE,
        baseline_eth_price=2000.0, baseline_pool_value_usd=50.0,
        baseline_amount0=0.01, baseline_amount1=100.0,
        baseline_collateral=100.0,
        baseline_token0_usd_price=2000.0, baseline_token1_usd_price=0.10,
    )
    out = compute_operation_pnl(
        op,
        current_pool_value_usd=50.0,
        current_token0_usd_price=2000.0,
        current_token1_usd_price=0.10,
        hedge_realized_per_symbol={"ETH-USD": 1.0, "ARB-USD": 2.0},
        hedge_unrealized_per_symbol={"ETH-USD": 0.5, "ARB-USD": 0.5},
        hedge_pnl_aggregate_override=-7.5,
    )
    assert out["hedge_pnl"] == -7.5
    assert out["hedge_pnl_token0"] == -7.5
    assert out["hedge_pnl_token1"] == 0.0


def test_compute_operation_pnl_keeps_per_leg_when_no_override():
    """When override is None (default), the existing per-leg sum
    behavior is preserved — backwards compatible."""
    from engine.pnl import compute_operation_pnl
    from engine.operation import Operation, OperationState
    op = Operation(
        id=1, started_at=1700000000.0, state=OperationState.ACTIVE,
        baseline_eth_price=2000.0, baseline_pool_value_usd=50.0,
        baseline_amount0=0.01, baseline_amount1=100.0,
        baseline_collateral=100.0,
        baseline_token0_usd_price=2000.0, baseline_token1_usd_price=0.10,
    )
    out = compute_operation_pnl(
        op,
        current_pool_value_usd=50.0,
        current_token0_usd_price=2000.0,
        current_token1_usd_price=0.10,
        hedge_realized_per_symbol={"ARB-USD": 2.0, "ETH-USD": 1.0},
        hedge_unrealized_per_symbol={"ARB-USD": 0.5, "ETH-USD": 0.5},
    )
    # sorted keys: ARB-USD < ETH-USD lexicographically -> token0_key="ARB-USD"
    assert out["hedge_pnl_token0"] == 2.5  # ARB realized + unrealized
    assert out["hedge_pnl_token1"] == 1.5  # ETH realized + unrealized
    assert out["hedge_pnl"] == 4.0


def test_compute_operation_pnl_uses_baseline_deposit_usd_when_set():
    """When op.baseline_deposit_usd > 0, pool_dollar = pool_now - baseline,
    overriding the HODL formula. il_natural alias mirrors the same value."""
    op = _op(baseline_deposit_usd=50.03)
    bd = compute_operation_pnl(
        op,
        current_pool_value_usd=51.58,
        current_token0_usd_price=1.75,
        current_token1_usd_price=4200.0,
        hedge_realized_per_symbol={},
        hedge_unrealized_per_symbol={},
    )
    assert bd["pool_dollar"] == 1.55  # 51.58 - 50.03
    assert bd["il_natural"] == 1.55  # alias = same
    assert bd["baseline_deposit_usd"] == 50.03


def test_compute_operation_pnl_falls_back_to_hodl_when_baseline_deposit_null():
    """Without baseline set, pool_dollar uses the HODL formula (legacy)."""
    op = _op(baseline_deposit_usd=None)
    bd = compute_operation_pnl(
        op,
        current_pool_value_usd=326.20,
        current_token0_usd_price=1.75,
        current_token1_usd_price=4200.0,
        hedge_realized_per_symbol={},
        hedge_unrealized_per_symbol={},
    )
    # HODL: 100 * 1.75 + 0.0375 * 4200 = 175.0 + 157.5 = 332.5
    # pool_dollar = 326.20 - 332.5 = -6.30
    assert bd["pool_dollar"] == -6.3
    assert bd["il_natural"] == -6.3
    assert bd["baseline_deposit_usd"] is None


def test_compute_operation_pnl_falls_back_to_hodl_when_baseline_deposit_zero():
    """Defensive: 0 or negative values fall back to HODL too."""
    op = _op(baseline_deposit_usd=0.0)
    bd = compute_operation_pnl(
        op,
        current_pool_value_usd=326.20,
        current_token0_usd_price=1.75,
        current_token1_usd_price=4200.0,
        hedge_realized_per_symbol={},
        hedge_unrealized_per_symbol={},
    )
    # Same HODL fallback as the null case: -6.30
    assert bd["pool_dollar"] == -6.3


def test_compute_operation_pnl_uses_funding_override_when_provided():
    """When funding_override=(token0_paid, token1_paid) is passed,
    compute_operation_pnl uses those values directly and IGNORES
    op.funding_paid_token0/1 from the DB. Sign matches existing behavior:
    positive in override = positive in DB column = 'we paid'.
    Display sign in breakdown is INVERTED from input (received convention).
    """
    op = _op(funding_paid_token0=999.0, funding_paid_token1=999.0)
    # Override says we paid 10 in t0, received 5 in t1 (negative = received)
    bd = compute_operation_pnl(
        op,
        current_pool_value_usd=326.20,
        current_token0_usd_price=1.75,
        current_token1_usd_price=4200.0,
        hedge_realized_per_symbol={},
        hedge_unrealized_per_symbol={},
        funding_override=(10.0, -5.0),
    )
    # breakdown["funding_token0"] = -override[0] = -10 (we paid 10 → display -10)
    # breakdown["funding_token1"] = -override[1] = 5 (we received 5 → display +5)
    assert bd["funding_token0"] == pytest.approx(-10.0)
    assert bd["funding_token1"] == pytest.approx(5.0)
    # Aggregate matches sum of per-leg.
    assert bd["funding"] == pytest.approx(-5.0)
