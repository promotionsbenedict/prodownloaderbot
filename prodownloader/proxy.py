"""Constrained HTTP forward proxy. Resolve once, validate every result, dial the approved IP."""
import asyncio
import ipaddress
import os
import socket
from contextlib import suppress
from urllib.parse import urlsplit

from .security import allowed_address, hostname, public_url

DENIED = tuple(ipaddress.ip_network(entry.strip(), strict=False)
               for entry in os.getenv("DENY_NETWORKS", "").split(",") if entry.strip())
SLOTS = asyncio.Semaphore(96)


async def connect(host, port):
    loop = asyncio.get_running_loop()
    records = await loop.getaddrinfo(hostname(host), port, type=socket.SOCK_STREAM)
    ips = list(dict.fromkeys(record[4][0] for record in records))
    if not ips or not all(allowed_address(ip, DENIED) for ip in ips):
        raise ValueError("Destination denied")
    for ip in ips:
        try:
            return await asyncio.wait_for(asyncio.open_connection(ip, port), 4)
        except (OSError, TimeoutError):
            continue
    raise OSError("Destination unavailable")


async def relay(source, destination):
    while data := await asyncio.wait_for(source.read(65536), 35):
        destination.write(data)
        await destination.drain()
    if destination.can_write_eof():
        destination.write_eof()


async def session(reader, writer):
    upstream = None
    started = False
    try:
        async with asyncio.timeout(8):
            raw = await reader.readuntil(b"\r\n\r\n")
            if len(raw) > 16384:
                raise ValueError("Request too large")
            request, *headers = raw[:-4].split(b"\r\n")
            method, target, version = request.decode("ascii").split()
            if version not in ("HTTP/1.0", "HTTP/1.1"):
                raise ValueError("Unsupported protocol")
            if method == "CONNECT":
                host, port = target.rsplit(":", 1)
                if port != "443":
                    raise ValueError("Port denied")
                response, upstream = await connect(host, 443)
                writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            else:
                if method not in ("GET", "HEAD"):
                    raise ValueError("Method denied")
                parsed = urlsplit(public_url(target))
                if parsed.scheme != "http":
                    raise ValueError("HTTPS requires CONNECT")
                safe = []
                for line in headers:
                    name, value = line.split(b":", 1)
                    if name.lower() in (b"accept", b"accept-encoding", b"user-agent", b"range", b"referer"):
                        safe.append(name + b":" + value)
                path = parsed.path + ("?" + parsed.query if parsed.query else "")
                response, upstream = await connect(parsed.hostname, 80)
                upstream.write(b"\r\n".join([
                    f"{method} {path} HTTP/1.1".encode("ascii"),
                    f"Host: {parsed.hostname}".encode("ascii"), b"Connection: close", *safe, b"", b"",
                ]))
                await upstream.drain()
            started = True
            await writer.drain()
        async with asyncio.timeout(420):
            if method == "CONNECT":
                tasks = [asyncio.create_task(relay(reader, upstream)),
                         asyncio.create_task(relay(response, writer))]
                try:
                    await asyncio.gather(*tasks)
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
            else:
                await relay(response, writer)
    except (ValueError, OSError, EOFError, TimeoutError, asyncio.LimitOverrunError):
        if not started:
            with suppress(OSError):
                writer.write(b"HTTP/1.1 403 Forbidden\r\nConnection: close\r\nContent-Length: 0\r\n\r\n")
                await asyncio.wait_for(writer.drain(), 1)
    finally:
        writer.close()
        if upstream:
            upstream.close()


async def handle(reader, writer):
    if SLOTS.locked():
        writer.close()
        return
    async with SLOTS:
        await session(reader, writer)


async def main():
    server = await asyncio.start_server(handle, "0.0.0.0", 8888, limit=16384)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
