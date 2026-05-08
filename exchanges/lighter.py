"""Lighter v1 exchange adapter.

Implements the same `ExchangeAdapter` interface as DydxAdapter so the engine
doesn't need to know which exchange it's hedging on. The selection is via
`settings.active_exchange == "lighter"`.

Key design choices:

1. **WebSocket-first state.** Account positions, collateral, and order book
   top-of-book are streamed via Lighter's `/stream` WS endpoint (channels
   `account_all/{id}` and `order_book/{market_id}`). The adapter holds the
   latest snapshot in memory. `get_position()`, `get_collateral()`,
   `get_oracle_prices()`, and the internal best-price lookup inside
   `place_long_term_order()` all read from cache — zero HTTP calls per
   engine iteration, so the WAF on Lighter's CloudFront edge never sees
   sustained polling. HTTP is kept only for one-shot operations: signer
   nonce probe, market metadata at connect, and the actual `create_order`
   submission.

2. **No slippage parameter.** When the engine calls `place_long_term_order()`
   with a `price` hint, this adapter IGNORES that price and uses the
   cached live top-of-book on the matching side (bid for sell, ask for
   buy). We post the IOC LIMIT slightly through the cached level so a
   tick-level book move doesn't auto-cancel the order.

3. **Zero fees** on standard accounts mean both maker and taker fills are 0
   cost — so the IOC-at-bid/ask pattern is economically equivalent to a
   market order, just with deterministic price and verifiable fill.

4. **Auth model**: Lighter API keys (separate from the eth wallet) sign every
   tx. The keys are generated via `scripts/lighter_setup.py` using the eth
   wallet, then their private form is stored in `.env`. This adapter uses
   `SignerClient` with the api_private_key (NOT the eth wallet key).

5. **Integer fixed-point**: Lighter uses int-encoded prices/sizes. Each market
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

        # Single-flight lock around `create_order`. Lighter's nonce_manager
        # in API mode still races when two coroutines call `next_nonce()`
        # concurrently — both can read the same server nonce before either
        # commits an order, the second sign produces an invalid signature
        # (server code 21120). Lighter docs also require ≥350ms between
        # orders on the same api_key. The lock + post-order sleep below
        # gives both: serialized signing AND the cooldown.
        self._order_lock = asyncio.Lock()
        self._last_order_at: float = 0.0
        self._MIN_GAP_S = 0.6

        # WebSocket state cache. Populated by the background WS pump task
        # subscribed to `order_book/{market_id}` and `account_all/{id}`.
        # The pump task runs forever (with exponential backoff reconnect),
        # writing into these dicts. Reader methods (get_position,
        # get_collateral, get_oracle_prices) read from here — no HTTP.
        # Keys for `_ws_book_top` are market_index (int).
        # `_ws_collateral` is the available_balance float.
        self._ws_book_top: dict[int, dict] = {}
        # Per the position-truth redesign (2026-05-07): split observed
        # state into the magnitude (engine-facing, unsigned) and the
        # metadata (diagnostic-facing). The engine drives drift off
        # `_observed_short_size` only; `_observed_position_meta` keeps
        # entry price / unrealized PnL / sign for `get_position`.
        self._observed_short_size: dict[int, float] = {}
        self._observed_position_meta: dict[int, dict] = {}
        self._ws_collateral: float | None = None
        # Locally-stamped expected magnitude per market_id. Written by
        # `place_long_term_order` when `create_order` returns
        # err=None (server-accept), regardless of `_verify_fill`.
        # Reset by `_reconciler_loop` when the WS-observed value catches
        # up, OR when an HTTP authoritative query confirms the truth.
        # See spec/2026-05-07-position-truth-redesign-design.md §
        # "Reconciliation logic" for the stamping/reset rules.
        self._expected_short_size: dict[int, float] = {}
        # Monotonic timestamp of the latest successful create_order on
        # this market_id. Reconciler measures HTTP-query timeout from
        # this; rewritten on every fire so a chain of fires within
        # 30 s waits for the latest fire to age out.
        self._last_fire_at: dict[int, float] = {}
        # Markets to subscribe to via WS. Subset of self._markets keys
        # populated by `register_active_symbols`. Lighter rejects mass
        # subscribes (>~10 at once) with "Too Many Inflight Messages"
        # (code 30010); we only watch the markets the engine actually
        # trades on this session.
        self._subscribed_markets: set[int] = set()
        # Reference to the live WsClient instance (when connected) — used
        # by `register_active_symbols` to close + reconnect with a new
        # subscription set. None when no WS is up.
        self._ws_active_client = None
        # Set on first message of any kind from the WS — readers wait on
        # this in `connect()` so the first iteration of the engine sees
        # populated state instead of empty cache.
        self._ws_first_snapshot = asyncio.Event()
        self._ws_task: asyncio.Task | None = None
        self._reconcile_task: asyncio.Task | None = None
        self._ws_closing = False

        # Funding poller (Lighter-specific): periodic HTTP poll of
        # AccountApi.position_funding emits each entry to the callback
        # registered via subscribe_funding. Engine uses this to populate
        # funding_paid_token0/1 on the active operation.
        self._funding_callback: Callable[..., Awaitable[None]] | None = None
        self._funding_task: asyncio.Task | None = None

    # ──────────────────────────────────────────────────────────────────────
    # Connection lifecycle
    # ──────────────────────────────────────────────────────────────────────

    async def connect(self) -> None:
        """Initialize the SignerClient and load market metadata."""
        # Lazy import: lighter SDK is a heavy optional dep
        from lighter import (
            ApiClient, Configuration, OrderApi, AccountApi, SignerClient,
        )
        from lighter import nonce_manager

        cfg = Configuration(host=self._url)
        self._api_client = ApiClient(configuration=cfg)
        self._order_api = OrderApi(self._api_client)
        self._account_api = AccountApi(self._api_client)

        # SignerClient holds api_private_keys keyed by api_key_index.
        # `nonce_management_type=API` makes the manager refresh the nonce
        # from the server before every order. The default OPTIMISTIC mode
        # caches locally and increments — which causes "invalid signature"
        # (server code 21120) when the local cache drifts from the server
        # (e.g. after a failed tx, a restart, or a parallel order). The
        # API mode adds ~one extra HTTP call per order in exchange for
        # always-correct nonces. Bootstrap fires few orders so the latency
        # cost is negligible.
        self._signer = SignerClient(
            url=self._url,
            account_index=self._account_index,
            api_private_keys={self._api_key_index: self._api_private_key},
            nonce_management_type=nonce_manager.NonceManagerType.API,
        )

        # Pre-flight: verify the configured api_private_key matches the
        # public key the exchange has registered for this account+slot.
        # Without this, the first order would fail with "code=21120
        # invalid signature" — a confusing error that takes several
        # rounds of debugging to trace back to a wrong .env. CheckClient
        # is a cheap (~50ms) RPC and surfaces this immediately.
        try:
            from lighter.signer_client import decode_and_free
            err_ptr = self._signer.signer.CheckClient(
                self._api_key_index, self._account_index,
            )
            err = decode_and_free(err_ptr)
            if err:
                # Don't raise — let the system come up so the operator can
                # fix the key without a crash loop. Mark the connection
                # state and surface the error in logs prominently.
                logger.error(
                    f"Lighter CheckClient FAILED — the LIGHTER_API_PRIVATE_KEY "
                    f"in your .env does NOT match the public key registered "
                    f"on the server for account {self._account_index} "
                    f"slot {self._api_key_index}. Detail: {err}"
                )
                self._key_validated = False
            else:
                logger.info("Lighter CheckClient: OK (key matches server)")
                self._key_validated = True
        except Exception as e:
            logger.warning(f"CheckClient probe failed (non-fatal): {e}")
            self._key_validated = False

        # Load market metadata
        await self._load_market_metadata()
        logger.info(
            f"LighterAdapter connected: {len(self._markets)} markets cached"
        )

        # Start the WS pump in the background. Don't await its first
        # snapshot here — at adapter-connect time no markets are
        # registered yet (engine calls `register_active_symbols` later
        # via `_refresh_vault_readers`). The pump idles until that happens,
        # then opens its WS subscription and starts populating the cache.
        # Until then, get_position/get_collateral return None/0 and the
        # engine treats those as "unknown" rather than active state.
        self._ws_closing = False
        self._ws_task = asyncio.create_task(self._run_ws_pump())
        # Background reconciler — resolves persistent observed/expected
        # divergence via HTTP authoritative query. See spec/2026-05-07-
        # position-truth-redesign-design.md § Reconciliation logic.
        self._reconcile_task = asyncio.create_task(self._reconciler_loop())

    async def disconnect(self) -> None:
        self._ws_closing = True
        if self._ws_task is not None and not self._ws_task.done():
            self._ws_task.cancel()
            try:
                await self._ws_task
            except (asyncio.CancelledError, Exception):
                pass
        # Cancel the reconciler task too — symmetric with WS.
        rec_task = getattr(self, "_reconcile_task", None)
        if rec_task is not None and not rec_task.done():
            rec_task.cancel()
            try:
                await rec_task
            except (asyncio.CancelledError, Exception):
                pass
        if self._signer is not None:
            await self._signer.close()
        if self._api_client is not None:
            await self._api_client.close()

    # ──────────────────────────────────────────────────────────────────────
    # WebSocket pump + handlers
    # ──────────────────────────────────────────────────────────────────────

    async def _run_ws_pump(self) -> None:
        """Persistent WS reader with exponential-backoff reconnect.

        Lighter's `WsClient.run_async()` blocks reading messages until the
        connection drops, then raises. We wrap it so a network blip just
        triggers a reconnect instead of bringing down the adapter.
        """
        backoff = 1.0
        while not self._ws_closing:
            try:
                await self._connect_and_pump_ws()
                # Clean exit (server closed): reset backoff so a transient
                # disconnect doesn't penalize the next reconnect.
                backoff = 1.0
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(
                    f"Lighter WS dropped ({type(e).__name__}: {e}); "
                    f"reconnect in {backoff:.0f}s"
                )
                try:
                    await asyncio.sleep(backoff)
                except asyncio.CancelledError:
                    return
                backoff = min(backoff * 2, 30.0)

    async def _connect_and_pump_ws(self) -> None:
        """Single WS connection; subscribes once, then pumps until close.

        We subscribe ONLY to markets in `self._subscribed_markets` (a small
        set populated by the engine via `register_active_symbols`). Lighter
        rejects too-many-subscribes with code 30010 ("Too Many Inflight
        Messages!") if we try to subscribe to all 170+ markets at once,
        which is what an unfiltered list would do.
        """
        from lighter import WsClient
        # The SDK's host comes from default Configuration unless specified.
        # Strip the scheme from our configured URL so it matches what the
        # WsClient expects (it prepends `wss://`).
        host = self._url.replace("https://", "").replace("http://", "")
        market_ids = sorted(self._subscribed_markets)
        if not market_ids:
            # Nothing to watch yet — wait briefly for the engine to
            # register a symbol, then retry.
            await asyncio.sleep(2.0)
            return
        ws = WsClient(
            host=host,
            order_book_ids=market_ids,
            account_ids=[self._account_index],
            on_order_book_update=self._on_book_update,
            on_account_update=self._on_account_update,
        )
        self._ws_active_client = ws
        try:
            await ws.run_async()
        finally:
            self._ws_active_client = None

    def register_active_symbols(self, symbols: list[str]) -> None:
        """Tell the WS pump which markets to subscribe to. Called by the
        engine when a pair is selected (see pair_factory). When the set
        of subscribed markets changes, we force-close the current WS so
        the pump loop reconnects with the new subscription list."""
        new_ids = set()
        for s in symbols:
            meta = self._markets.get(s)
            if meta is not None:
                new_ids.add(meta.market_index)
        if new_ids != self._subscribed_markets:
            logger.info(
                f"Lighter WS subscriptions: {sorted(self._subscribed_markets)} "
                f"-> {sorted(new_ids)}"
            )
            self._subscribed_markets = new_ids
            # If a WS is currently connected with the OLD subscription
            # list, close it so the pump loop reconnects with the new
            # list. The close runs as a fire-and-forget task to keep
            # this method synchronous (matches the engine's call site).
            ws = getattr(self, "_ws_active_client", None)
            if ws is not None and getattr(ws, "ws", None) is not None:
                try:
                    asyncio.create_task(ws.ws.close())
                except Exception:
                    pass

    def _on_book_update(self, market_id, state) -> None:
        """Callback from WsClient on `subscribed/order_book` and
        `update/order_book`. `state` is a dict like
        `{"asks": [{"price": "0.12674", "size": "..."}], "bids": [...]}`.
        We extract the best (lowest ask, highest bid) into our cache.
        """
        try:
            mid = int(market_id)
            asks = state.get("asks") or []
            bids = state.get("bids") or []
            best_ask = None
            if asks:
                # Sort by float(price) ascending; first is lowest ask.
                best_ask = min(
                    (float(a.get("price", 0)) for a in asks if float(a.get("price", 0)) > 0),
                    default=None,
                )
            best_bid = None
            if bids:
                best_bid = max(
                    (float(b.get("price", 0)) for b in bids if float(b.get("price", 0)) > 0),
                    default=None,
                )
            self._ws_book_top[mid] = {
                "best_bid": best_bid,
                "best_ask": best_ask,
                "ts": time.time(),
            }
            self._ws_first_snapshot.set()
        except Exception as e:
            logger.warning(f"WS book update parse failed: {e}")

    def _on_account_update(self, account_id, state) -> None:
        """Callback from WsClient on `subscribed/account_all` and
        `update/account_all`. Per Lighter's official WS spec
        (https://apidocs.lighter.xyz/docs/websocket-reference), the
        message contains these TOP-LEVEL fields on every snapshot:

          - `account` (int) — the account_id (NOT a nested object!)
          - `available_balance` (str decimal)
          - `collateral` (str decimal)
          - `positions` (dict keyed by market_id string → position obj)
          - `assets`, `funding_histories`, ... (other dicts)

        `update/account_all` is a FULL snapshot — when a position closes
        it disappears from the dict (no size=0 sentinel). So we replace
        the whole cache wholesale on every message.

        Earlier this parser tried to drill into `state["account"]` as a
        dict and treat `positions` as a list — both wrong. The bug
        produced silent parse failures, leaving the position cache
        empty, which made the engine think "no position open" right
        after bootstrap → fire ANOTHER hedge → over-hedge stack on
        every iter. Critical regression covered by tests below.
        """
        try:
            # Collateral resolution. Per the official spec, top-level
            # `available_balance` should hold the free USDC balance, but
            # empirically (probed against a live cross-margin account)
            # that field comes back null and the real margin balance
            # lives inside `assets[<asset_id>].margin_balance`. Lighter
            # USDC = asset_id "3" on mainnet. We prefer top-level if
            # present, fall back to the assets dict, then total across
            # all assets if multi-asset margin is enabled.
            avail_val: float | None = None
            top_avail = state.get("available_balance")
            if top_avail not in (None, ""):
                try:
                    avail_val = float(top_avail)
                except (TypeError, ValueError):
                    pass
            if avail_val is None:
                assets = state.get("assets") or {}
                if isinstance(assets, dict):
                    # Sum all assets' margin_balance (handles single-
                    # asset USDC and multi-asset margin uniformly).
                    total = 0.0
                    seen = False
                    for v in assets.values():
                        if not isinstance(v, dict):
                            continue
                        mb = v.get("margin_balance") or v.get("balance")
                        if mb in (None, ""):
                            continue
                        try:
                            total += float(mb)
                            seen = True
                        except (TypeError, ValueError):
                            continue
                    if seen:
                        avail_val = total
            if avail_val is not None:
                self._ws_collateral = avail_val

            positions = state.get("positions") or {}
            new_short_size: dict[int, float] = {}
            new_position_meta: dict[int, dict] = {}
            # Lighter encodes positions as `{"<market_id_str>": {…}, …}`.
            if isinstance(positions, dict):
                for key, pos in positions.items():
                    if not isinstance(pos, dict):
                        continue
                    # `market_id` lives inside the position object too;
                    # prefer it but fall back to the dict key.
                    try:
                        mid = int(pos.get("market_id", key))
                    except (TypeError, ValueError):
                        continue
                    try:
                        size = float(pos.get("position", 0) or 0)
                    except (TypeError, ValueError):
                        size = 0.0
                    if size <= 0:
                        # Closed position — skip both dicts.
                        continue
                    sign = int(pos.get("sign", 1))
                    # `_observed_short_size` is the engine-facing magnitude.
                    # Only count short positions (sign=-1); longs get 0 so
                    # the engine's drift math doesn't try to "hedge" them.
                    new_short_size[mid] = size if sign == -1 else 0.0
                    new_position_meta[mid] = {
                        "sign": sign,
                        "position": size,
                        "avg_entry_price": float(
                            pos.get("avg_entry_price", 0) or 0
                        ),
                        "unrealized_pnl": float(
                            pos.get("unrealized_pnl", 0) or 0
                        ),
                    }
            # Defensive merge instead of wholesale replace.
            #
            # `update/account_all` is documented as a FULL snapshot, so
            # missing-from-message → "position closed". In practice we
            # have observed transient snapshots that drop a still-open
            # position mid-iteration (probably a race in Lighter's match
            # engine emitting an intermediate state during a fill). When
            # that happens and we wholesale-replace, the engine reads
            # current=0, computes drift=full_target, and OPENS the
            # entire hedge a second time — observed in production
            # 2026-05-08 (incidents on op #28).
            #
            # Fix: keep mids that were previously non-zero but missing
            # from this snapshot. The reconciler runs every 5 s and will
            # query HTTP authoritative within RECONCILE_TIMEOUT_S to
            # confirm a genuine close, so this only DEFERS clearing —
            # it doesn't permanently mask a closed position.
            for mid, prev_size in self._observed_short_size.items():
                if mid in new_short_size:
                    continue  # explicit value in new snapshot — accept it
                if prev_size > 0:
                    # Held back. Reconciler will verify via HTTP.
                    new_short_size[mid] = prev_size
                    if mid in self._observed_position_meta:
                        new_position_meta[mid] = self._observed_position_meta[mid]
                    logger.warning(
                        f"WS dropped mid={mid} (was {prev_size}); preserving "
                        f"until reconciler confirms via HTTP."
                    )
            self._observed_short_size = new_short_size
            self._observed_position_meta = new_position_meta
            self._ws_first_snapshot.set()
        except Exception as e:
            logger.warning(f"WS account update parse failed: {e}")

    # Reconciler tunables. WS account_all pushes typically land in <1 s
    # under normal Lighter load; we treat 10 s as "definitely should
    # have delivered, if we're still seeing divergence the IOC must
    # have failed to fill — query HTTP authoritative". Tightened from
    # the original 30 s after observing that real-world WS lag never
    # exceeds 5 s. Faster recovery from no-fill scenarios at the cost
    # of one extra HTTP call per genuinely-failed fire (the over-hedge
    # protection in get_effective_position is independent of this
    # value). Engine-level over-hedge race is structurally impossible
    # regardless of timeout — see 2026-05-07 position-truth redesign.
    RECONCILE_TIMEOUT_S = 10.0

    def _step_size_for_mid(self, mid: int) -> float:
        """Tolerance for treating observed as matching expected — one
        size tick. Pulled from the per-market metadata cached at connect."""
        for meta in self._markets.values():
            if meta.market_index == mid:
                return meta.step_size
        return 1e-9  # unknown market — strict equality

    async def _reconcile_once(self) -> None:
        """One pass over `_expected_short_size`. For each entry:
          1. If observed already matches expected (within step size),
             pin expected to observed and clear local credit.
          2. Else if elapsed since last fire > RECONCILE_TIMEOUT_S,
             query HTTP authoritative and pin both layers to the
             returned truth — UNLESS a concurrent fire stamped
             `_last_fire_at` mid-await, in which case abort to avoid
             overwriting fresher state with stale truth.
        """
        # Snapshot the current state so per-symbol decisions reference
        # a consistent picture, not values that mutated during awaits.
        snapshot = {
            mid: (
                self._expected_short_size.get(mid, 0.0),
                self._last_fire_at.get(mid, 0.0),
            )
            for mid in list(self._expected_short_size.keys())
        }
        for mid, (expected_at_scan, last_fire_at_scan) in snapshot.items():
            observed = self._observed_short_size.get(mid, 0.0)
            tol = self._step_size_for_mid(mid)
            # Catch-up case: WS already shows expected. Pin and move on.
            if abs(expected_at_scan - observed) <= tol:
                # Only commit if no new fire happened — otherwise the
                # newer expected is more current than `observed`.
                if self._last_fire_at.get(mid, 0.0) == last_fire_at_scan:
                    self._expected_short_size[mid] = observed
                continue
            # Timeout case: if not enough time since last fire, wait.
            elapsed = time.monotonic() - last_fire_at_scan
            if elapsed <= self.RECONCILE_TIMEOUT_S:
                continue
            # Authoritative query.
            truth = await self._fetch_short_size_via_http(mid)
            if truth is None:
                continue  # HTTP failed — retry next scan
            # Race guard: did a fire happen during our await?
            if self._last_fire_at.get(mid, 0.0) != last_fire_at_scan:
                logger.debug(
                    f"Reconcile[{mid}] aborted: new fire stamped during "
                    f"HTTP query — next scan will re-evaluate."
                )
                continue
            self._observed_short_size[mid] = truth
            self._expected_short_size[mid] = truth
            logger.info(
                f"Reconciled short_size[{mid}] via HTTP: "
                f"observed_was={observed}, expected_was={expected_at_scan}, "
                f"truth={truth}"
            )

    async def _reconciler_loop(self) -> None:
        """Background task that calls `_reconcile_once` every 5 s for
        the lifetime of the adapter. Started in `connect()`, cancelled
        in `disconnect()`. Catches per-iteration exceptions so a
        transient HTTP error doesn't crash the loop — next scan retries.
        """
        while not self._ws_closing:
            try:
                await self._reconcile_once()
            except asyncio.CancelledError:
                return
            except Exception as e:
                logger.warning(
                    f"Reconciler iteration failed: {type(e).__name__}: {e}"
                )
            try:
                await asyncio.sleep(5.0)
            except asyncio.CancelledError:
                return

    async def _load_market_metadata(self) -> None:
        """Fetch all order books and build symbol→meta cache."""
        from lighter import OrderApi  # type-hint only

        # /orderBookDetails returns all markets at once when no market_id given
        resp = await self._order_api.order_book_details()
        details_list = getattr(resp, "order_book_details", None) or []

        for detail in details_list:
            sym_lighter = detail.symbol  # e.g. "ETH"
            sym_user = f"{sym_lighter}-USD"  # engine convention
            # step_size is the smallest representable quantity given size_decimals,
            # NOT the API's min_base_amount field. Empirically Lighter's matching
            # engine accepts orders smaller than min_base_amount as long as they
            # round to a valid integer at supported_size_decimals (e.g., ARB
            # accepts 0.4 even though min_base_amount=20.0).
            self._markets[sym_user] = _MarketMeta(
                symbol_user=sym_user,
                symbol_lighter=sym_lighter,
                market_index=detail.market_id,
                price_decimals=detail.supported_price_decimals,
                size_decimals=detail.supported_size_decimals,
                tick_size=10 ** -detail.supported_price_decimals,
                step_size=10 ** -detail.supported_size_decimals,
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

    def subscribe_funding(
        self, callback: Callable[..., Awaitable[None]],
    ) -> None:
        """Register a callback invoked per funding payment. Engine uses
        this to populate funding_paid_token0/1 on the active operation.
        The poller (started in connect) drives invocations."""
        self._funding_callback = callback

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
        # Serialize ALL order placements on this adapter — Lighter's nonce
        # manager races when two coroutines hit `next_nonce()` in parallel
        # (e.g. dual-leg shorts via asyncio.gather), and the loser gets a
        # stale nonce → server returns code=21120 invalid signature. Plus
        # Lighter docs require ≥350ms between orders on the same api_key.
        # The lock + min-gap below makes both guarantees structural.
        async with self._order_lock:
            now = time.monotonic()
            gap = now - self._last_order_at
            if gap < self._MIN_GAP_S:
                await asyncio.sleep(self._MIN_GAP_S - gap)
            try:
                return await self._place_long_term_order_unlocked(
                    symbol=symbol, side=side, size=size, price=price,
                    cloid_int=cloid_int, ttl_seconds=ttl_seconds,
                )
            finally:
                self._last_order_at = time.monotonic()

    async def _place_long_term_order_unlocked(
        self, *,
        symbol: str, side: str, size: float, price: float,
        cloid_int: int, ttl_seconds: int = 86400,
    ) -> Order:
        meta = self._market_meta_or_raise(symbol)
        is_ask = (side == "sell")
        size_int = self._size_to_int(size, meta)
        if size_int <= 0:
            raise ValueError(
                f"Size {size} below market step {meta.step_size}",
            )

        # Snapshot pre-trade position so we can detect fills that the
        # `_verify_fill` cloid lookup misses (account_inactive_orders is
        # eventually-consistent — under load it can take >2s to surface
        # a filled order). Without this guard, returning `cancelled` to
        # the engine when the position actually grew triggers a SECOND
        # order on the next iteration — exactly the retry loop that
        # produced the 0.9 ETH over-hedge in this session's postmortem.
        try:
            pre_pos = await self.get_position(symbol)
            pre_size = abs(pre_pos.size) if pre_pos else 0.0
            pre_side = pre_pos.side if pre_pos else None
        except Exception:
            pre_size = 0.0
            pre_side = None

        last_error: str | None = None
        for attempt in range(_TAKER_RETRIES):
            # Read top-of-book from the WS cache on the side we're hitting:
            #   sell → hit bid (someone willing to buy from us)
            #   buy  → hit ask (someone willing to sell to us)
            # The cache is updated by the WS pump from `update/order_book`
            # messages; lag is typically <50ms. No HTTP here — that's
            # what was triggering Lighter's CloudFront WAF on sustained
            # 1Hz polling.
            top = self._ws_book_top.get(meta.market_index)
            if not top:
                last_error = "WS book cache empty for this market"
                await asyncio.sleep(0.2)
                continue
            best_float = top.get("best_bid") if is_ask else top.get("best_ask")
            if not best_float or best_float <= 0:
                last_error = "empty top-of-book in WS cache"
                await asyncio.sleep(0.2)
                continue
            # Convert display-units price → integer ticks for the SDK.
            best_int = int(round(best_float * (10 ** meta.price_decimals)))
            if best_int <= 0:
                last_error = "best price rounds to zero ticks"
                await asyncio.sleep(0.2)
                continue

            # No price buffer — user requirement: IOC LIMIT exactly at the
            # cached bid (sell) or ask (buy). If the book moves a tick
            # between our send and Lighter's matching engine processing
            # the request, the IOC will auto-cancel and the engine's
            # per-leg cooldown (30s) decides when to re-fire. Trade-off:
            # on fast-moving markets occasional non-fills, but every fill
            # prints at the exact level we observed — zero slippage by
            # construction.
            limit_int = best_int

            # IOC LIMIT at the buffered bid/ask.
            # `order_expiry=DEFAULT_IOC_EXPIRY` (=0) is REQUIRED when
            # `time_in_force=IOC`. The SDK's default order_expiry is -1
            # (DEFAULT_28_DAY_ORDER_EXPIRY) which is only valid for resting
            # GTC orders; passing -1 with IOC time-in-force makes the
            # exchange reject the order with "OrderExpiry is invalid".
            #
            # `create_order` DOES have @process_api_key_and_nonce in SDK
            # 1.0.9 — passing the defaults (api_key_index=255, nonce=-1)
            # would auto-resolve via nonce_manager. We pass them explicitly
            # so the outer retry/backoff path sees the SAME nonce that was
            # actually signed (the decorator's auto-fill is opaque, which
            # makes nonce-error logging unreliable).
            from lighter import SignerClient
            api_key_idx, nonce = self._signer.nonce_manager.next_nonce()
            tx, resp, err = await self._signer.create_order(
                market_index=meta.market_index,
                client_order_index=int(cloid_int) & 0xFFFFFFFF,
                base_amount=size_int,
                price=limit_int,
                is_ask=is_ask,
                order_type=SignerClient.ORDER_TYPE_LIMIT,
                time_in_force=SignerClient.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL,
                order_expiry=SignerClient.DEFAULT_IOC_EXPIRY,
                api_key_index=api_key_idx,
                nonce=nonce,
            )
            if err is not None:
                # Exchange-side error (insufficient margin, market halted,
                # nonce conflict, signature drift, etc.). Tell the nonce
                # manager so the next attempt gets a fresh nonce.
                last_error = err
                try:
                    self._signer.nonce_manager.acknowledge_failure(api_key_idx)
                except Exception:
                    pass
                # Lighter docs: "wait at least 350ms before using the same
                # api key". Faster retries on the SAME key cause server-side
                # nonce dedup races even when the manager hits the API for
                # a fresh value. Back off proportionally.
                err_s = str(err)
                if "invalid nonce" in err_s or "invalid signature" in err_s:
                    try:
                        self._signer.nonce_manager.hard_refresh_nonce(api_key_idx)
                    except Exception:
                        pass
                    backoff = 0.6  # > 350ms server cooldown
                else:
                    backoff = 0.2
                logger.warning(
                    f"Lighter create_order rejected: {err} "
                    f"(attempt {attempt+1}/{_TAKER_RETRIES}); "
                    f"sleeping {backoff}s before retry"
                )
                await asyncio.sleep(backoff)
                continue

            # CRITICAL — once create_order returns err=None, the exchange
            # accepted the order. Whether IOC filled or auto-cancelled,
            # the tx exists. We MUST NOT retry — retrying after a server
            # accept means a SECOND order on the same side, which stacks
            # short positions and creates over-hedge (real cost: an
            # earlier version of this code burned ~9× the intended short
            # size by retrying after every server-accept where _verify_fill
            # didn't return >0 fast enough).
            #
            # If _verify_fill comes back 0 we either (a) didn't fill (book
            # moved between accept and match — IOC auto-cancels, the order
            # genuinely didn't take any size) or (b) the cloid lookup is
            # racing the exchange's internal state. Either way: return as
            # "no fill" — the engine treats that as a missed opportunity,
            # NOT as "try again".

            # Server accepted the order. Stamp the LOCAL expected_short_size
            # IMMEDIATELY — independent of `_verify_fill`. The whole point
            # of the position-truth redesign (2026-05-07) is that
            # verify_fill and the WS account snapshot are both
            # eventually-consistent and have produced over-hedge stacks
            # by under-reporting position right after a fill. With the
            # stamp in place, `get_effective_position` returns the new
            # expected size on the very next iter, drift goes to 0,
            # engine doesn't re-fire. The reconciler resolves any genuine
            # non-fill (IOC auto-cancel) at the 30 s timeout via HTTP.
            mid = meta.market_index
            if is_ask:  # side == "sell" — short increases
                self._expected_short_size[mid] = (
                    self._expected_short_size.get(mid, 0.0) + size
                )
            else:  # side == "buy" — covering short, decrement clamped at 0
                cur = self._expected_short_size.get(mid, 0.0)
                new = cur - size
                # Clamp denormals/FP residue from chained decrements to
                # exact 0. Threshold = quarter of one market step (size_decimals
                # tick), well below any real position the engine can act on.
                step = meta.step_size or 1e-12
                self._expected_short_size[mid] = 0.0 if new < step / 4 else new
            self._last_fire_at[mid] = time.monotonic()

            fill_size, fill_price = await self._verify_fill(
                meta=meta, cloid_int=cloid_int, expected_size=size,
            )
            if fill_size > 0:
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
            # Server accepted but verify_fill didn't return >0. Before
            # returning "cancelled", reconcile via the position itself:
            # if our net position grew (sell→short increased, or
            # buy→short decreased), the order DID fill — verify_fill just
            # missed it (eventually-consistent inactive_orders endpoint).
            try:
                post_pos = await self.get_position(symbol)
                post_size = abs(post_pos.size) if post_pos else 0.0
                post_side = post_pos.side if post_pos else pre_side
            except Exception:
                post_size = pre_size
                post_side = pre_side

            position_delta = post_size - pre_size
            # For a SELL we expect short to grow (positive delta on short side);
            # for a BUY we expect short to shrink (positive delta when we were long).
            # Either way, if `|post − pre| ≈ requested size` (within market step),
            # treat as filled.
            step = meta.step_size or 1e-12
            if abs(abs(position_delta) - size) <= step * 2:
                fill_size_inferred = abs(position_delta)
                fill_price_inferred = self._int_to_price(best_int, meta)
                logger.info(
                    f"Lighter {symbol} {side} {size}: verify_fill missed but "
                    f"position grew by {position_delta} → treating as filled."
                )
                if self._fill_callback is not None:
                    fill = Fill(
                        fill_id=str(int(time.time() * 1000)),
                        order_id=str(cloid_int),
                        symbol=symbol, side=side,
                        size=fill_size_inferred, price=fill_price_inferred,
                        fee=0.0, fee_currency="USDC",
                        liquidity="taker",
                        realized_pnl=0.0,
                        timestamp=time.time(),
                    )
                    await self._fill_callback(fill)
                return Order(
                    order_id=str(cloid_int), symbol=symbol, side=side,
                    size=fill_size_inferred, price=fill_price_inferred,
                    status="filled",
                )

            # Position didn't change → IOC genuinely didn't fill (book
            # moved between accept and match). Don't retry, return cancelled.
            logger.warning(
                f"Lighter accepted IOC {symbol} {side} {size} but verify_fill=0 "
                f"and position unchanged. NOT retrying — book likely moved."
            )
            return Order(
                order_id=str(cloid_int), symbol=symbol, side=side,
                size=0.0,
                price=self._int_to_price(best_int, meta),
                status="cancelled",
            )

        # All retries exhausted with err != None on every attempt.
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
        """RAW WS-observed position (no fusion with locally-stamped
        expected state). Used by the reconciler and diagnostic tooling.

        The hedge engine MUST NOT call this directly for drift math —
        use `get_effective_position` instead, which protects against
        the over-hedge race documented in the 2026-05-07 spec.
        """
        meta = self._market_meta_or_raise(symbol)
        meta_d = self._observed_position_meta.get(meta.market_index)
        if meta_d is None:
            return None
        size_signed = meta_d["position"] * meta_d["sign"]
        if abs(size_signed) < 1e-12:
            return None
        return Position(
            symbol=symbol,
            side="short" if size_signed < 0 else "long",
            size=abs(size_signed),
            entry_price=meta_d["avg_entry_price"],
            unrealized_pnl=meta_d["unrealized_pnl"],
        )

    async def get_effective_position(self, symbol: str) -> Position | None:
        """Returns the position the hedge engine should drive drift
        against. Fuses the WS-observed magnitude with the locally-
        stamped expected magnitude:

            size = max(_observed_short_size, _expected_short_size)

        Right after a successful `place_long_term_order`, expected jumps
        to the new total. WS observed lags (eventually-consistent up to
        ~30 s under load). Without this fusion the engine would read
        observed=0 immediately after a fill, compute drift=target,
        fire ANOTHER order — that's the over-hedge stack from
        2026-05-07. The fusion makes the race structurally impossible.

        Returns None when both layers report 0 (closed position).
        """
        meta = self._market_meta_or_raise(symbol)
        mid = meta.market_index
        observed = self._observed_short_size.get(mid, 0.0)
        expected = self._expected_short_size.get(mid, 0.0)
        size = max(observed, expected)
        if size <= 0:
            return None
        # Metadata (entry, unrealized PnL) only available when WS has
        # reported observed. If we're in the just-fired window with
        # observed=0, return placeholder zeros — the engine doesn't
        # use these for drift, only for display.
        meta_d = self._observed_position_meta.get(mid)
        return Position(
            symbol=symbol,
            side="short",
            size=size,
            entry_price=(meta_d or {}).get("avg_entry_price", 0.0),
            unrealized_pnl=(meta_d or {}).get("unrealized_pnl", 0.0),
        )

    async def _fetch_short_size_via_http(self, market_index: int) -> float | None:
        """Authoritative HTTP query for the current short magnitude.
        Used by the reconciler when WS hasn't caught up to expected
        within the timeout. Returns:
          - the unsigned magnitude if the position is a short (sign=-1),
          - 0.0 if the position is flat or long (we don't track longs),
          - None on HTTP/parse error so the caller can skip and retry
            on the next reconciler scan without overwriting state.
        """
        try:
            resp = await self._account_api.account(
                by="index", value=str(self._account_index),
            )
        except Exception as e:
            logger.warning(
                f"_fetch_short_size_via_http({market_index}) failed: {e}"
            )
            return None
        accounts = getattr(resp, "accounts", None) or []
        if not accounts:
            return 0.0
        positions = getattr(accounts[0], "positions", None) or []
        for pos in positions:
            try:
                pos_mid = int(getattr(pos, "market_id"))
            except (TypeError, ValueError):
                continue
            if pos_mid != market_index:
                continue
            sign = int(getattr(pos, "sign", 1))
            if sign != -1:
                return 0.0  # long or flat → no short to track
            try:
                return float(getattr(pos, "position", 0))
            except (TypeError, ValueError):
                return 0.0
        return 0.0  # market not in account → no position

    async def _fetch_position_funding(
        self, *, limit: int = 100,
    ) -> list:
        """Fetch the most-recent funding payments for this account from
        Lighter's position_funding endpoint. Returns the SDK's typed
        PositionFunding objects (or empty list on HTTP/parse failure
        so the poller doesn't crash).

        We do not paginate via cursor here — the poll cadence is 60 s
        and the page size (100) covers far more than one cycle on any
        reasonable funding-history rate. If we ever lag enough that
        100 entries don't cover the gap, the next poll catches up.
        """
        try:
            resp = await self._account_api.position_funding(
                account_index=self._account_index,
                limit=limit,
            )
        except Exception as e:
            logger.warning(f"_fetch_position_funding failed: {e}")
            return []
        return list(getattr(resp, "position_fundings", None) or [])

    async def _funding_poller_iteration(self) -> None:
        """One pass: fetch recent funding entries and dispatch each to
        the engine callback. Dedup + ts filtering live on the engine
        side (it knows the active op's started_at and what funding_ids
        have been counted)."""
        if self._funding_callback is None:
            # Still fetch (cheap and forces an API health probe), then
            # drop on the floor — engine hasn't subscribed yet.
            await self._fetch_position_funding(limit=100)
            return
        entries = await self._fetch_position_funding(limit=100)
        for entry in entries:
            try:
                await self._funding_callback(entry)
            except Exception as e:
                logger.warning(
                    f"funding callback raised on entry "
                    f"{getattr(entry, 'funding_id', '?')}: {e}"
                )

    async def get_oracle_prices(self, symbols: list[str]) -> dict[str, float]:
        """Returns the WS top-of-book midpoint per symbol. We previously
        used `last_trade_price` from /orderBookDetails (HTTP); now the
        midpoint of the cached WS bid/ask is fresher AND requires zero
        HTTP calls — critical for staying under Lighter's WAF."""
        out: dict[str, float] = {}
        for s in symbols:
            meta = self._markets.get(s)
            if meta is None:
                out[s] = 0.0
                continue
            top = self._ws_book_top.get(meta.market_index)
            if top is None:
                out[s] = 0.0
                continue
            bid, ask = top.get("best_bid"), top.get("best_ask")
            if bid and ask:
                out[s] = (bid + ask) / 2.0
            elif bid:
                out[s] = bid
            elif ask:
                out[s] = ask
            else:
                out[s] = 0.0
        return out

    async def get_collateral(self) -> float:
        """Returns the cached available_balance from the WS account_all
        subscription. Returns 0.0 if the WS hasn't sent a snapshot yet —
        callers should treat 0 as 'unknown' (the engine already does)."""
        return self._ws_collateral if self._ws_collateral is not None else 0.0

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
