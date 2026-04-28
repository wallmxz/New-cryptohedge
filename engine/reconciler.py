from __future__ import annotations
import time
import logging

logger = logging.getLogger(__name__)


class Reconciler:
    """Periodically compares DB grid_orders state with exchange open orders.

    - Orders in exchange but not in DB -> cancel (orphans, e.g., from previous run with stale cloid)
    - Orders in DB but not on exchange -> mark as cancelled in DB (lost orders, expired, etc.)
    """
    def __init__(self, *, db, exchange, settings):
        self._db = db
        self._exchange = exchange
        self._settings = settings

    async def reconcile(self) -> list[str]:
        """Run one reconciliation cycle. Returns list of cloids cancelled on exchange."""
        db_active = await self._db.get_active_grid_orders()
        db_cloids = {row["cloid"] for row in db_active}

        try:
            ex_cloids = set(await self._exchange.get_open_orders_cloids(
                self._settings.dydx_symbol,
            ))
        except Exception as e:
            logger.error(f"Reconciler: failed to read open orders: {e}")
            return []

        # Orphans on exchange (not in DB)
        orphans = ex_cloids - db_cloids
        cancelled: list[str] = []
        for cloid in orphans:
            try:
                await self._exchange.cancel_long_term_order(
                    symbol=self._settings.dydx_symbol, cloid_int=int(cloid),
                )
                cancelled.append(cloid)
                logger.info(f"Reconciler: cancelled orphan {cloid}")
            except Exception as e:
                logger.error(f"Reconciler: cancel orphan {cloid} failed: {e}")

        # DB-active but not on exchange (lost)
        lost = db_cloids - ex_cloids
        now = time.time()
        for cloid in lost:
            await self._db.mark_grid_order_cancelled(cloid, now)
            logger.info(f"Reconciler: marked lost grid order {cloid} as cancelled")

        return cancelled
