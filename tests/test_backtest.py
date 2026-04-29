import pytest
from backtest.cache import Cache


@pytest.mark.asyncio
async def test_cache_set_get_string(tmp_path):
    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    await cache.set("k1", "value1")
    assert await cache.get("k1") == "value1"
    assert await cache.get("missing") is None
    await cache.close()


@pytest.mark.asyncio
async def test_cache_overwrites(tmp_path):
    cache = Cache(str(tmp_path / "c.db"))
    await cache.initialize()
    await cache.set("k1", "first")
    await cache.set("k1", "second")
    assert await cache.get("k1") == "second"
    await cache.close()


@pytest.mark.asyncio
async def test_cache_persists_across_instances(tmp_path):
    path = str(tmp_path / "c.db")
    c1 = Cache(path)
    await c1.initialize()
    await c1.set("k", "persisted")
    await c1.close()
    c2 = Cache(path)
    await c2.initialize()
    assert await c2.get("k") == "persisted"
    await c2.close()
