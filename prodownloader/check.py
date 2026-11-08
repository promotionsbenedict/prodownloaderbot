import asyncio
import socket
import sys
import time
from pathlib import Path
from urllib.request import urlopen


async def check_worker():
    from .db import connect

    pool, redis = await connect()
    try:
        assert await pool.fetchval("SELECT 1") == 1
        assert await redis.get("prodownloader:worker-heartbeat")
    finally:
        await redis.aclose()
        await pool.close()


def main():
    role = sys.argv[1]
    if role == "http":
        with urlopen("http://127.0.0.1:8080/healthz", timeout=3) as response:
            assert b'"ok":true' in response.read(128)
    elif role == "proxy":
        with socket.create_connection(("127.0.0.1", 8888), timeout=3):
            pass
    elif role == "bot":
        assert time.time() - Path("/tmp/prodownloader-polling").stat().st_mtime < 90
    elif role == "worker":
        asyncio.run(check_worker())
    else:
        raise ValueError("Unknown role")


if __name__ == "__main__":
    main()
