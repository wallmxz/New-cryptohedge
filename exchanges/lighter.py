"""Lighter v1 exchange adapter.

Implements the same `ExchangeAdapter` interface as DydxAdapter so the engine
doesn't need to know which exchange it's hedging on. The selection is via
`settings.active_exchange == "lighter"`.

Key design choices (per Phase A constraints):

1. **No slippage parameter.** When the engine calls `place_long_term_order()`
   with a `price` hint, this adapter IGNORES that price and instead reads the
   live top-of-book on the matching side (bid for sell, ask for buy), then
   posts an IOC LIMIT order at that exact price. If the book moves between
   our read and the matching, the order won't fill — we retry with a fresh
   bid/ask up to `_TAKER_RETRIES` times. Slippage is therefore never silently
   incurred.

2. **Zero fees** on standard accounts mean both maker and taker fills are 0
   cost — so the IOC-at-bid/ask pattern is economically equivalent to a
   market order, just with deterministic price and verifiable fill.

3. **Auth model**: Lighter API keys (separate from the eth wallet) sign every
   tx. The keys are generated via `scripts/lighter_setup.py` using the eth
   wallet, then their private form is stored in `.env`. This adapter uses
   `SignerClient` with the api_private_key (NOT the eth wallet key).

4. **Integer fixed-point**: Lighter uses int-encoded prices/sizes. Each market
   has its own `supported_price_decimals` and `supported_size_decimals` from
   `/orderBookDetails`. We cache that metadata at connect time and convert
   floats↔ints at the boundary.
"""
from __future__ import annotations
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from exchanges.base import ExchangeAdapter, Order, Fill, Position

logger = logging.getLogger(__name__)


# Number of times to re-read bid/ask and re-send the IOC if a fill doesn't
# materialize. With <100ms latency a few retries cover almost all book moves.
_TAKER_RETRIES = 5

# How long after sending an order to wait before considering it stale.
_FILL_VERIFY_TIMEOUT_S = 2.0


@dataclass
class _MarketMeta:
    """Per-market metadata cached at connect time."""
    symbol_user: str        # Engine-facing symbol, e.g. "ETH-USD"
    symbol_lighter: str     # Lighter base, e.g. "ETH"
    market_index: int       # Lighter's integer index for this market
    price_decimals: int     # Multiplier for price float→int
    size_decimals: int      # Multiplier for size float→int
    tick_size: float        # Smallest price increment (display units)
    step_size: float        # Smallest size increment (display units)
    min_base_amount: float  # Smallest order size (display units)
    min_quote_amount: float # Smallest notional (USD display units)

    @property
    def min_notional(self) -> float:
        return self.min_quote_amount


def _user_to_lighter_symbol(user_symbol: str) -> str:
    """Engine uses 'ETH-USD' style; Lighter uses 'ETH'. Strip the suffix."""
    return user_symbol.split("-")[0]


class LighterAdapter(ExchangeAdapter):
    """Lighter v1 exchange adapter.

    Construction (in app.py):

        adapter = LighterAdapter(
            url=settings.lighter_url,
            account_index=settings.lighter_account_index,
            api_private_key=settings.lighter_api_private_key,
            api_key_index=settings.lighter_api_key_index,
        )
        await adapter.connect()
    """

    name = "lighter"

    def __init__(
        self, *,
        url: str,
        account_index: int,
        api_private_key: str,
        api_key_index: int,
    ):
        self._url = url
        self._account_index = account_index
        self._api_private_key = api_private_key
        self._api_key_index = api_key_index

        # Filled at connect()
        self._signer = None  # SignerClient
        self._api_client = None  # ApiClient
        self._order_api = None  # OrderApi
        self._account_api = None  # AccountApi
        self._markets: dict[str, _MarketMeta] = {}  # user_symbol → meta

        # Engine callbacks
        self._book_callback: Callable[[dict], Awaitable[None]] | None = None
        self._fill_callback: Callable[[Fill], Awaitable[None]] | None = None

    # ──────────────────────────────────────────────────────────────────────
    # Connection lifecycle
    # ──────────────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Initialize the SignerClient and load market metadata."""
        # Lazy import: lighter SDK is a heavy optional dep
        from lighter import (
            ApiClient, Configuration, OrderApi, AccountApi, SignerClient,
        )

        cfg = Configuration(host=self._url)
        self._api_client = ApiClient(configuration=cfg)
        self._order_api = OrderApi(self._api_client)
        self._account_api = AccountApi(self._api_client)

        # SignerClient holds api_private_keys keyed by api_key_index
        self._signer = SignerClient(
            url=self._url,
            account_index=self._account_index,
            api_private_keys={self._api_key_index: self._api_private_key},
        )

        # Load market metadata
        await self._load_market_metadata()
        logger.info(
            f"LighterAdapter connected: {len(self._markets)} markets cached"
        )

    async def disconnect(self) -> None:
        if self._signer is not None:
            await self._signer.close()
        if self._api_client is not None:
            await self._api_client.close()

    async def _load_market_metadata(self) -> None:
        """Fetch all order books and build symbol→meta cache."""
        from lighter import OrderApi  # type-hint only

        # /orderBookDetails returns all markets at once when no market_id given
        resp = await self._order_api.order_book_details()
        details_list = getattr(resp, "order_book_details", None) or []

        for detail in details_list:
            sym_lighter = detail.symbol  # e.g. "ETH"
            sym_user = f"{sym_lighter}-USD"  # engine convention
            self._markets[sym_user] = _MarketMeta(
                symbol_user=sym_user,
                symbol_lighter=sym_lighter,
                market_index=detail.market_id,
                price_decimals=detail.supported_price_decimals,
                size_decimals=detail.supported_size_decimals,
                tick_size=10 ** -detail.supported_price_decimals,
                step_size=float(detail.min_base_amount),
                min_base_amount=float(detail.min_base_amount),
                min_quote_amount=float(detail.min_quote_amount),
            )

    # ──────────────────────────────────────────────────────────────────────
    # Subscriptions (simple polling stubs for Phase A; WS upgrade later)
    # ──────────────────────────────────────────────────────────────────────

    async def subscribe_orderbook(
        self, symbol: str, callback: Callable[[dict], Awaitable[None]],
    ) -> None:
        # Phase A: engine reads top-of-book on demand via _read_top_of_book.
        # WS feed is a follow-up if we need lower latency than 1Hz polling.
        self._book_callback = callback

    async def subscribe_fills(
        self, symbol: str, callback: Callable[[Fill], Awaitable[None]],
    ) -> None:
        # Phase A: fills are confirmed inline by place_long_term_order's
        # response and synthesized into the engine's Fill record. WS upgrade
        # for async fill notifications is a follow-up.
        self._fill_callback = callback

    # ──────────────────────────────────────────────────────────────────────
    # Order placement (the constraint-critical path)
    # ──────────────────────────────────────────────────────────────────────

    async def place_long_term_order(
        self, *,
        symbol: str, side: str, size: float, price: float,
        cloid_int: int, ttl_seconds: int = 86400,
    ) -> Order:
        """Place a "taker" order via IOC LIMIT at the live bid/ask.

        The `price` argument is the engine's slippage-adjusted hint (from the
        legacy DydxAdapter pattern). We **ignore** it and read the live
        top-of-book ourselves. This eliminates silent slippage: each fill
        prints at exactly the bid (sell) or ask (buy) we observed.

        On no-fill (book moved between read and match), retry up to
        `_TAKER_RETRIES` times. After exhausting retries, raise — the engine
        treats this as the order failing, which is correct: no fill, no
        position change, no silent loss.
        """
        meta = self._market_meta_or_raise(symbol)
        is_ask = (side == "sell")
        size_int = self._size_to_int(size, meta)
        if size_int <= 0:
            raise ValueError(
                f"Size {size} below market step {meta.step_size}",
            )

        last_error: str | None = None
        for attempt in range(_TAKER_RETRIES):
            # Read top-of-book on the side we're hitting:
            #   sell → hit bid (someone willing to buy from us)
            #   buy  → hit ask (someone willing to sell to us)
            # SignerClient.get_best_price uses is_ask=True for ASK side of
            # book; we hit the OPPOSITE side as taker.
            best_int = await self._signer.get_best_price(
                meta.market_index, is_ask=(not is_ask),
            )
            if not best_int or best_int <= 0:
                last_error = "empty top-of-book"
                await asyncio.sleep(0.1)
                continue

            # IOC LIMIT at exactly the bid/ask we observed.
            from lighter import SignerClient
            tx, resp, err = await self._signer.create_order(
                market_index=meta.market_index,
                client_order_index=int(cloid_int) & 0xFFFFFFFF,
                base_amount=size_int,
                price=best_int,
                is_ask=is_ask,
                order_type=SignerClient.ORDER_TYPE_LIMIT,
                time_in_force=SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
            )
            if err is not None:
                # Exchange-side error (insufficient margin, market halted, etc.)
                last_error = err
                logger.warning(
                    f"Lighter create_order rejected: {err} "
                    f"(attempt {attempt+1}/{_TAKER_RETRIES})"
                )
                await asyncio.sleep(0.1)
                continue

            # Success path: order accepted by the exchange. IOC means it
            # either filled or was cancelled. Verify which by checking the
            # order's terminal state.
            fill_size, fill_price = await self._verify_fill(
                meta=meta, cloid_int=cloid_int, expected_size=size,
            )
            if fill_size > 0:
                # Notify engine via fill callback (synthetic Fill record).
                if self._fill_callback is not None:
                    fill = Fill(
                        fill_id=str(int(time.time() * 1000)),
                        order_id=str(cloid_int),
                        symbol=symbol, side=side,
                        size=fill_size, price=fill_price,
                        fee=0.0, fee_currency="USDC",  # Lighter zero fee
                        liquidity="taker",
                        realized_pnl=0.0,
                        timestamp=time.time(),
                    )
                    await self._fill_callback(fill)
                return Order(
                    order_id=str(cloid_int), symbol=symbol, side=side,
                    size=fill_size,
                    price=self._int_to_price(best_int, meta),
                    status="filled" if fill_size >= size else "partial",
                )
            # IOC was accepted but didn't fill — book moved. Retry.
            logger.debug(
                f"Lighter IOC accepted but no fill (book moved); "
                f"retry {attempt+1}/{_TAKER_RETRIES}"
            )

        raise RuntimeError(
            f"LighterAdapter: failed to fill {side} {size} {symbol} after "
            f"{_TAKER_RETRIES} retries (last_error: {last_error})"
        )

    async def cancel_long_term_order(
        self, *, symbol: str, cloid_int: int,
    ) -> None:
        """Best-effort cancel. With IOC orders this is mostly a no-op since
        IOC auto-cancels unfilled, but it's part of the interface contract."""
        meta = self._market_meta_or_raise(symbol)
        try:
            await self._signer.cancel_order(
                market_index=meta.market_index,
                order_index=int(cloid_int) & 0xFFFFFFFF,
            )
        except Exception as e:
            logger.debug(f"cancel_long_term_order best-effort failed: {e}")

    async def batch_place(self, orders: list[dict]) -> list[Order]:
        # Sequential for now; Lighter has create_grouped_orders for batch
        # but keeping it simple in Phase A.
        out = []
        for spec in orders:
            out.append(await self.place_long_term_order(**spec))
        return out

    async def batch_cancel(self, items: list[dict]) -> int:
        n = 0
        for spec in items:
            try:
                await self.cancel_long_term_order(**spec)
                n += 1
            except Exception:
                pass
        return n

    # ──────────────────────────────────────────────────────────────────────
    # Account state
    # ──────────────────────────────────────────────────────────────────────

    async def get_position(self, symbol: str) -> Position | None:
        meta = self._market_meta_or_raise(symbol)
        try:
            resp = await self._account_api.account(
                by="index", value=str(self._account_index),
            )
        except Exception as e:
            logger.warning(f"get_position failed: {e}")
            return None
        accounts = getattr(resp, "accounts", None) or []
        if not accounts:
            return None
        acct = accounts[0]
        positions = getattr(acct, "positions", None) or []
        for pos in positions:
            if pos.market_id != meta.market_index:
                continue
            sign = float(pos.sign) if hasattr(pos, "sign") else 1.0
            size = float(pos.position) * sign  # negative=short, positive=long
            if abs(size) < 1e-12:
                return None
            return Position(
                symbol=symbol,
                side="short" if size < 0 else "long",
                size=abs(size),
                entry_price=float(pos.avg_entry_price),
                unrealized_pnl=float(pos.unrealized_pnl),
            )
        return None

    async def get_oracle_prices(self, symbols: list[str]) -> dict[str, float]:
        """Returns last trade price per symbol. We use last_trade_price as
        the oracle reference since /orderBookDetails exposes it consistently.
        For the engine's purposes (scaling notionals) this is close enough to
        the mark price."""
        out: dict[str, float] = {}
        for s in symbols:
            meta = self._markets.get(s)
            if meta is None:
                out[s] = 0.0
                continue
            try:
                resp = await self._order_api.order_book_details(
                    market_id=meta.market_index,
                )
                details = getattr(resp, "order_book_details", None) or []
                if details:
                    out[s] = float(details[0].last_trade_price)
                else:
                    out[s] = 0.0
            except Exception as e:
                logger.debug(f"oracle price for {s} failed: {e}")
                out[s] = 0.0
        return out

    async def get_collateral(self) -> float:
        try:
            resp = await self._account_api.account(
                by="index", value=str(self._account_index),
            )
        except Exception as e:
            logger.warning(f"get_collateral failed: {e}")
            return 0.0
        accounts = getattr(resp, "accounts", None) or []
        if not accounts:
            return 0.0
        return float(accounts[0].available_balance)

    async def get_fills(self, symbol: str, since: float | None = None) -> list[Fill]:
        # Phase A: engine relies on the inline fill callback from
        # place_long_term_order. Historical fills via /trades is a follow-up.
        return []

    async def get_open_orders_cloids(self, symbol: str) -> list[str]:
        meta = self._market_meta_or_raise(symbol)
        try:
            resp = await self._order_api.account_active_orders(
                account_index=self._account_index,
                market_id=meta.market_index,
            )
        except Exception:
            return []
        orders = getattr(resp, "orders", None) or []
        return [str(o.client_order_index) for o in orders]

    # ──────────────────────────────────────────────────────────────────────
    # Market meta accessors (engine-facing sync API)
    # ──────────────────────────────────────────────────────────────────────

    async def get_market_meta(self, symbol: str) -> _MarketMeta:
        return self._market_meta_or_raise(symbol)

    def get_tick_size(self, symbol: str) -> float:
        return self._market_meta_or_raise(symbol).tick_size

    def get_min_notional(self, symbol: str) -> float:
        return self._market_meta_or_raise(symbol).min_notional

    # ──────────────────────────────────────────────────────────────────────
    # Internal helpers
    # ──────────────────────────────────────────────────────────────────────

    def _market_meta_or_raise(self, symbol: str) -> _MarketMeta:
        m = self._markets.get(symbol)
        if m is None:
            raise KeyError(
                f"Symbol {symbol!r} not in Lighter markets cache. "
                f"Known: {sorted(self._markets.keys())[:10]}…"
            )
        return m

    def _size_to_int(self, size: float, meta: _MarketMeta) -> int:
        return int(round(size * 10 ** meta.size_decimals))

    def _int_to_price(self, price_int: int, meta: _MarketMeta) -> float:
        return price_int / 10 ** meta.price_decimals

    def _int_to_size(self, size_int: int, meta: _MarketMeta) -> float:
        return size_int / 10 ** meta.size_decimals

    async def _verify_fill(
        self, *, meta: _MarketMeta, cloid_int: int, expected_size: float,
    ) -> tuple[float, float]:
        """Poll the order's terminal state to determine fill size/price.

        IOC orders settle within one block — typically <1s on Lighter. We
        poll the inactive-orders endpoint (which lists terminal-state orders)
        for our cloid. Returns (filled_size, fill_price). If no record is
        found within the timeout, returns (0, 0) — caller will retry.
        """
        deadline = time.time() + _FILL_VERIFY_TIMEOUT_S
        cloid_lookup = int(cloid_int) & 0xFFFFFFFF
        while time.time() < deadline:
            try:
                resp = await self._order_api.account_inactive_orders(
                    account_index=self._account_index,
                    limit=20,
                    market_id=meta.market_index,
                )
                orders = getattr(resp, "orders", None) or []
            except Exception as e:
                logger.debug(f"verify_fill: inactive query failed: {e}")
                orders = []
            for o in orders:
                if int(o.client_order_index) != cloid_lookup:
                    continue
                # Found our terminal-state order. filled_base is in display
                # units when the SDK serializes; if it's int we'd convert.
                filled_size = float(getattr(o, "filled_base_amount", 0))
                fill_price = float(getattr(o, "average_filled_price", 0))
                return filled_size, fill_price
            await asyncio.sleep(0.1)
        return 0.0, 0.0
