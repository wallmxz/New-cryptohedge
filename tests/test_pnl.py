from engine.pnl import calc_pnl, PnLBreakdown


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
