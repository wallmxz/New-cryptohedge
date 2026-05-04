"""DB: cross-pair columns + accumulator allowlist for new fields."""
import pytest
from db import Database


@pytest.mark.asyncio
async def test_operations_table_has_dual_leg_columns(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    cursor = await db._conn.execute("PRAGMA table_info(operations)")
    cols = {row["name"] for row in await cursor.fetchall()}
    assert "baseline_token0_usd_price" in cols
    assert "baseline_token1_usd_price" in cols
    assert "perp_fees_paid_token0" in cols
    assert "perp_fees_paid_token1" in cols
    assert "funding_paid_token0" in cols
    assert "funding_paid_token1" in cols
    await db.close()


@pytest.mark.asyncio
async def test_beefy_pairs_cache_has_dydx_perp_token1(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    cursor = await db._conn.execute("PRAGMA table_info(beefy_pairs_cache)")
    cols = {row["name"] for row in await cursor.fetchall()}
    assert "dydx_perp_token1" in cols
    await db.close()


@pytest.mark.asyncio
async def test_accumulator_allows_per_leg_fields(tmp_path):
    db = Database(str(tmp_path / "test.db"))
    await db.initialize()
    op_id = await db.insert_operation(
        started_at=0, status="active",
        baseline_eth_price=4000, baseline_pool_value_usd=300,
        baseline_amount0=0.1, baseline_amount1=0, baseline_collateral=130,
    )
    # Should NOT raise
    await db.add_to_operation_accumulator(op_id, "perp_fees_paid_token0", 0.5)
    await db.add_to_operation_accumulator(op_id, "perp_fees_paid_token1", 0.3)
    await db.add_to_operation_accumulator(op_id, "funding_paid_token0", 1.2)
    await db.add_to_operation_accumulator(op_id, "funding_paid_token1", 0.8)
    row = await db.get_operation(op_id)
    assert row["perp_fees_paid_token0"] == 0.5
    assert row["perp_fees_paid_token1"] == 0.3
    await db.close()
