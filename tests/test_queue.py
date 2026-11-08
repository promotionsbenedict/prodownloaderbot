import asyncio
import os

import pytest

from prodownloader import db


@pytest.mark.skipif(not os.getenv("TEST_DATABASE_URL") or not os.getenv("TEST_REDIS_URL"),
                    reason="Set TEST_DATABASE_URL and TEST_REDIS_URL for disposable integration services")
def test_queue_lifecycle(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", os.environ["TEST_DATABASE_URL"])
    monkeypatch.setenv("REDIS_URL", os.environ["TEST_REDIS_URL"])
    async def check():
        await db.migrate()
        pool, redis = await db.connect()
        try:
            assert await redis.ping()
            await pool.execute("TRUNCATE jobs, users, system_events, platform_stats")
            url = "https://example.com/media"
            first = await db.create_choice(pool, 123, url, "Other website")
            second = await db.create_choice(pool, 456, url, "Other website")
            assert await db.enqueue(pool, 456, first["id"], "original", 1) == "expired"
            assert await db.enqueue(pool, 123, first["id"], "original", 1) == "queued"
            assert await db.enqueue(pool, 456, second["id"], "original", 2) == "queued"
            await db.wake(redis)
            assert await redis.blpop(db.WAKE, timeout=1)
            jobs = await asyncio.gather(db.claim(pool), db.claim(pool))
            assert {job["id"] for job in jobs} == {first["id"], second["id"]}
            one, two = sorted(jobs, key=lambda row: row["user_id"])
            duplicate = await db.create_choice(pool, 123, url, "Other website")
            assert await db.enqueue(pool, 123, duplicate["id"], "original", 3) == "duplicate"
            assert not await db.cancel(pool, 456, one["id"])
            assert await db.cancel(pool, 123, one["id"])
            assert not await db.renew(pool, one)
            await pool.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=$1", two["id"])
            assert [row["state"] for row in await db.recover(pool)] == ["queued"]
            retry = await db.claim(pool)
            assert retry["id"] == two["id"] and retry["attempts"] == 2
            assert not await db.finish(pool, two, "done")
            assert await db.begin_delivery(pool, retry)
            assert not await db.cancel(pool, 456, retry["id"])
            await pool.execute("UPDATE jobs SET lease_until=now()-interval '1 second' WHERE id=$1", retry["id"])
            row, = await db.recover(pool)
            assert row["state"] == "failed" and row["error"] == "delivery_interrupted"
            assert await pool.fetchval("SELECT sum(total) FROM platform_stats") == 2
        finally:
            await redis.aclose()
            await pool.close()

    asyncio.run(check())
