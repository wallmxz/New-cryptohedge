import pytest
import aiosqlite
from db import Database


@pytest.mark.asyncio
async def test_grid_orders_has_trigger_price_and_is_stop_order_cols(tmp_path):
    """Migration A4: grid_orders ganha trigger_price + is_stop_order cols."""
    db_path = tmp_path / "test.db"
    db = Database(str(db_path))
    await db.initialize()
    async with aiosqlite.connect(str(db_path)) as conn:
        cur = await conn.execute("PRAGMA table_info(grid_orders)")
        cols = [r[1] for r in await cur.fetchall()]
    assert "trigger_price" in cols
    assert "is_stop_order" in cols
    await db.close()
