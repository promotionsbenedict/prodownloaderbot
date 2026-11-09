import asyncio
import fcntl
import hashlib
import json
import os
import shutil
import signal
import time
from contextlib import suppress
from pathlib import Path
from uuid import uuid4

import httpx
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramRetryAfter
from aiogram.types import FSInputFile
from redis.exceptions import RedisError

from . import db
from .config import (
    CACHE_TTL,
    MAX_BYTES,
    MEDIA,
    PUBLIC_BASE,
    SIGNING_KEY,
    SPOOL,
    TELEGRAM_LIMIT,
    TIMEOUT,
    event,
    setup_logging,
)
from .security import EXTENSIONS, FILENAME, signed_path
from .telegram import ERRORS, make_bot, update


async def previous_files(pool, job):
    raw = await pool.fetchval(
        """SELECT cache_files FROM jobs WHERE user_id=$1 AND url=$2 AND quality=$3
        AND state='done' AND finished_at>now()-$4*interval '1 second'
        ORDER BY finished_at DESC LIMIT 1""", job["user_id"], job["url"], job["quality"], CACHE_TTL)
    names = json.loads(raw) if raw else []
    if names and all(FILENAME.fullmatch(name) and (MEDIA / name).is_file()
                     and (MEDIA / name).stat().st_mtime > time.time() - CACHE_TTL + 90 for name in names):
        return names
    return []


async def download(client, job):
    attempt = uuid4()
    folder = SPOOL / attempt.hex
    limit = MAX_BYTES if PUBLIC_BASE or job["quality"] == "album" else min(MAX_BYTES, TELEGRAM_LIMIT)
    response = await client.post("/extract", json={
        "attempt": str(attempt), "url": job["url"], "quality": job["quality"], "limit": limit})
    response.raise_for_status()
    result = response.json()
    if "error" in result:
        return [], result["error"]
    names = []
    total = 0
    try:
        for name in result["files"]:
            source = folder / name
            if Path(name).name != name or source.is_symlink() or source.suffix[1:] not in EXTENSIONS:
                raise ValueError("Invalid extraction result")
            size = source.stat().st_size
            total += size
            if total > limit or (not PUBLIC_BASE and size > TELEGRAM_LIMIT):
                return [], "too_large"
            target = f"{uuid4().hex}{source.suffix}"
            await asyncio.to_thread(publish, source, target, size)
            names.append(target)
        return names, "" if names else "unsupported"
    finally:
        await asyncio.to_thread(shutil.rmtree, folder, True)


def publish(source, target, size):
    partial = MEDIA / (target + ".part")
    quota = int(os.getenv("CACHE_CAP_MIB", "2048")) * 1024**2
    with (MEDIA / ".lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        used = 0
        for path in MEDIA.iterdir():
            if FILENAME.fullmatch(path.name.removesuffix(".part")):
                with suppress(FileNotFoundError):
                    used += path.stat().st_size
        if used + size > quota or shutil.disk_usage(MEDIA).free < size + 256 * 1024**2:
            raise OSError("Media storage is full")
        try:
            with open(source, "rb", opener=no_symlink) as stream, partial.open("xb") as output:
                shutil.copyfileobj(stream, output)
            partial.chmod(0o600)
            partial.replace(MEDIA / target)
        finally:
            partial.unlink(missing_ok=True)


def no_symlink(path, flags):
    return os.open(path, flags | os.O_NOFOLLOW)



async def file_digest(path):
    def digest():
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    return await asyncio.to_thread(digest)


async def send(bot, redis, user, name, index):
    path = MEDIA / name
    if path.stat().st_size > TELEGRAM_LIMIT:
        expiry = min(int(time.time()) + 900, int(path.stat().st_mtime) + CACHE_TTL - 5)
        link = PUBLIC_BASE + signed_path(SIGNING_KEY, name, expiry)
        await bot.send_message(user, f"Download file {index} using this temporary link\n{link}",
                               disable_web_page_preview=True)
        return
    key = "prodownloader:telegram-file:" + await file_digest(path)
    file_id = None
    with suppress(RedisError):
        file_id = await redis.get(key)
    if file_id:
        try:
            await bot.send_document(user, file_id.decode())
            return
        except TelegramBadRequest:
            pass
    response = await bot.send_document(user, FSInputFile(path, filename=f"pro-downloader-{index}{path.suffix}"))
    if response.document:
        with suppress(RedisError):
            await redis.set(key, response.document.file_id, ex=86400)


async def run_job(pool, redis, client, bot, job):
    await update(bot, job, "Downloading your media…", cancel=True)
    try:
        names = await previous_files(pool, job)
        reason = ""
        if not names:
            names, reason = await download(client, job)
    except (httpx.HTTPError, OSError, ValueError, KeyError):
        names, reason = [], "temporary"
    if reason:
        state = "queued" if reason in {"temporary", "busy"} and job["attempts"] < 3 else "failed"
        if await db.finish(pool, job, state, reason):
            await update(bot, job, "Source busy. Retrying shortly…" if state == "queued"
                         else ERRORS.get(reason, ERRORS["temporary"]), cancel=state == "queued")
            event("download_result", job=str(job["id"]), state=state, reason=reason)
        return

    if not await db.begin_delivery(pool, job):
        return
    await update(bot, job, "Download ready. Sending your files…")
    try:
        for index, name in enumerate(names, 1):
            try:
                await send(bot, redis, job["user_id"], name, index)
            except TelegramRetryAfter as error:
                if error.retry_after > 60:
                    raise
                await asyncio.sleep(error.retry_after + 1)
                await send(bot, redis, job["user_id"], name, index)
    except (TelegramAPIError, OSError) as error:
        if await db.finish(pool, job, "failed", "delivery"):
            await update(bot, job, ERRORS["delivery"])
        event("delivery_error", job=str(job["id"]), error=type(error).__name__)
        return

    if await db.finish(pool, job, "done", files=json.dumps(names)):
        await update(bot, job, f"✅ Delivered {len(names)} file{'s' if len(names) != 1 else ''}.")
        event("download_result", job=str(job["id"]), state="done", files=len(names))


async def heartbeat(pool, job):
    while True:
        await asyncio.sleep(4)
        if not await db.renew(pool, job):
            return


async def work(pool, redis, client, bot, stop):
    while not stop.is_set():
        try:
            job = await db.claim(pool)
            if not job:
                try:
                    await redis.blpop(db.WAKE, timeout=2)
                except RedisError:
                    await asyncio.sleep(2)
                continue
            processing = asyncio.create_task(run_job(pool, redis, client, bot, job))
            lease = asyncio.create_task(heartbeat(pool, job))
            try:
                done, _ = await asyncio.wait({processing, lease}, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in (processing, lease):
                    task.cancel()
                await asyncio.gather(processing, lease, return_exceptions=True)
        except Exception as error:
            event("worker_error", error=type(error).__name__)
            await asyncio.sleep(3)


def clean_files():
    cutoff = time.time() - CACHE_TTL
    for path in MEDIA.iterdir():
        if FILENAME.fullmatch(path.name.removesuffix(".part")):
            with suppress(FileNotFoundError):
                if path.stat().st_mtime < cutoff:
                    path.unlink()


async def maintenance(pool, redis, bot, stop):
    while not stop.is_set():
        try:
            await redis.set("prodownloader:worker-heartbeat", "1", ex=35)
        except RedisError:
            pass
        try:
            for job in await db.recover(pool):
                if job["state"] == "failed":
                    await update(bot, job, ERRORS[job["error"]])
            await pool.execute(
                """DELETE FROM jobs WHERE (state='choice' AND created_at<now()-interval '30 minutes')
                OR finished_at<now()-interval '30 days'""")
            await pool.execute("DELETE FROM system_events WHERE created_at<now()-interval '30 days'")
            await pool.execute("DELETE FROM platform_stats WHERE day<CURRENT_DATE-90")
            await asyncio.to_thread(clean_files)
        except Exception as error:
            event("maintenance_error", error=type(error).__name__)
        try:
            await asyncio.wait_for(stop.wait(), timeout=10)
        except TimeoutError:
            pass


async def main():
    setup_logging()
    pool, redis = await db.connect()
    stop = asyncio.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(signum, stop.set)
    async with make_bot() as bot, httpx.AsyncClient(
        base_url=os.getenv("EXTRACTOR_URL", "http://extractor:8080"),
        timeout=httpx.Timeout(TIMEOUT + 15, connect=4), trust_env=False
    ) as client:
        tasks = [asyncio.create_task(work(pool, redis, client, bot, stop))
                 for _ in range(int(os.getenv("WORKER_SLOTS", "2")))]
        tasks.append(asyncio.create_task(maintenance(pool, redis, bot, stop)))
        try:
            await stop.wait()
            await asyncio.wait_for(asyncio.gather(*tasks), 25)
        except TimeoutError:
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await pool.close()
            await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
