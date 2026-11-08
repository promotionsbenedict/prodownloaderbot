import json
import logging
import os
from pathlib import Path
from urllib.parse import urlsplit

MEDIA = Path(os.getenv("MEDIA_ROOT", "/media"))
SPOOL = Path(os.getenv("SPOOL_ROOT", "/spool"))
MAX_BYTES = int(os.getenv("MAX_MEDIA_MIB", "200")) * 1024**2
MAX_ITEMS = int(os.getenv("MAX_ITEMS", "8"))
TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "240"))
TELEGRAM_LIMIT = 49_000_000
CACHE_TTL = int(os.getenv("CACHE_TTL", "3600"))
PUBLIC_BASE = os.getenv("PUBLIC_BASE_URL", "").rstrip("/")
SIGNING_KEY = os.getenv("FILE_SIGNING_KEY", "")
if PUBLIC_BASE:
    base = urlsplit(PUBLIC_BASE)
    if base.scheme != "https" or not base.hostname or base.path or base.query or base.fragment or base.username:
        raise RuntimeError("PUBLIC_BASE_URL must be an HTTPS origin")
    if len(SIGNING_KEY) < 32:
        raise RuntimeError("FILE_SIGNING_KEY must contain at least 32 characters")


def event(name, **fields):
    logging.getLogger("prodownloader").info(json.dumps({"event": name, **fields}))


def setup_logging():
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    # Third-party loggers can include request URLs or user input.
    for name in ("httpx", "httpcore", "aiogram"):
        logging.getLogger(name).disabled = True
    logging.getLogger("aiogram.event").disabled = True
    logging.getLogger("aiogram.dispatcher").disabled = True
