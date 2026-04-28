from __future__ import annotations
import asyncio
import time
import logging
from state import StateHub
from db import Database
from config import Settings
from chains.evm import EVMChainReader, calc_pool_position
from exchanges.base import ExchangeAdapter, Fill
from exchanges.hyperliquid import HyperliquidAdapter
from exchanges.dydx import DydxAdapter
from engine.hedge import compute_hedge_action
from engine.orderbook import calc_maker_price, calc_aggressive_price, check_order_depth
from engine.pnl import calc_pnl

logger = logging.getLogger(__name__)
SNAPSHOT_INTERVAL = 10.0


class Engine:
    def __init__(self, settings: Settings, hub: StateHub, db: Database):
        self._settings = settings
        self._hub = hub
        self._db = db
        self._exchange: ExchangeAdapter | None = None
        self._chain: EVMChainReader | None = None
        self._snapshot_task: asyncio.Task | None = None

    async def start(self) -> None:
        if self._settings.active_exchange == "hyperliquid":
            self._exchange = HyperliquidAdapter(
                api_key=self._settings.hyperliquid_api_key,
                api_secret=self._settings.hyperliquid_api_secret,
                wallet_address=self._settings.wallet_address,
            )
        else:
            self._exchange = DydxAdapter(
                mnemonic=self._settings.dydx_mnemonic,
                wallet_address=self._settings.wallet_address,
            )

        await self._exchange.connect()
        self._hub.connected_exchange = True

        symbol = self._settings.hyperliquid_symbol if self._settings.active_exchange == "hyperliquid" else self._settings.dydx_symbol
        await self._exchange.subscribe_orderbook(symbol, self._on_book_update)
        await self._exchange.subscribe_fills(symbol, self._on_fill)

        self._chain = EVMChainReader(
            rpc_url=self._settings.arbitrum_rpc_url,
            fallback_rpc_url=self._settings.arbitrum_rpc_fallback,
            vault_address=self._settings.clm_vault_address,
            pool_address=self._settings.clm_pool_address,
            wallet_address=self._settings.wallet_address,
            poll_interval=1.0,
            on_update=self._on_chain_update,
        )
        await self._chain.start()
        self._hub.connected_chain = True

        self._snapshot_task = asyncio.create_task(self._snapshot_loop())
        logger.info("Engine started")

    async def stop(self) -> None:
        if self._snapshot_task:
            self._snapshot_task.cancel()
        if self._exchange:
            await self._exchange.disconnect()
        if self._chain:
            await self._chain.stop()

    async def _on_chain_update(self, data: dict) -> None:
        self._hub.cow_balance = data["cow_balance"]
        self._hub.cow_total_supply = data["total_supply"]
        self._hub.vault_balances = (data["vault_token0"], data["vault_token1"])
        price_token0 = self._hub.best_bid if self._hub.best_bid > 0 else 0.0
        if price_token0 <= 0:
            # No live quote yet — defer valuation until exchange book arrives.
            self._hub.last_update = time.time()
            return
        price_token1 = 1.0 if self._settings.pool_token1_is_stable else self._settings.pool_token1_usd_price
        pos = calc_pool_position(
            cow_balance=data["cow_balance"],
            total_supply=data["total_supply"],
            vault_token0=data["vault_token0"],
            vault_token1=data["vault_token1"],
            price_token0_usd=price_token0,
            price_token1_usd=price_token1,
        )
        self._hub.pool_value_usd = pos["value_usd"]
        self._hub.pool_tokens = {
            self._settings.pool_token0_symbol: pos["my_token0"],
            self._settings.pool_token1_symbol: pos["my_token1"],
        }
        self._hub.last_update = time.time()

    async def _on_book_update(self, data: dict) -> None:
        bids = data.get("bids", data.get("levels", []))
        asks = data.get("asks", [])
        if bids and isinstance(bids[0], (list, tuple)):
            self._hub.best_bid = float(bids[0][0])
        if asks and isinstance(asks[0], (list, tuple)):
            self._hub.best_ask = float(asks[0][0])

        if self._hub.my_order:
            book_levels = {}
            side = self._hub.my_order["side"]
            levels = bids if side == "buy" else asks
            for level in levels:
                book_levels[float(level[0])] = float(level[1])
            action = check_order_depth(
                side=side, price=self._hub.my_order["price"],
                book_levels=book_levels, max_depth=self._hub.repost_depth,
            )
            if action == "REPOST":
                await self._repost_order()

        await self._hedge_cycle()
        self._hub.last_update = time.time()

    async def _on_fill(self, fill: Fill) -> None:
        await self._db.insert_fill(
            timestamp=fill.timestamp, exchange=self._exchange.name,
            symbol=fill.symbol, side=fill.side, size=fill.size,
            price=fill.price, fee=fill.fee, fee_currency=fill.fee_currency,
            liquidity=fill.liquidity, realized_pnl=fill.realized_pnl,
            order_id=fill.order_id,
        )
        if fill.liquidity == "maker":
            self._hub.total_maker_fills += 1
            self._hub.total_maker_volume += fill.size
        else:
            self._hub.total_taker_fills += 1
            self._hub.total_taker_volume += fill.size
        self._hub.total_fees_paid += fill.fee
        self._hub.hedge_realized_pnl += fill.realized_pnl
        await self._db.insert_order_log(
            timestamp=time.time(), exchange=self._exchange.name,
            action="fill", side=fill.side, size=fill.size,
            price=fill.price, reason=fill.liquidity,
        )
        self._hub.my_order = None
        self._hub.last_update = time.time()

    async def _hedge_cycle(self) -> None:
        if self._hub.safe_mode or self._hub.pool_value_usd <= 0:
            return

        symbol = self._settings.hyperliquid_symbol if self._settings.active_exchange == "hyperliquid" else self._settings.dydx_symbol
        current_hedge = 0.0
        pos = await self._exchange.get_position(symbol)
        if pos:
            current_hedge = pos.size
            self._hub.hedge_position = {"side": pos.side, "size": pos.size, "entry": pos.entry_price}
            self._hub.hedge_unrealized_pnl = pos.unrealized_pnl

        token_exposure_base = self._hub.pool_tokens.get(self._settings.pool_token0_symbol, 0.0)
        decision = compute_hedge_action(
            token_exposure_base=token_exposure_base,
            hedge_ratio=self._hub.hedge_ratio, current_hedge_size=current_hedge,
            max_exposure_pct=self._hub.max_exposure_pct, safe_mode=self._hub.safe_mode,
        )

        if decision.action == "HOLD":
            return

        tick = self._exchange.get_tick_size(symbol)
        if decision.action == "MAKER":
            price = calc_maker_price(side=decision.side, best_bid=self._hub.best_bid, best_ask=self._hub.best_ask, tick=tick)
        else:
            price = calc_aggressive_price(side=decision.side, best_bid=self._hub.best_bid, best_ask=self._hub.best_ask, tick=tick)

        if self._hub.my_order:
            await self._exchange.cancel_order(self._hub.my_order["order_id"])
            self._hub.my_order = None

        order = await self._exchange.place_limit_order(symbol=symbol, side=decision.side, size=decision.delta, price=price)
        self._hub.my_order = {"order_id": order.order_id, "side": order.side, "size": order.size, "price": order.price}

        reason = "exposure_rebalance" if decision.action == "MAKER" else "aggressive"
        await self._db.insert_order_log(
            timestamp=time.time(), exchange=self._exchange.name,
            action="place", side=decision.side, size=decision.delta,
            price=price, reason=reason,
        )

    async def _repost_order(self) -> None:
        if not self._hub.my_order:
            return
        await self._exchange.cancel_order(self._hub.my_order["order_id"])
        await self._db.insert_order_log(
            timestamp=time.time(), exchange=self._exchange.name,
            action="cancel", side=self._hub.my_order["side"],
            price=self._hub.my_order["price"], reason="depth_repost",
        )
        self._hub.my_order = None

    async def _snapshot_loop(self) -> None:
        while True:
            await asyncio.sleep(SNAPSHOT_INTERVAL)
            pnl = calc_pnl(
                pool_value_usd=self._hub.pool_value_usd,
                pool_deposited_usd=self._hub.pool_deposited_usd,
                hedge_realized_pnl=self._hub.hedge_realized_pnl,
                hedge_unrealized_pnl=self._hub.hedge_unrealized_pnl,
                funding_total=self._hub.funding_total,
                total_fees_paid=self._hub.total_fees_paid,
            )
            await self._db.insert_pool_snapshot(
                timestamp=time.time(),
                pool_value_usd=self._hub.pool_value_usd,
                token0_amount=self._hub.pool_tokens.get("ARB", 0),
                token1_amount=self._hub.pool_tokens.get("WETH", 0),
                hedge_value_usd=self._hub.hedge_position["size"] if self._hub.hedge_position else 0,
                hedge_pnl=pnl.hedge_pnl, pool_pnl=pnl.pool_pnl, net_pnl=pnl.net_pnl,
                funding_cumulative=self._hub.funding_total,
                fees_earned_cumulative=self._hub.total_fees_earned,
                fees_paid_cumulative=self._hub.total_fees_paid,
            )
