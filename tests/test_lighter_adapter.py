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
        DEFAULT_IOC_EXPIRY = 0
        DEFAULT_28_DAY_ORDER_EXPIRY = -1

        def __init__(self, **kwargs):
            self.kwargs = kwargs
            # Mimic the real SDK's nonce_manager: production code allocates
            # (api_key_index, nonce) before each create_order to work around
            # a missing decorator in lighter SDK 1.x.
            self.nonce_manager = MagicMock()
            self.nonce_manager.next_nonce = MagicMock(return_value=(0, 1))
            self.nonce_manager.acknowledge_failure = MagicMock(return_value=None)
            self.nonce_manager.hard_refresh_nonce = MagicMock(return_value=None)

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


def _seed_book(a, market_index: int, bid: float, ask: float) -> None:
    """Seed the WS top-of-book cache the way the real WS pump would after
    a `subscribed/order_book` snapshot. Production reads from this cache
    in `_place_long_term_order_unlocked` (no HTTP get_best_price calls).
    Bid and ask are display-units floats (e.g., 2399.5)."""
    import time as _t
    a._ws_book_top[market_index] = {
        "best_bid": bid, "best_ask": ask, "ts": _t.time(),
    }


def _seed_position(a, market_index: int, *, sign: int, size: float,
                   avg_entry: float, unrealized: float = 0.0) -> None:
    """Seed the adapter's observed-short-size + metadata caches."""
    a._observed_short_size[market_index] = size if sign == -1 else 0.0
    a._observed_position_meta[market_index] = {
        "sign": sign,
        "position": size,
        "avg_entry_price": avg_entry,
        "unrealized_pnl": unrealized,
    }


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
    always read bid (for sell) or ask (for buy) from the WS book cache."""
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta()
    # Seed WS cache: bid $2399, ask $2400. price_decimals=2 in _meta().
    _seed_book(a, 0, bid=2399.0, ask=2400.0)

    # Stub signer with recording mocks
    a._signer = MagicMock()
    # nonce_manager.next_nonce() must return (api_key_index, nonce) tuple
    # — production code allocates one before each create_order to work
    # around a missing decorator in lighter SDK 1.x.
    a._signer.nonce_manager.next_nonce = MagicMock(return_value=(0, 1))
    a._signer.create_order = AsyncMock(return_value=(None, MagicMock(tx_hash="0xabc"), None))

    # Stub _verify_fill to claim full fill
    async def fake_verify(meta, cloid_int, expected_size):
        return expected_size, 2399.0
    a._verify_fill = fake_verify  # type: ignore

    # SELL → should hit bid from cache
    order = await a.place_long_term_order(
        symbol="ETH-USD", side="sell", size=0.01,
        price=2350.0,  # garbage hint, must be ignored
        cloid_int=12345,
    )
    assert order.status == "filled"
    # Verify the price sent to create_order is the cached bid (ticks:
    # bid=2399 * 10^2 = 239900) — exact, no buffer. The user requires
    # zero slippage by construction; on book-tick moves during flight
    # the IOC will simply auto-cancel and the engine's per-leg cooldown
    # (30s) governs when to re-fire.
    create_kwargs = a._signer.create_order.call_args.kwargs
    assert create_kwargs["price"] == 239900
    assert create_kwargs["is_ask"] is True  # selling

    # BUY → should hit ask, exact (no buffer above).
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
    _seed_book(a, 0, bid=2399.0, ask=2400.0)
    a._signer = MagicMock()
    a._signer.nonce_manager.next_nonce = MagicMock(return_value=(0, 1))
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
async def test_place_order_no_retry_after_server_accept():
    """REGRESSION: when create_order returns success (err=None) but
    _verify_fill comes back 0, the adapter must NOT retry. Retrying
    after a server-accept means a SECOND order on the same side, which
    is what produced the 0.9 ETH over-hedge in the 2026-05-06 session.
    """
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta()
    _seed_book(a, 0, bid=2399.0, ask=2400.0)
    a._signer = MagicMock()
    a._signer.nonce_manager.next_nonce = MagicMock(return_value=(0, 1))
    a._signer.create_order = AsyncMock(return_value=(None, MagicMock(), None))
    # _verify_fill always returns 0 (book moved or stale lookup)
    async def fake_verify(meta, cloid_int, expected_size):
        return 0.0, 0.0
    a._verify_fill = fake_verify  # type: ignore
    # Position unchanged across the call — fallback inference also says
    # "didn't fill". Adapter must return cancelled with size=0, NOT retry.
    a.get_position = AsyncMock(return_value=None)

    order = await a.place_long_term_order(
        symbol="ETH-USD", side="buy", size=0.01, price=0,
        cloid_int=999,
    )
    assert order.size == 0.0
    assert order.status == "cancelled"
    # Critical: only ONE create_order call, even though verify_fill=0.
    assert a._signer.create_order.await_count == 1


@pytest.mark.asyncio
async def test_place_order_infers_fill_from_position_change():
    """When verify_fill misses but get_position shows the short grew by
    the requested size, treat as filled (don't retry, don't pretend
    cancelled — that would make the engine fire ANOTHER order).
    """
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta()
    _seed_book(a, 0, bid=2399.0, ask=2400.0)
    a._signer = MagicMock()
    a._signer.nonce_manager.next_nonce = MagicMock(return_value=(0, 1))
    a._signer.create_order = AsyncMock(return_value=(None, MagicMock(), None))
    async def fake_verify(meta, cloid_int, expected_size):
        return 0.0, 0.0  # missed
    a._verify_fill = fake_verify  # type: ignore
    # Pre: no position. Post: short of 0.01 ETH. Delta = 0.01 = requested.
    pre_pos = None
    post_pos = MagicMock(side="short", size=0.01)
    a.get_position = AsyncMock(side_effect=[pre_pos, post_pos])

    order = await a.place_long_term_order(
        symbol="ETH-USD", side="sell", size=0.01, price=0,
        cloid_int=999,
    )
    assert order.size == 0.01
    assert order.status == "filled"
    assert a._signer.create_order.await_count == 1


@pytest.mark.asyncio
async def test_size_below_step_raises():
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta(size_decimals=4, step_size=0.0001)
    with pytest.raises(ValueError, match="below market step"):
        await a.place_long_term_order(
            symbol="ETH-USD", side="buy", size=1e-9, price=0, cloid_int=1,
        )


@pytest.mark.asyncio
async def test_get_position_reads_from_ws_cache():
    """get_position must NOT call /account — it reads the WS-cached
    position. Sustained 1Hz HTTP polling triggered the CloudFront WAF
    in the 2026-05-07 session."""
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta()
    # Seed a SHORT position of 0.05 ETH @ $2390 entry, $1.20 unrealized.
    _seed_position(
        a, market_index=0, sign=-1, size=0.05,
        avg_entry=2390.0, unrealized=1.20,
    )
    pos = await a.get_position("ETH-USD")
    assert pos is not None
    assert pos.side == "short"
    assert pos.size == 0.05
    assert pos.entry_price == 2390.0
    assert pos.unrealized_pnl == 1.20
    # Empty cache → None (closed position or pre-snapshot).
    a._observed_short_size = {}
    a._observed_position_meta = {}
    assert await a.get_position("ETH-USD") is None


@pytest.mark.asyncio
async def test_get_collateral_reads_from_ws_cache():
    a = _make_adapter()
    a._ws_collateral = 137.42
    assert await a.get_collateral() == 137.42
    # Pre-snapshot: returns 0.0 (engine treats 0 as "unknown").
    a._ws_collateral = None
    assert await a.get_collateral() == 0.0


@pytest.mark.asyncio
async def test_get_oracle_prices_uses_cached_book_midpoint():
    """Midpoint of cached top-of-book is the new oracle. Previously this
    was last_trade_price from /orderBookDetails (HTTP), which doubled
    the polling rate."""
    a = _make_adapter()
    a._markets["ETH-USD"] = _meta()
    a._markets["ARB-USD"] = _meta(symbol_user="ARB-USD", market_index=1)
    _seed_book(a, 0, bid=2399.0, ask=2401.0)
    _seed_book(a, 1, bid=0.1265, ask=0.1267)
    prices = await a.get_oracle_prices(["ETH-USD", "ARB-USD"])
    assert prices["ETH-USD"] == 2400.0  # midpoint
    assert prices["ARB-USD"] == 0.1266
    # Empty cache: returns 0.0 per symbol so callers can detect "unknown".
    a._ws_book_top = {}
    prices = await a.get_oracle_prices(["ETH-USD"])
    assert prices["ETH-USD"] == 0.0


def test_on_book_update_extracts_best_bid_and_ask():
    """The WS callback must turn a multi-level book into best_bid/best_ask.
    Lighter sends prices as strings."""
    a = _make_adapter()
    a._on_book_update(
        market_id=7,
        state={
            "asks": [
                {"price": "100.50", "size": "1"},
                {"price": "100.10", "size": "5"},
                {"price": "100.20", "size": "2"},
            ],
            "bids": [
                {"price": "99.80", "size": "3"},
                {"price": "99.95", "size": "1"},
                {"price": "99.90", "size": "2"},
            ],
        },
    )
    top = a._ws_book_top[7]
    assert top["best_ask"] == 100.10  # lowest ask
    assert top["best_bid"] == 99.95   # highest bid
    assert a._ws_first_snapshot.is_set()


def test_on_account_update_extracts_positions_and_collateral():
    """REGRESSION: the WS account_all spec — per
    apidocs.lighter.xyz/docs/websocket-reference — has TOP-LEVEL fields:

      - `account` is the integer account_id (NOT a nested object!)
      - `available_balance`, `collateral`, `positions` (DICT keyed by
        market_id), `assets`, `funding_histories` are all top-level.

    Earlier version of this parser drilled into `state["account"]` as a
    dict and treated `positions` as a LIST. Both wrong: parse silently
    failed, cache was empty, engine kept firing hedges thinking
    `position == 0` after each successful fill → over-hedge stack.
    """
    a = _make_adapter()
    a._on_account_update(
        account_id=42,
        state={
            "type": "subscribed/account_all",
            "channel": "account_all:42",
            "timestamp": 1700000000000,
            "account": 42,  # integer account_id at top level
            "available_balance": "150.75",
            "collateral": "200.00",
            "assets": {
                "1": {"symbol": "USDC", "asset_id": 1, "balance": "150.75",
                      "locked_balance": "0"},
            },
            "positions": {
                # Dict keyed by market_id string — NOT a list!
                "0": {
                    "market_id": 0, "symbol": "ETH-USD", "sign": -1,
                    "position": "0.05", "avg_entry_price": "2390.0",
                    "unrealized_pnl": "1.20",
                    "position_value": "119.5", "realized_pnl": "0",
                },
                "50": {
                    "market_id": 50, "symbol": "ARB-USD", "sign": 1,
                    "position": "100.0", "avg_entry_price": "0.13",
                    "unrealized_pnl": "-0.50",
                    "position_value": "13", "realized_pnl": "0",
                },
            },
        },
    )
    assert a._ws_collateral == 150.75
    # Magnitude (engine-facing): only shorts (sign=-1) carry magnitude.
    # Longs are tracked as 0 in this dict so the engine's drift math
    # doesn't try to hedge against an operator-opened long; the long
    # is still surfaced through the diagnostic metadata dict.
    assert a._observed_short_size[0] == 0.05  # ETH short
    assert a._observed_short_size[50] == 0.0  # ARB long → 0 magnitude
    # Metadata (diagnostic-facing):
    assert a._observed_position_meta[0]["sign"] == -1
    assert a._observed_position_meta[0]["avg_entry_price"] == 2390.0
    assert a._observed_position_meta[50]["sign"] == 1
    assert a._observed_position_meta[50]["position"] == 100.0

    # Live-account variant: top-level `available_balance` is null and
    # the real collateral lives inside `assets[asset_id].margin_balance`.
    # Probed empirically against the user's mainnet account (issue
    # 2026-05-07). The parser must fall back to summing assets.
    a2 = _make_adapter()
    a2._on_account_update(
        account_id=42,
        state={
            "type": "subscribed/account_all",
            "account": 42,
            "available_balance": None,
            "assets": {
                "3": {
                    "symbol": "USDC", "asset_id": 3,
                    "balance": "0.000000", "locked_balance": "0",
                    "margin_balance": "197.851681701356",
                },
            },
            "positions": {},
        },
    )
    assert abs(a2._ws_collateral - 197.85168170) < 1e-6

    # Update where ETH position closes: per docs, a closed position
    # DISAPPEARS from the dict (no size=0 sentinel). The cache must
    # reflect that — ETH gone, ARB still there.
    a._on_account_update(
        account_id=42,
        state={
            "type": "update/account_all",
            "channel": "account_all:42",
            "account": 42,
            "available_balance": "100.0",
            "positions": {
                "50": {
                    "market_id": 50, "sign": 1, "position": "100.0",
                    "avg_entry_price": "0.13", "unrealized_pnl": "-0.30",
                },
            },
        },
    )
    # After ETH closes:
    assert 0 not in a._observed_short_size
    # ARB long → 0 magnitude, but still tracked.
    assert a._observed_short_size[50] == 0.0
    assert a._observed_position_meta[50]["position"] == 100.0
    assert a._ws_collateral == 100.0

    # Empty positions dict (everything closed) wipes everything.
    a._on_account_update(
        account_id=42,
        state={"account": 42, "available_balance": "200.0", "positions": {}},
    )
    # After everything closes:
    assert a._observed_short_size == {}
    assert a._observed_position_meta == {}
    assert a._ws_collateral == 200.0


@pytest.mark.asyncio
async def test_parallel_orders_serialize_with_min_gap(monkeypatch):
    """Two place_long_term_order calls in parallel must NOT race the
    nonce_manager — the adapter's internal lock + 600ms min-gap should
    serialize them. Without this, Lighter rejects the second with
    code=21120 invalid signature (the loser of the next_nonce race
    signs a stale nonce). Regression test for the ARB/WETH dual-leg
    failure on 2026-05-07.
    """
    import asyncio
    a = _make_adapter()
    # Shorten the gap so the test doesn't take ages but still verifies
    # the lock + gap mechanism is wired up.
    a._MIN_GAP_S = 0.05
    a._markets["ETH-USD"] = _meta()
    a._markets["ARB-USD"] = _meta(market_index=1)
    # Both markets need book cache seeded — production adapter reads
    # them from the WS pump, here we seed directly.
    _seed_book(a, 0, bid=99.95, ask=100.05)
    _seed_book(a, 1, bid=99.95, ask=100.05)

    # Record the order in which create_order calls actually hit the signer.
    call_log: list[tuple[float, str]] = []
    a._signer = MagicMock()
    a._signer.nonce_manager.next_nonce = MagicMock(return_value=(0, 1))
    a._signer.nonce_manager.acknowledge_failure = MagicMock(return_value=None)

    async def fake_create_order(**kw):
        # Simulate ~10ms of server work
        call_log.append((asyncio.get_event_loop().time(), str(kw.get("market_index"))))
        await asyncio.sleep(0.01)
        return MagicMock(tx_hash="0x" + "a" * 64), MagicMock(code=0), None

    a._signer.create_order = fake_create_order
    a.get_position = AsyncMock(return_value=None)
    a._verify_fill = AsyncMock(return_value=(0.001, 100.0))

    # Fire both legs in parallel — exactly what the previous lifecycle
    # did via asyncio.gather. The lock must serialize them.
    await asyncio.gather(
        a.place_long_term_order(
            symbol="ETH-USD", side="sell", size=0.001, price=100, cloid_int=1,
        ),
        a.place_long_term_order(
            symbol="ARB-USD", side="sell", size=0.001, price=100, cloid_int=2,
        ),
    )

    # Both must have hit the signer, but the gap between them must be
    # ≥ _MIN_GAP_S (= 50ms here) — proving serialization, not parallel.
    assert len(call_log) == 2, f"Expected 2 calls, got {len(call_log)}"
    gap_ms = (call_log[1][0] - call_log[0][0]) * 1000
    assert gap_ms >= 45, (
        f"Calls were too close ({gap_ms:.1f}ms apart), lock/gap not "
        f"working — would race the nonce_manager on real Lighter."
    )
