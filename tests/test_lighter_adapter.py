"""Unit tests for LighterAdapter.

The lighter SDK is mocked at sys.modules level so these run in any
environment without needing the real package. The conftest.py already
has a similar pattern for dydx_v4_client.
"""
from __future__ import annotations
import sys
import types
import pytest
from unittest.mock import AsyncMock, MagicMock


def _install_lighter_stub() -> None:
    """Inject minimal lighter SDK stubs so `from lighter import ...` works."""
    if "lighter" in sys.modules:
        return  # real SDK present (CI/Linux); leave it alone
    pkg = types.ModuleType("lighter")

    class _SignerClient:
        ORDER_TYPE_LIMIT = 0
        ORDER_TYPE_MARKET = 1
        ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL = 0
        ORDER_TIME_IN_FORCE_GOOD_TILL_TIME = 1
        ORDER_TIME_IN_FORCE_POST_ONLY = 2

        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def get_best_price(self, market_index, is_ask, ob_orders=None):
            return 100  # placeholder; tests will mock this

        async def create_order(self, **kwargs):
            return None, None, None

        async def cancel_order(self, **kwargs):
            return None, None, None

        async def close(self):
            pass

    class _ApiClient:
        def __init__(self, configuration=None):
            self.configuration = configuration

        async def close(self):
            pass

    class _Configuration:
        def __init__(self, host=None):
            self.host = host

    class _OrderApi:
        def __init__(self, api_client):
            self.api_client = api_client

        async def order_book_details(self, market_id=None):
            return MagicMock(order_book_details=[])

        async def account_active_orders(self, **kw):
            return MagicMock(orders=[])

        async def account_inactive_orders(self, **kw):
            return MagicMock(orders=[])

    class _AccountApi:
        def __init__(self, api_client):
            self.api_client = api_client

        async def account(self, **kw):
            return MagicMock(accounts=[])

    pkg.SignerClient = _SignerClient
    pkg.ApiClient = _ApiClient
    pkg.Configuration = _Configuration
    pkg.OrderApi = _OrderApi
    pkg.AccountApi = _AccountApi
    sys.modules["lighter"] = pkg


_install_lighter_stub()


from exchanges.lighter import LighterAdapter, _MarketMeta


def _meta(symbol_user="ETH-USD", **kw):
    """Helper to build a market metadata fixture."""
    return _MarketMeta(
        symbol_user=symbol_user,
        symbol_lighter=symbol_user.split("-")[0],
        market_index=kw.get("market_index", 0),
        price_decimals=kw.get("price_decimals", 2),
        size_decimals=kw.get("size_decimals", 4),
        tick_size=kw.get("tick_size", 0.01),
        step_size=kw.get("step_size", 0.0001),
        min_base_amount=kw.get("min_base_amount", 0.005),
        min_quote_amount=kw.get("min_quote_amount", 10.0),
    )


def _make_adapter():
    a = LighterAdapter(
        url="https://stub",
        account_index=42,
        api_private_key="0x" + "1" * 64,
        api_key_index=1,
    )
    return a


@pytest.mark.asyncio
async def test_size_to_int_uses_market_decimals():
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta(size_decimals=4)
    assert a._size_to_int(0.0050, a._markets["ETH-USD"]) == 50
    assert a._size_to_int(1.2345, a._markets["ETH-USD"]) == 12345


@pytest.mark.asyncio
async def test_int_to_price_uses_market_decimals():
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta(price_decimals=2)
    # 237824 → $2378.24
    assert a._int_to_price(237824, a._markets["ETH-USD"]) == 2378.24


@pytest.mark.asyncio
async def test_unknown_symbol_raises():
    a = _make_adapter()
    with pytest.raises(KeyError, match="not in Lighter"):
        a._market_meta_or_raise("FAKE-USD")


@pytest.mark.asyncio
async def test_place_order_reads_bid_for_sell_and_ask_for_buy(monkeypatch):
    """The constraint: never use the engine's `price` arg as slippage hint —
    always read bid (for sell) or ask (for buy) from get_best_price."""
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta()

    # Stub signer with recording mocks
    a._signer = MagicMock()
    # get_best_price: when called with is_ask=True returns 240000 (=$2400 ask),
    # is_ask=False returns 239900 (=$2399 bid)
    async def fake_best_price(mi, is_ask, ob_orders=None):
        return 240000 if is_ask else 239900
    a._signer.get_best_price = fake_best_price
    a._signer.create_order = AsyncMock(return_value=(None, MagicMock(tx_hash="0xabc"), None))

    # Stub _verify_fill to claim full fill
    async def fake_verify(meta, cloid_int, expected_size):
        return expected_size, 2399.0
    a._verify_fill = fake_verify  # type: ignore

    # SELL → should hit bid (is_ask=False on get_best_price)
    order = await a.place_long_term_order(
        symbol="ETH-USD", side="sell", size=0.01,
        price=2350.0,  # garbage hint, must be ignored
        cloid_int=12345,
    )
    assert order.status == "filled"
    # Verify the price sent to create_order was the bid (239900), NOT 2350.0
    create_kwargs = a._signer.create_order.call_args.kwargs
    assert create_kwargs["price"] == 239900
    assert create_kwargs["is_ask"] is True  # selling

    # BUY → should hit ask
    a._signer.create_order.reset_mock()
    order = await a.place_long_term_order(
        symbol="ETH-USD", side="buy", size=0.01,
        price=99999.0,  # garbage
        cloid_int=12346,
    )
    create_kwargs = a._signer.create_order.call_args.kwargs
    assert create_kwargs["price"] == 240000
    assert create_kwargs["is_ask"] is False  # buying


@pytest.mark.asyncio
async def test_place_order_uses_ioc_limit():
    """Constraint: order_type=LIMIT, time_in_force=IOC. Never market order."""
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta()
    a._signer = MagicMock()
    async def fake_best_price(mi, is_ask, ob_orders=None):
        return 240000
    a._signer.get_best_price = fake_best_price
    a._signer.create_order = AsyncMock(return_value=(None, MagicMock(), None))

    async def fake_verify(meta, cloid_int, expected_size):
        return expected_size, 2400.0
    a._verify_fill = fake_verify  # type: ignore

    await a.place_long_term_order(
        symbol="ETH-USD", side="buy", size=0.01, price=0,
        cloid_int=999,
    )
    kw = a._signer.create_order.call_args.kwargs
    # SignerClient.ORDER_TYPE_LIMIT == 0, ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL == 0
    assert kw["order_type"] == 0
    assert kw["time_in_force"] == 0


@pytest.mark.asyncio
async def test_place_order_retries_when_book_moves():
    """If first IOC doesn't fill (book moved), retry with refreshed bid/ask."""
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta()
    a._signer = MagicMock()

    # Best price drifts up each call (book moving against us on a buy)
    prices = [240000, 240010, 240020]
    async def fake_best_price(mi, is_ask, ob_orders=None):
        return prices.pop(0) if prices else 240020
    a._signer.get_best_price = fake_best_price
    a._signer.create_order = AsyncMock(return_value=(None, MagicMock(), None))

    # First two attempts: fill returns 0 (didn't fill). Third: success.
    fill_results = [(0.0, 0.0), (0.0, 0.0), (0.01, 2400.20)]
    async def fake_verify(meta, cloid_int, expected_size):
        return fill_results.pop(0)
    a._verify_fill = fake_verify  # type: ignore

    order = await a.place_long_term_order(
        symbol="ETH-USD", side="buy", size=0.01, price=0,
        cloid_int=999,
    )
    assert order.size == 0.01
    # Third call's price was the most recent observed ask (240020)
    final_kw = a._signer.create_order.call_args.kwargs
    assert final_kw["price"] == 240020


@pytest.mark.asyncio
async def test_place_order_raises_when_no_fill_after_all_retries():
    """If all retries fail, raise — never silently swallow."""
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta()
    a._signer = MagicMock()
    async def fake_best_price(mi, is_ask, ob_orders=None):
        return 240000
    a._signer.get_best_price = fake_best_price
    a._signer.create_order = AsyncMock(return_value=(None, MagicMock(), None))

    async def fake_verify(meta, cloid_int, expected_size):
        return 0.0, 0.0  # never fills
    a._verify_fill = fake_verify  # type: ignore

    with pytest.raises(RuntimeError, match="failed to fill"):
        await a.place_long_term_order(
            symbol="ETH-USD", side="buy", size=0.01, price=0,
            cloid_int=999,
        )


@pytest.mark.asyncio
async def test_size_below_step_raises():
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta(size_decimals=4, step_size=0.0001)
    with pytest.raises(ValueError, match="below market step"):
        await a.place_long_term_order(
            symbol="ETH-USD", side="buy", size=1e-9, price=0, cloid_int=1,
        )
