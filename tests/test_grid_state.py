from engine.grid_state import GridStop, lowest_buy, highest_sell, top_sell, bottom_buy


def test_gridstop_dataclass_fields():
    s = GridStop(cloid=12345, side="sell", trigger_price=0.135, size=3.0)
    assert s.cloid == 12345
    assert s.side == "sell"
    assert s.trigger_price == 0.135
    assert s.size == 3.0


def test_lowest_buy_returns_lowest_price_buy():
    grid = {
        1: GridStop(1, "buy", 0.130, 3.0),
        2: GridStop(2, "buy", 0.131, 3.0),
        3: GridStop(3, "sell", 0.140, 3.0),
    }
    result = lowest_buy(grid)
    assert result.cloid == 1
    assert result.trigger_price == 0.130


def test_highest_sell_returns_highest_price_sell():
    grid = {
        1: GridStop(1, "sell", 0.140, 3.0),
        2: GridStop(2, "sell", 0.142, 3.0),
        3: GridStop(3, "buy", 0.130, 3.0),
    }
    result = highest_sell(grid)
    assert result.cloid == 2
    assert result.trigger_price == 0.142


def test_top_sell_returns_highest_price_sell():
    """top_sell == highest_sell (alias for clarity in event-driven algo)."""
    grid = {1: GridStop(1, "sell", 0.140, 3.0), 2: GridStop(2, "sell", 0.142, 3.0)}
    assert top_sell(grid).trigger_price == 0.142


def test_bottom_buy_returns_lowest_price_buy():
    """bottom_buy == lowest_buy (alias)."""
    grid = {1: GridStop(1, "buy", 0.130, 3.0), 2: GridStop(2, "buy", 0.128, 3.0)}
    assert bottom_buy(grid).trigger_price == 0.128


def test_helpers_return_none_when_no_matching_side():
    grid = {1: GridStop(1, "sell", 0.140, 3.0)}
    assert lowest_buy(grid) is None
    assert bottom_buy(grid) is None
