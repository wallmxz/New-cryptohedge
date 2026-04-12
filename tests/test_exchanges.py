from exchanges.base import Order, Fill, Position


def test_order_creation():
    o = Order(order_id="abc-123", symbol="ARB", side="sell", size=50.0, price=1.06, status="open")
    assert o.order_id == "abc-123"
    assert o.is_open

def test_order_not_open():
    o = Order(order_id="abc", symbol="ARB", side="sell", size=50.0, price=1.06, status="filled")
    assert not o.is_open

def test_fill_creation():
    f = Fill(fill_id="f1", order_id="abc", symbol="ARB", side="sell", size=50.0, price=1.06, fee=0.015, fee_currency="USDC", liquidity="maker", realized_pnl=0.0, timestamp=1000.0)
    assert f.liquidity == "maker"
    assert f.fee == 0.015

def test_position_notional():
    p = Position(symbol="ARB", side="short", size=95.0, entry_price=1.05, unrealized_pnl=-1.20)
    assert p.notional == 95.0 * 1.05
