import asyncio
import os
from pathlib import Path
from uuid import uuid4

import asyncpg
from redis.asyncio import Redis
from redis.exceptions import RedisError

from .config import event

WAKE = "prodownloader:queue"


async def connect():
    db = await asyncpg.create_pool(os.environ["DATABASE_URL"], min_size=1, max_size=8,
                                   command_timeout=15)
    redis = Redis.from_url(os.environ["REDIS_URL"], socket_connect_timeout=3, socket_timeout=6)
    return db, redis


async def migrate():
    conn = await asyncpg.connect(os.environ["DATABASE_URL"])
    try:
        async with conn.transaction():
            await conn.execute("SELECT pg_advisory_xact_lock(42493717)")
            await conn.execute("CREATE TABLE IF NOT EXISTS migrations (name text PRIMARY KEY)")
            for path in sorted(Path("migrations").glob("*.sql")):
                if not await conn.fetchval("SELECT 1 FROM migrations WHERE name=$1", path.name):
                    await conn.execute(path.read_text())
                    await conn.execute("INSERT INTO migrations VALUES($1)", path.name)
        for admin_id in os.getenv("ADMIN_IDS", "").split(","):
            if admin_id.strip():
                await conn.execute("INSERT INTO admins VALUES($1) ON CONFLICT DO NOTHING", int(admin_id))
    finally:
        await conn.close()


async def wake(redis):
    try:
        async with redis.pipeline(transaction=True) as pipe:
            await pipe.lpush(WAKE, "1").ltrim(WAKE, 0, 63).execute()
    except RedisError:
        event("queue_signal_unavailable")


async def register(db, user):
    await db.execute("INSERT INTO users (id) VALUES ($1) ON CONFLICT DO NOTHING", user)


async def create_choice(db, user, url, platform):
    async with db.acquire() as conn, conn.transaction():
        await register(conn, user)
        await conn.fetchval("SELECT id FROM users WHERE id=$1 FOR UPDATE", user)
        await conn.execute("DELETE FROM jobs WHERE user_id=$1 AND state='choice'", user)
        if await conn.fetchval(
            "SELECT count(*) FROM jobs WHERE user_id=$1 AND state IN ('queued','running','delivering')", user
        ) >= 3:
            return None
        return await conn.fetchrow(
            """INSERT INTO jobs (id,user_id,url,platform,quality,state)
            SELECT $1,id,$3,$4,quality,'choice' FROM users WHERE id=$2 RETURNING id,quality""",
            uuid4(), user, url, platform)


async def enqueue(db, user, job, quality, message):
    async with db.acquire() as conn, conn.transaction():
        await conn.fetchval("SELECT id FROM users WHERE id=$1 FOR UPDATE", user)
        await conn.execute("SELECT pg_advisory_xact_lock(42493718)")
        if await conn.fetchval("SELECT count(*) FROM jobs WHERE state='queued'") >= 200:
            return "full"
        choice = await conn.fetchrow(
            """SELECT url FROM jobs WHERE id=$1 AND user_id=$2 AND state='choice'
            AND created_at > now() - interval '15 minutes' FOR UPDATE""", job, user)
        if choice is None:
            return "expired"
        if await conn.fetchval(
            """SELECT 1 FROM jobs WHERE user_id=$1 AND url=$2 AND quality=$3
            AND state IN ('queued','running','delivering')""", user, choice["url"], quality):
            return "duplicate"
        await conn.execute(
            """UPDATE jobs SET state='queued', quality=$2, message_id=$3,
            available_at=now() WHERE id=$1""", job, quality, message)
        return "queued"


async def claim(db):
    return await db.fetchrow(
        """WITH next AS (
            SELECT id FROM jobs WHERE state='queued' AND available_at<=now()
            ORDER BY available_at,created_at LIMIT 1 FOR UPDATE SKIP LOCKED
        ) UPDATE jobs SET state='running', attempts=attempts+1, lease=$1,
        lease_until=now()+interval '45 seconds' FROM next
        WHERE jobs.id=next.id RETURNING jobs.*""", uuid4())


async def renew(db, job):
    return await db.fetchval(
        """UPDATE jobs SET lease_until=now()+interval '45 seconds'
        WHERE id=$1 AND lease=$2 AND lease_until>now()
        AND state IN ('running','delivering') RETURNING true""", job["id"], job["lease"])


async def begin_delivery(db, job):
    return await db.fetchval(
        """UPDATE jobs SET state='delivering' WHERE id=$1 AND lease=$2
        AND state='running' AND lease_until>now() RETURNING true""", job["id"], job["lease"])


async def finish(db, job, state, error=None, files=None):
    async with db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """UPDATE jobs SET state=$3, error=$4, cache_files=$5, lease=NULL, lease_until=NULL,
            finished_at=CASE WHEN $3='queued' THEN NULL ELSE now() END,
            available_at=now()+attempts*interval '6 seconds'
            WHERE id=$1 AND lease=$2 AND lease_until>now() AND state IN ('running','delivering')
            RETURNING platform""", job["id"], job["lease"], state, error, files)
        if row is None:
            return False
        await record_outcome(conn, job["id"], row["platform"], state, error)
        return True


async def record_outcome(conn, job_id, platform, state, error=None):
    await conn.execute("INSERT INTO system_events(job_id,event) VALUES($1,$2)", job_id, error or state)
    if state in {"done", "failed", "cancelled"}:
        await conn.execute(
            """INSERT INTO platform_stats(platform,state,total) VALUES($1,$2,1)
            ON CONFLICT(platform,day,state) DO UPDATE SET total=platform_stats.total+1""",
            platform, state)


async def cancel(db, user, job):
    async with db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            """UPDATE jobs SET state='cancelled', lease=NULL, lease_until=NULL, finished_at=now()
            WHERE id=$1 AND user_id=$2 AND state IN ('choice','queued','running')
            RETURNING platform""", job, user)
        if row:
            await record_outcome(conn, job, row["platform"], "cancelled")
        return bool(row)


async def recover(db):
    async with db.acquire() as conn, conn.transaction():
        rows = await conn.fetch(
            """UPDATE jobs SET state=CASE WHEN state='running' AND attempts<3 THEN 'queued' ELSE 'failed' END,
            error=CASE WHEN state='delivering' THEN 'delivery_interrupted' ELSE 'worker_lost' END,
            finished_at=CASE WHEN state='running' AND attempts<3 THEN NULL ELSE now() END,
            lease=NULL, lease_until=NULL, available_at=now()
            WHERE state IN ('running','delivering') AND lease_until<=now() RETURNING *""")
        for row in rows:
            await record_outcome(conn, row["id"], row["platform"], row["state"], row["error"])
        return rows


if __name__ == "__main__":
    asyncio.run(migrate())
