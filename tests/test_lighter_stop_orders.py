"""Tests for LighterAdapter.place_stop_limit_order (Task A6).

Verifies SDK is called with correct fixed-point conversions, is_ask flag,
and that limit_price == trigger_price (no slippage by design).
"""
import pytest
from unittest.mock import AsyncMock, MagicMock

from exchanges.lighter import LighterAdapter, _MarketMeta


def _make_adapter_with_meta(meta: _MarketMeta, symbol: str) -> LighterAdapter:
    """Build an adapter instance bypassing __init__ (which needs network)."""
    adapter = LighterAdapter.__new__(LighterAdapter)
    adapter._signer = MagicMock()
    adapter._signer.create_sl_limit_order = AsyncMock(
        return_value=(MagicMock(), MagicMock(), None),
    )
    adapter._markets = {symbol: meta}
    return adapter


@pytest.mark.asyncio
async def test_place_stop_limit_order_calls_sdk_with_correct_params():
    """SDK receives base_amount/trigger_price/price in raw int units,
    is_ask reflects side, limit price == trigger price."""
    meta = _MarketMeta(
        symbol_user="ARB-USD",
        symbol_lighter="ARB",
        market_index=50,
        price_decimals=5,
        size_decimals=1,
        tick_size=0.00001,
        step_size=0.1,
        min_base_amount=0.1,
        min_quote_amount=1.0,
    )
    adapter = _make_adapter_with_meta(meta, "ARB-USD")

    await adapter.place_stop_limit_order(
        symbol="ARB-USD",
        side="sell",
        size=3.5,
        trigger_price=0.135,
        cloid_int=12345,
    )

    call = adapter._signer.create_sl_limit_order.call_args
    assert call.kwargs["market_index"] == 50
    assert call.kwargs["base_amount"] == 35           # 3.5 * 10^1
    assert call.kwargs["trigger_price"] == 13500      # 0.135 * 10^5
    assert call.kwargs["price"] == 13500              # limit = trigger (exact)
    assert call.kwargs["is_ask"] is True              # sell
    assert call.kwargs["client_order_index"] == 12345
    assert call.kwargs["reduce_only"] is False


@pytest.mark.asyncio
async def test_place_stop_limit_order_buy_side():
    """Buy side → is_ask=False."""
    meta = _MarketMeta(
        symbol_user="ETH-USD",
        symbol_lighter="ETH",
        market_index=1,
        price_decimals=2,
        size_decimals=4,
        tick_size=0.01,
        step_size=0.0001,
        min_base_amount=0.0001,
        min_quote_amount=1.0,
    )
    adapter = _make_adapter_with_meta(meta, "ETH-USD")

    await adapter.place_stop_limit_order(
        symbol="ETH-USD",
        side="buy",
        size=0.05,
        trigger_price=3500.50,
        cloid_int=999,
        reduce_only=True,
    )

    call = adapter._signer.create_sl_limit_order.call_args
    assert call.kwargs["market_index"] == 1
    assert call.kwargs["base_amount"] == 500           # 0.05 * 10^4
    assert call.kwargs["trigger_price"] == 350050      # 3500.50 * 10^2
    assert call.kwargs["price"] == 350050
    assert call.kwargs["is_ask"] is False              # buy
    assert call.kwargs["reduce_only"] is True


@pytest.mark.asyncio
async def test_place_stop_limit_order_zero_size_raises():
    meta = _MarketMeta(
        symbol_user="ARB-USD",
        symbol_lighter="ARB",
        market_index=50,
        price_decimals=5,
        size_decimals=1,
        tick_size=0.00001,
        step_size=0.1,
        min_base_amount=0.1,
        min_quote_amount=1.0,
    )
    adapter = _make_adapter_with_meta(meta, "ARB-USD")

    with pytest.raises(ValueError, match="below market step"):
        await adapter.place_stop_limit_order(
            symbol="ARB-USD", side="sell", size=0.01,  # rounds to 0
            trigger_price=0.135, cloid_int=1,
        )


@pytest.mark.asyncio
async def test_place_stop_limit_order_sdk_error_raises():
    meta = _MarketMeta(
        symbol_user="ARB-USD",
        symbol_lighter="ARB",
        market_index=50,
        price_decimals=5,
        size_decimals=1,
        tick_size=0.00001,
        step_size=0.1,
        min_base_amount=0.1,
        min_quote_amount=1.0,
    )
    adapter = _make_adapter_with_meta(meta, "ARB-USD")
    adapter._signer.create_sl_limit_order = AsyncMock(
        return_value=(MagicMock(), MagicMock(), "bad signature"),
    )

    with pytest.raises(RuntimeError, match="bad signature"):
        await adapter.place_stop_limit_order(
            symbol="ARB-USD", side="sell", size=3.5,
            trigger_price=0.135, cloid_int=1,
        )


@pytest.mark.asyncio
async def test_place_stop_limit_order_cloid_masked_to_32bit():
    meta = _MarketMeta(
        symbol_user="ARB-USD",
        symbol_lighter="ARB",
        market_index=50,
        price_decimals=5,
        size_decimals=1,
        tick_size=0.00001,
        step_size=0.1,
        min_base_amount=0.1,
        min_quote_amount=1.0,
    )
    adapter = _make_adapter_with_meta(meta, "ARB-USD")

    big_cloid = (1 << 40) | 12345  # bits above 32 should be stripped
    await adapter.place_stop_limit_order(
        symbol="ARB-USD", side="sell", size=3.5,
        trigger_price=0.135, cloid_int=big_cloid,
    )
    call = adapter._signer.create_sl_limit_order.call_args
    assert call.kwargs["client_order_index"] == big_cloid & 0xFFFFFFFF


# ────────────────────────────────────────────────────────────────────────────
# Task A7: cancel_stop_order + cancel_all_stops
# ────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cancel_stop_order_calls_sdk():
    """cancel_stop_order routes to SDK cancel_order with market_index +
    order_index. SDK returns 3-tuple (CancelOrder, RespSendTx, err_or_None)."""
    adapter = LighterAdapter.__new__(LighterAdapter)
    adapter._signer = MagicMock()
    adapter._signer.cancel_order = AsyncMock(
        return_value=(MagicMock(), MagicMock(), None),
    )
    meta = MagicMock()
    meta.market_index = 50
    adapter._market_meta_or_raise = MagicMock(return_value=meta)

    await adapter.cancel_stop_order(symbol="ARB-USD", order_index=987)
    adapter._signer.cancel_order.assert_called_once_with(
        market_index=50, order_index=987,
    )


@pytest.mark.asyncio
async def test_cancel_stop_order_sdk_error_raises():
    adapter = LighterAdapter.__new__(LighterAdapter)
    adapter._signer = MagicMock()
    adapter._signer.cancel_order = AsyncMock(
        return_value=(None, None, "not found"),
    )
    meta = MagicMock()
    meta.market_index = 50
    adapter._market_meta_or_raise = MagicMock(return_value=meta)

    with pytest.raises(RuntimeError, match="not found"):
        await adapter.cancel_stop_order(symbol="ARB-USD", order_index=987)


@pytest.mark.asyncio
async def test_cancel_all_stops_calls_sdk():
    """cancel_all_stops routes to SDK cancel_all_orders. SDK requires
    time_in_force + timestamp_ms; we pass IMMEDIATE + current ms.
    Note: SDK cancels ALL orders for the account, not market-scoped."""
    adapter = LighterAdapter.__new__(LighterAdapter)
    adapter._signer = MagicMock()
    adapter._signer.cancel_all_orders = AsyncMock(
        return_value=(MagicMock(), MagicMock(), None),
    )
    meta = MagicMock()
    meta.market_index = 50
    adapter._market_meta_or_raise = MagicMock(return_value=meta)

    await adapter.cancel_all_stops(symbol="ARB-USD")
    adapter._signer.cancel_all_orders.assert_called_once()
    call = adapter._signer.cancel_all_orders.call_args
    # Must pass time_in_force=IMMEDIATE (0) and a timestamp_ms
    assert call.kwargs["time_in_force"] == 0
    assert isinstance(call.kwargs["timestamp_ms"], int)
    assert call.kwargs["timestamp_ms"] > 0


@pytest.mark.asyncio
async def test_cancel_all_stops_sdk_error_raises():
    adapter = LighterAdapter.__new__(LighterAdapter)
    adapter._signer = MagicMock()
    adapter._signer.cancel_all_orders = AsyncMock(
        return_value=(None, None, "rate limited"),
    )
    meta = MagicMock()
    meta.market_index = 50
    adapter._market_meta_or_raise = MagicMock(return_value=meta)

    with pytest.raises(RuntimeError, match="rate limited"):
        await adapter.cancel_all_stops(symbol="ARB-USD")
