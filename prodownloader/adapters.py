"""Extraction subprocess entry point. Runs without bot, database or signing credentials."""
import asyncio
import os
import re
import resource
import ssl
import subprocess
import sys
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from .config import MAX_BYTES, MAX_ITEMS
from .security import MEDIA_TYPES, QUALITIES, public_url

PROXY = os.getenv("EXTRACTION_PROXY", "http://egress:8888")
FORMAT = {
    "original": "bv*+ba/b",
    "mp3": "ba/b",
    **{height: f"bv*[height<={height}]+ba/b[height<={height}]"
       for height in ("1080", "720", "480")},
}
DEMUXERS = {"mp4": "mov", "mov": "mov", "m4a": "mov", "mkv": "matroska",
            "webm": "matroska", "ogg": "ogg", "wav": "wav", "flac": "flac"}
MIME_SNIFF = {
    "jpg": lambda b: b.startswith(b"\xff\xd8\xff"),
    "png": lambda b: b.startswith(b"\x89PNG\r\n\x1a\n"),
    "gif": lambda b: b.startswith((b"GIF89a", b"GIF87a")),
    "webp": lambda b: b.startswith(b"RIFF") and b[8:12] == b"WEBP",
    "wav": lambda b: b.startswith(b"RIFF") and b[8:12] == b"WAVE",
}


def extension_matches(path, ext):
    if ext not in MIME_SNIFF:
        return True
    with path.open("rb") as stream:
        return MIME_SNIFF[ext](stream.read(16))


def error_kind(message):
    if any(word in message for word in ("drm", "copyright protection", "widevine", "fairplay")):
        return "protected"
    if any(word in message for word in ("login", "log in", "sign in", "private", "401", "403",
                                        "age-restricted", "forbidden", "captcha", "geo restricted")):
        return "inaccessible"
    if re.search(r"\b(429|50[0234])\b", message) or any(word in message for word in (
            "timed out", "timeout", "connection reset", "temporary failure", "network is unreachable")):
        return "temporary"
    if "requested format is not available" in message:
        return "quality"
    if "max-filesize" in message or "file too large" in message:
        return "too_large"
    return "unsupported"


async def ytdlp(url, quality, folder):
    cmd = [
        sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-cache-dir", "--no-color",
        "--proxy", PROXY, "--compat-options", "no-certifi", "--socket-timeout", "12", "--retries", "1", "--fragment-retries", "1",
        "--extractor-retries", "1", "--no-live-from-start", "--restrict-filenames", "--no-mtime",
        "--match-filters", "!is_live", "--max-filesize", str(MAX_BYTES),
        "--playlist-end", str(MAX_ITEMS if quality == "album" else 1),
        "--downloader", "native", "--js-runtimes", "node",
        "--postprocessor-args", "ffmpeg_i:-protocol_whitelist file,pipe",
        "--no-part", "--no-keep-video",
        "-f", FORMAT.get(quality, FORMAT["original"]),
        "-o", str(folder / "%(autonumber)03d-%(id).55s.%(ext)s"),
        "--print", "after_move:filepath", "--no-simulate",
    ]
    if quality != "album":
        cmd.append("--no-playlist")
    if quality == "mp3":
        cmd += ["-x", "--audio-format", "mp3", "--audio-quality", "192K"]
    process = await asyncio.create_subprocess_exec(*cmd, "--", url, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.PIPE)
    output, errors = await process.communicate()
    files = []
    for line in output.decode(errors="replace").splitlines():
        path = Path(line)
        if path.parent == folder and path.is_file():
            files.append(path)
    return files, error_kind(errors.decode(errors="replace").lower())


async def direct(client, url, folder, index, remaining, quality):
    url = public_url(url)
    for _ in range(6):
        async with client.stream("GET", url) as response:
            if response.is_redirect:
                url = public_url(urljoin(url, response.headers["location"]))
                continue
            if response.status_code in (401, 403, 429, 500, 502, 503, 504):
                return None, error_kind(str(response.status_code))
            ext = MEDIA_TYPES.get(response.headers.get("content-type", "").split(";", 1)[0].strip().lower())
            if response.status_code != 200 or not ext:
                return None, "unsupported"
            if quality == "mp3" and ext in ("jpg", "png", "gif", "webp"):
                return None, "quality"
            length = response.headers.get("content-length", "")
            if length.isdecimal() and int(length) > remaining:
                return None, "too_large"
            path = folder / f"media-{index}.{ext}"
            received = 0
            try:
                with path.open("xb") as stream:
                    async for chunk in response.aiter_bytes(65536):
                        received += len(chunk)
                        if received > remaining:
                            return None, "too_large"
                        stream.write(chunk)
                if received and extension_matches(path, ext):
                    if quality == "mp3" and ext != "mp3":
                        target = path.with_suffix(".mp3")
                        result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist",
                                                 "file,pipe", "-f", DEMUXERS[ext], "-i", str(path), "-vn", "-b:a", "192k",
                                                 "-fs", str(remaining + 1), str(target)],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                timeout=60, check=False)
                        if result.returncode or not target.is_file():
                            target.unlink(missing_ok=True)
                            return None, "quality"
                        path.unlink()
                        return target, ""
                    if quality in ("480", "720", "1080") and ext in ("mp4", "mov", "webm", "mkv"):
                        target = folder / f"scaled-{index}.mp4"
                        result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-protocol_whitelist",
                                                 "file,pipe", "-f", DEMUXERS[ext], "-i", str(path), "-vf",
                                                 f"scale=-2:min(ih\\,{quality})", "-threads", "2",
                                                 "-c:v", "libx264", "-preset", "veryfast",
                                                 "-c:a", "aac", "-fs", str(remaining + 1), str(target)],
                                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                                timeout=120, check=False)
                        if result.returncode or not target.is_file():
                            target.unlink(missing_ok=True)
                            return None, "quality"
                        path.unlink()
                        return target, ""
                    return path, ""
                return None, "unsupported"
            finally:
                if not received or received > remaining:
                    path.unlink(missing_ok=True)
    return None, "unsupported"


async def browser_media(url):
    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            proxy={"server": PROXY}, args=["--disable-quic", "--disable-background-networking",
                                          "--proxy-bypass-list=<-loopback>"])
        try:
            context = await browser.new_context(accept_downloads=False, service_workers="block")
            await context.route("**/*", lambda route: route.abort() if route.request.resource_type
                                in {"image", "media", "font"} else route.continue_())
            page = await context.new_page()
            response = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
            if not response or response.status >= 400:
                return []
            return await page.evaluate("""() => {
                const values = [...document.querySelectorAll(
                    'meta[property="og:video:url"],meta[property="og:video:secure_url"],' +
                    'meta[property="og:audio"],meta[property="og:audio:secure_url"],' +
                    'link[rel="image_src"]'
                )].map(e => e.content || e.href);
                if (document.querySelector('meta[property="og:type"][content="image"]')) {
                    values.push(...[...document.querySelectorAll('meta[property="og:image"]')].map(e => e.content));
                }
                for (const e of document.querySelectorAll('article video, article audio, article video source, article audio source')) {
                    values.push(e.currentSrc || e.src);
                }
                return [...new Set(values.filter(v => typeof v === 'string' && /^https?:/i.test(v)))].slice(0, 20);
            }""")
        finally:
            await browser.close()


async def gallery_media(url):
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "gallery_dl", "--config-ignore", "--proxy", PROXY,
        "--range", f"1-{MAX_ITEMS}", "--child-range", f"1-{MAX_ITEMS}",
        "-o", "extractor.timeout=12", "-o", "extractor.retries=1", "-g", "--", url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    output, _ = await process.communicate()
    return [line for line in output.decode(errors="replace").splitlines()
            if line.startswith(("https://", "http://"))][:MAX_ITEMS]


async def save_candidates(client, urls, folder, quality, start_index):
    remaining = MAX_BYTES
    count = 0
    for index, candidate in enumerate(dict.fromkeys(urls), start_index):
        try:
            path, _ = await direct(client, candidate, folder, index, remaining, quality)
        except (httpx.RequestError, ValueError):
            continue
        if path:
            count += 1
            remaining -= path.stat().st_size
            print(path, flush=True)
            if count >= (MAX_ITEMS if quality == "album" else 1):
                break
    return count


async def extract(url, quality, folder):
    if quality not in QUALITIES:
        return "unsupported"
    url = public_url(url)
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BYTES * 2, MAX_BYTES * 2))
    files, reason = await ytdlp(url, quality, folder)
    direct_video = Path(urlsplit(url).path).suffix.lower() in {".mp4", ".mov", ".webm", ".mkv"}
    # A generic extractor can reject a format even when the original media URL is public.
    retry_direct = direct_video and quality in ("480", "720", "1080")
    if reason == "protected":
        return reason
    if not files and (reason == "too_large" or (reason == "inaccessible" and not retry_direct)):
        return reason
    async with httpx.AsyncClient(proxy=PROXY, timeout=12, trust_env=False,
                                 verify=ssl.create_default_context(),
                                 headers={"Accept-Encoding": "identity"}) as client:
        if quality == "album" and len(files) <= 1 and reason != "inaccessible":
            if await save_candidates(client, await gallery_media(url), folder, quality, 100):
                return ""
        if files:
            for path in files:
                print(path, flush=True)
            return ""
        try:
            path, direct_reason = await direct(client, url, folder, 1, MAX_BYTES, quality)
        except (httpx.RequestError, ValueError):
            path, direct_reason = None, "temporary"
        if path:
            print(path, flush=True)
            return ""
        if direct_reason in {"too_large", "inaccessible", "quality"}:
            return direct_reason
        if reason == "inaccessible":
            return reason
        if reason == "quality":
            return reason
        if quality != "album" and await save_candidates(client, await gallery_media(url), folder, quality, 100):
            return ""
        if os.getenv("ENABLE_BROWSER", "1") == "1":
            from playwright.async_api import Error as BrowserError
            try:
                urls = await browser_media(url)
            except BrowserError:
                urls = []
            if await save_candidates(client, urls, folder, quality, 200):
                return ""
    return "temporary" if "temporary" in (reason, direct_reason) else reason


async def main():
    url, quality, target = sys.argv[1:]
    folder = Path(target)
    reason = await extract(url, quality, folder)
    if reason:
        print(reason, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
