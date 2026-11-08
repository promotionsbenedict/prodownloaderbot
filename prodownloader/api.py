from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from .config import MEDIA, PUBLIC_BASE, SIGNING_KEY, setup_logging
from .db import connect
from .security import valid_signature


@asynccontextmanager
async def lifespan(app):
    setup_logging()
    app.state.db, app.state.redis = await connect()
    try:
        yield
    finally:
        await app.state.redis.aclose()
        await app.state.db.close()


app = FastAPI(lifespan=lifespan, docs_url=None, openapi_url=None, redoc_url=None)


@app.get("/healthz")
async def health():
    try:
        await app.state.db.fetchval("SELECT 1 FROM migrations LIMIT 1")
        await app.state.redis.ping()
        if not MEDIA.is_dir():
            raise RuntimeError("Media unavailable")
    except Exception:
        raise HTTPException(503, "Unavailable") from None
    return {"ok": True}


@app.api_route("/files/{key}/{expiry}/{signature}", methods=["GET", "HEAD"])
async def file(key: str, expiry: int, signature: str):
    if not PUBLIC_BASE or not valid_signature(SIGNING_KEY, key, expiry, signature):
        raise HTTPException(404)
    path = MEDIA / key
    if path.is_symlink() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, filename=f"pro-downloader{Path(key).suffix}",
                        media_type="application/octet-stream", headers={
                            "Cache-Control": "private, no-store",
                            "Referrer-Policy": "no-referrer",
                            "X-Content-Type-Options": "nosniff",
                        })
