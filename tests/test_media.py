import asyncio
import shutil
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from prodownloader import adapters, bot
from prodownloader.security import public_url

PNG = b"\x89PNG\r\n\x1a\nexample"


def test_photo_redirect_and_size(tmp_path):
    def serve(request):
        if request.url.path == "/redirect":
            return httpx.Response(302, headers={"location": "/photo"})
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            path, reason = await adapters.direct(client, "https://example.com/redirect", tmp_path, 0, 100, "original")
            assert not reason and path.read_bytes() == PNG
            assert await adapters.direct(client, "https://example.com/photo", tmp_path, 1, 5, "original") == (
                None, "too_large")
    asyncio.run(check())


def test_redirect_to_local_address_is_rejected(tmp_path):
    def serve(request):
        assert request.url.host == "example.com"
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            with pytest.raises(ValueError):
                await adapters.direct(client, "https://example.com/post", tmp_path, 0, 100, "original")
    asyncio.run(check())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required")
@pytest.mark.parametrize(("quality", "ext"), [("mp3", ".mp3"), ("480", ".mp4")])
def test_real_ffmpeg_conversion(tmp_path, quality, ext):
    sample = tmp_path / "input.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=960x540:rate=5",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "1",
                    "-c:v", "libx264", "-c:a", "aac", str(sample)], check=True)
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, headers={"content-type": "video/mp4"}, content=sample.read_bytes()))

    async def check():
        async with httpx.AsyncClient(transport=transport) as client:
            path, reason = await adapters.direct(client, "https://example.com/post", tmp_path, 0, 500000, quality)
            assert not reason and path.suffix == ext and path.stat().st_size > 0
            if quality == "480":
                height = subprocess.check_output(["ffprobe", "-v", "error", "-select_streams", "v:0",
                                                  "-show_entries", "stream=height", "-of", "csv=p=0", str(path)])
                assert height.strip() == b"480"
    asyncio.run(check())


def test_album_adapter_and_protected_source(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ENABLE_BROWSER", "1")

    async def check():
        @asynccontextmanager
        async def client(*args, **kwargs):
            yield None

        async def direct(_, url, folder, index, remaining, quality):
            if url.endswith("/post"):
                return None, "unsupported"
            path = folder / f"media-{index}.png"
            path.write_bytes(PNG)
            return path, ""

        with patch.object(adapters.httpx, "AsyncClient", client), patch.object(
                adapters, "ytdlp", AsyncMock(return_value=([], "unsupported"))), patch.object(
                adapters, "direct", direct), patch.object(adapters, "gallery_media", AsyncMock(return_value=[])), patch.object(
                adapters, "browser_media", AsyncMock(return_value=["https://example.com/1", "https://example.com/2"])):
            assert await adapters.extract("https://example.com/post", "album", tmp_path) == ""
            assert len(capsys.readouterr().out.splitlines()) == 2
        with patch.object(adapters, "ytdlp", AsyncMock(return_value=([], "protected"))), patch.object(
                adapters, "browser_media", AsyncMock()) as browser:
            assert await adapters.extract("https://example.com/post", "album", tmp_path) == "protected"
            browser.assert_not_awaited()

    # Resource limits are applied only in the actual extraction subprocess.
    with patch.object(adapters.resource, "setrlimit"):
        asyncio.run(check())


def test_commands_and_platform_names():
    assert {cmd.command for cmd in bot.COMMANDS} == {"start", "help", "settings", "history", "status", "support"}
    assert bot.platform_for(public_url("https://youtu.be/123")) == "YouTube"
    assert bot.platform_for(public_url("https://youtube.com.example.com/123")) == "Other website"


def test_telegram_registration(monkeypatch):
    from types import SimpleNamespace

    telegram = AsyncMock()
    telegram.get_me.return_value = SimpleNamespace(username="prodownloadersbot")
    telegram.get_my_commands.return_value = bot.COMMANDS
    monkeypatch.delenv("BOT_USERNAME", raising=False)
    asyncio.run(bot.configure(telegram))
    telegram.set_my_commands.assert_awaited_once_with(bot.COMMANDS)
    telegram.set_chat_menu_button.assert_awaited_once()
    telegram.delete_webhook.assert_awaited_once_with(drop_pending_updates=False)


def test_extractor_deadline_terminates_subprocess(tmp_path):
    import sys

    from prodownloader import extractor

    async def check():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(30)",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True)
        request = AsyncMock()
        request.is_disconnected.return_value = True
        assert await extractor.wait_for_download(process, request, Path(tmp_path), 100) == ([], "cancelled")
        assert process.returncode < 0

    asyncio.run(check())


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is required")
def test_ytdlp_unencrypted_hls_through_test_proxy(tmp_path, monkeypatch):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    from urllib.parse import urlsplit

    from prodownloader import adapters

    source = tmp_path / "source"
    target = tmp_path / "output"
    source.mkdir()
    target.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=size=128x72:rate=10",
                    "-t", "1", "-c:v", "libx264", "-f", "hls", "-hls_playlist_type", "vod",
                    str(source / "sample.m3u8")], check=True)

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            name = Path(urlsplit(self.path).path).name
            payload = (source / name).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/vnd.apple.mpegurl" if name.endswith(".m3u8")
                             else "video/mp2t")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        monkeypatch.setattr(adapters, "PROXY", f"http://127.0.0.1:{server.server_port}")
        try:
            paths, reason = asyncio.run(adapters.ytdlp("http://example.com/sample.m3u8", "original", target))
            assert paths, reason
            assert paths[0].stat().st_size > 0
        finally:
            server.shutdown()
            thread.join()
