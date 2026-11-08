import asyncio
import os
import shutil
import signal
import sys
import time
from contextlib import asynccontextmanager, suppress
from uuid import UUID

from fastapi import FastAPI, Request
from pydantic import BaseModel, Field

from .config import MAX_BYTES, MAX_ITEMS, SPOOL, TIMEOUT, event, setup_logging
from .security import EXTENSIONS, QUALITIES, public_url

SLOTS = asyncio.Semaphore(int(os.getenv("EXTRACTOR_SLOTS", "2")))
ACTIVE = set()
REASONS = {"protected", "inaccessible", "too_large", "temporary", "unsupported", "quality"}


async def reaper():
    while True:
        for path in SPOOL.iterdir():
            try:
                if path not in ACTIVE and time.time() - path.stat().st_mtime > TIMEOUT + 600:
                    shutil.rmtree(path, ignore_errors=True)
            except FileNotFoundError:
                pass
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(_):
    setup_logging()
    task = asyncio.create_task(reaper())
    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


class Extract(BaseModel):
    attempt: UUID
    url: str = Field(max_length=2048)
    quality: str = Field(max_length=12)
    limit: int = Field(gt=0, le=MAX_BYTES)


@app.get("/healthz")
async def health():
    return {"ok": SPOOL.is_dir()}


async def wait_for_download(process, request, folder, limit):
    task = asyncio.create_task(process.communicate())
    try:
        async with asyncio.timeout(TIMEOUT):
            while not task.done():
                if await request.is_disconnected():
                    return [], "cancelled"
                size = sum(path.stat().st_size for path in folder.iterdir() if path.exists())
                if size > limit * 2 + 1024 * 1024:
                    return [], "too_large"
                await asyncio.wait({task}, timeout=0.4)
            output, errors = task.result()
            reason = errors.decode(errors="replace").strip()
            if process.returncode:
                return [], reason if reason in REASONS else "temporary"
            return output.decode(errors="replace").splitlines(), ""
    except TimeoutError:
        return [], "temporary"
    finally:
        # Browser and ffmpeg descendants share the process group.
        with suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        await task


@app.post("/extract")
async def extract(body: Extract, request: Request):
    try:
        url = public_url(body.url)
    except ValueError:
        return {"error": "unsupported"}
    if body.quality not in QUALITIES:
        return {"error": "unsupported"}
    if SLOTS.locked():
        return {"error": "busy"}

    async with SLOTS:
        folder = SPOOL / body.attempt.hex
        try:
            folder.mkdir(mode=0o700)
        except FileExistsError:
            return {"error": "temporary"}
        ACTIVE.add(folder)
        keep = False
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "prodownloader.adapters", url, body.quality, str(folder),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
            files, reason = await wait_for_download(process, request, folder, body.limit)
            if reason:
                return {"error": reason}
            safe = []
            total = 0
            for value in dict.fromkeys(files):
                path = folder / os.path.basename(value)
                if (str(path) != value or path.is_symlink() or not path.is_file()
                        or path.suffix[1:] not in EXTENSIONS):
                    continue
                size = path.stat().st_size
                if size:
                    safe.append(path.name)
                    total += size
            if not safe:
                return {"error": "unsupported"}
            if total > body.limit:
                return {"error": "too_large"}
            keep = True
            return {"files": safe[:MAX_ITEMS]}
        except OSError as error:
            event("extractor_error", error=type(error).__name__)
            return {"error": "temporary"}
        finally:
            ACTIVE.discard(folder)
            if not keep:
                shutil.rmtree(folder, ignore_errors=True)
