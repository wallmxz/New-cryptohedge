from engine.orderbook import calc_maker_price, calc_aggressive_price, check_order_depth


def test_maker_sell_wide_spread():
    price = calc_maker_price(side="sell", best_bid=1.0000, best_ask=1.0010, tick=0.0001)
    assert price == 1.0009

def test_maker_sell_min_spread():
    price = calc_maker_price(side="sell", best_bid=1.0000, best_ask=1.0001, tick=0.0001)
    assert price == 1.0001

def test_maker_buy_wide_spread():
    price = calc_maker_price(side="buy", best_bid=1.0000, best_ask=1.0010, tick=0.0001)
    assert price == 1.0001

def test_maker_buy_min_spread():
    price = calc_maker_price(side="buy", best_bid=1.0000, best_ask=1.0001, tick=0.0001)
    assert price == 1.0000

def test_maker_sell_never_crosses_bid():
    price = calc_maker_price(side="sell", best_bid=1.0005, best_ask=1.0005, tick=0.0001)
    assert price > 1.0005

def test_maker_buy_never_crosses_ask():
    price = calc_maker_price(side="buy", best_bid=1.0005, best_ask=1.0005, tick=0.0001)
    assert price < 1.0005

def test_aggressive_sell():
    price = calc_aggressive_price(side="sell", best_bid=1.0000, best_ask=1.0010, tick=0.0001)
    assert price == 1.0001

def test_aggressive_buy():
    price = calc_aggressive_price(side="buy", best_bid=1.0000, best_ask=1.0010, tick=0.0001)
    assert price == 1.0009

def test_depth_at_best_level():
    book_bids = {1.06: 300, 1.0599: 180, 1.0598: 90}
    result = check_order_depth(side="buy", price=1.06, book_levels=book_bids)
    assert result == "HOLD"

def test_depth_at_second_level():
    book_bids = {1.06: 300, 1.0599: 180, 1.0598: 90}
    result = check_order_depth(side="buy", price=1.0599, book_levels=book_bids)
    assert result == "HOLD"

def test_depth_at_third_level_triggers_repost():
    book_bids = {1.06: 300, 1.0599: 180, 1.0598: 90}
    result = check_order_depth(side="buy", price=1.0598, book_levels=book_bids)
    assert result == "REPOST"

def test_depth_order_not_in_book():
    book_bids = {1.06: 300, 1.0599: 180}
    result = check_order_depth(side="buy", price=1.05, book_levels=book_bids)
    assert result == "REPOST"

def test_depth_sell_side():
    book_asks = {1.0610: 200, 1.0609: 150, 1.0608: 100}
    result = check_order_depth(side="sell", price=1.0610, book_levels=book_asks)
    assert result == "REPOST"
