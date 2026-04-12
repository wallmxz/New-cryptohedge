from chains.evm import calc_pool_position


def test_calc_pool_position():
    result = calc_pool_position(
        cow_balance=100.0, total_supply=1000.0,
        vault_token0=15000.0, vault_token1=3.0,
        price_token0_usd=1.05, price_token1_usd=3500.0,
    )
    assert result["my_token0"] == 1500.0
    assert result["my_token1"] == 0.3
    assert result["value_usd"] == 2625.0


def test_calc_pool_position_zero_supply():
    result = calc_pool_position(
        cow_balance=0.0, total_supply=0.0,
        vault_token0=0.0, vault_token1=0.0,
        price_token0_usd=1.0, price_token1_usd=3500.0,
    )
    assert result["value_usd"] == 0.0
    assert result["my_token0"] == 0.0
