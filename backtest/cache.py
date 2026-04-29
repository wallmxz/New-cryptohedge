# backtest/cache.py
from __future__ import annotations
import aiosqlite


class Cache:
    """Simple key/value cache backed by SQLite. Stores serialized blobs by key.

    Used to avoid re-fetching historical data from external APIs.
    """

    def __init__(self, path: str):
        self._path = path
        self._conn: aiosqlite.Connection | None = None

    async def initialize(self) -> None:
        self._conn = await aiosqlite.connect(self._path)
        await self._conn.execute(
            """CREATE TABLE IF NOT EXISTS cache (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                stored_at REAL NOT NULL DEFAULT (strftime('%s', 'now'))
            )"""
        )
        await self._conn.commit()

    async def close(self) -> None:
        if self._conn:
            await self._conn.close()
            self._conn = None

    async def set(self, key: str, value: str) -> None:
        await self._conn.execute(
            "INSERT OR REPLACE INTO cache (key, value, stored_at) VALUES (?, ?, strftime('%s','now'))",
            (key, value),
        )
        await self._conn.commit()

    async def get(self, key: str) -> str | None:
        cursor = await self._conn.execute(
            "SELECT value FROM cache WHERE key = ?", (key,)
        )
        row = await cursor.fetchone()
        return row[0] if row else None
