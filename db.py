from __future__ import annotations
import time
import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at REAL
);

CREATE TABLE IF NOT EXISTS deposits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    action TEXT NOT NULL,
    pool_value_usd REAL NOT NULL,
    token0_amount REAL,
    token1_amount REAL,
    cow_tokens REAL,
    tx_hash TEXT
);

CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    side TEXT NOT NULL,
    size REAL NOT NULL,
    price REAL NOT NULL,
    fee REAL NOT NULL,
    fee_currency TEXT,
    liquidity TEXT NOT NULL,
    realized_pnl REAL,
    order_id TEXT
);

CREATE TABLE IF NOT EXISTS funding (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    exchange TEXT NOT NULL,
    symbol TEXT NOT NULL,
    amount REAL NOT NULL,
    rate REAL
);

CREATE TABLE IF NOT EXISTS pool_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    pool_value_usd REAL NOT NULL,
    token0_amount REAL,
    token1_amount REAL,
    hedge_value_usd REAL,
    hedge_pnl REAL,
    pool_pnl REAL,
    net_pnl REAL,
    funding_cumulative REAL,
    fees_earned_cumulative REAL,
    fees_paid_cumulative REAL
);

CREATE TABLE IF NOT EXISTS order_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp REAL NOT NULL,
    exchange TEXT NOT NULL,
    action TEXT NOT NULL,
    side TEXT,
    size REAL,
    price REAL,
    reason TEXT
);

CREATE TABLE IF NOT EXISTS grid_orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cloid TEXT UNIQUE NOT NULL,
    side TEXT NOT NULL,
    target_price REAL NOT NULL,
    size REAL NOT NULL,
    placed_at REAL NOT NULL,
    cancelled_at REAL,
    fill_id INTEGER REFERENCES fills(id)
);
CREATE INDEX IF NOT EXISTS idx_grid_orders_active ON grid_orders(cloid)
    WHERE cancelled_at IS NULL AND fill_id IS NULL;
"""


class Database:
    def __init__(self, path: str = "automoney.db"):
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.executescript(SCHEMA)
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()

    async def list_tables(self) -> list[str]:
        cursor = await self._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
        rows = await cursor.fetchall()
        return [r["name"] for r in rows]

    async def set_config(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO config (key, value, updated_at) VALUES (?, ?, ?)",
            (key, value, time.time()),
        )
        await self._conn.commit()

    async def get_config(self, key: str) -> str | None:
        cursor = await self._conn.execute(
            "SELECT value FROM config WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row["value"] if row else None

    async def insert_fill(
        self, *, timestamp: float, exchange: str, symbol: str, side: str,
        size: float, price: float, fee: float, fee_currency: str,
        liquidity: str, realized_pnl: float, order_id: str,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO fills
            (timestamp, exchange, symbol, side, size, price, fee, fee_currency,
             liquidity, realized_pnl, order_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, exchange, symbol, side, size, price, fee, fee_currency,
             liquidity, realized_pnl, order_id),
        )
        await self._conn.commit()

    async def get_fills(
        self, exchange: str | None = None, symbol: str | None = None,
        limit: int = 100,
    ) -> list[dict]:
        query = "SELECT * FROM fills WHERE 1=1"
        params: list = []
        if exchange:
            query += " AND exchange = ?"
            params.append(exchange)
        if symbol:
            query += " AND symbol = ?"
            params.append(symbol)
        query += " ORDER BY timestamp DESC LIMIT ?"
        params.append(limit)
        cursor = await self._conn.execute(query, params)
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def get_fill_stats(self) -> dict:
        cursor = await self._conn.execute("""
            SELECT
                SUM(CASE WHEN liquidity='maker' THEN 1 ELSE 0 END) as maker_count,
                SUM(CASE WHEN liquidity='taker' THEN 1 ELSE 0 END) as taker_count,
                SUM(CASE WHEN liquidity='maker' THEN size ELSE 0 END) as maker_volume,
                SUM(CASE WHEN liquidity='taker' THEN size ELSE 0 END) as taker_volume,
                SUM(fee) as total_fees,
                SUM(realized_pnl) as total_realized_pnl
            FROM fills
        """)
        row = await cursor.fetchone()
        return dict(row)

    async def insert_funding(
        self, *, timestamp: float, exchange: str, symbol: str,
        amount: float, rate: float,
    ) -> None:
        await self._conn.execute(
            "INSERT INTO funding (timestamp, exchange, symbol, amount, rate) VALUES (?, ?, ?, ?, ?)",
            (timestamp, exchange, symbol, amount, rate),
        )
        await self._conn.commit()

    async def insert_pool_snapshot(
        self, *, timestamp: float, pool_value_usd: float,
        token0_amount: float, token1_amount: float, hedge_value_usd: float,
        hedge_pnl: float, pool_pnl: float, net_pnl: float,
        funding_cumulative: float, fees_earned_cumulative: float,
        fees_paid_cumulative: float,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO pool_snapshots
            (timestamp, pool_value_usd, token0_amount, token1_amount,
             hedge_value_usd, hedge_pnl, pool_pnl, net_pnl,
             funding_cumulative, fees_earned_cumulative, fees_paid_cumulative)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, pool_value_usd, token0_amount, token1_amount,
             hedge_value_usd, hedge_pnl, pool_pnl, net_pnl,
             funding_cumulative, fees_earned_cumulative, fees_paid_cumulative),
        )
        await self._conn.commit()

    async def get_pool_snapshots(self, limit: int = 1000) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM pool_snapshots ORDER BY timestamp ASC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def insert_order_log(
        self, *, timestamp: float, exchange: str, action: str,
        side: str | None = None, size: float | None = None,
        price: float | None = None, reason: str | None = None,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO order_log
            (timestamp, exchange, action, side, size, price, reason)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, exchange, action, side, size, price, reason),
        )
        await self._conn.commit()

    async def get_order_logs(self, limit: int = 50) -> list[dict]:
        cursor = await self._conn.execute(
            "SELECT * FROM order_log ORDER BY timestamp DESC LIMIT ?",
            (limit,),
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]

    async def insert_deposit(
        self, *, timestamp: float, action: str, pool_value_usd: float,
        token0_amount: float, token1_amount: float, cow_tokens: float,
        tx_hash: str,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO deposits
            (timestamp, action, pool_value_usd, token0_amount, token1_amount,
             cow_tokens, tx_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (timestamp, action, pool_value_usd, token0_amount, token1_amount,
             cow_tokens, tx_hash),
        )
        await self._conn.commit()

    async def insert_grid_order(
        self, *, cloid: str, side: str, target_price: float,
        size: float, placed_at: float,
    ) -> None:
        await self._conn.execute(
            """INSERT INTO grid_orders (cloid, side, target_price, size, placed_at)
               VALUES (?, ?, ?, ?, ?)""",
            (cloid, side, target_price, size, placed_at),
        )
        await self._conn.commit()

    async def mark_grid_order_cancelled(self, cloid: str, ts: float) -> None:
        await self._conn.execute(
            "UPDATE grid_orders SET cancelled_at = ? WHERE cloid = ?",
            (ts, cloid),
        )
        await self._conn.commit()

    async def mark_grid_order_filled(self, cloid: str, fill_id: int) -> None:
        await self._conn.execute(
            "UPDATE grid_orders SET fill_id = ? WHERE cloid = ?",
            (fill_id, cloid),
        )
        await self._conn.commit()

    async def get_active_grid_orders(self) -> list[dict]:
        cursor = await self._conn.execute(
            """SELECT * FROM grid_orders
               WHERE cancelled_at IS NULL AND fill_id IS NULL
               ORDER BY placed_at ASC"""
        )
        rows = await cursor.fetchall()
        return [dict(r) for r in rows]
