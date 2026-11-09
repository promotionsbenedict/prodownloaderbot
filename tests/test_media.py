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


def test_signed_file_serving_and_range(tmp_path, monkeypatch):
    import time

    from fastapi.testclient import TestClient

    from prodownloader import api
    from prodownloader.security import signed_path

    name = "a" * 32 + ".mp4"
    (tmp_path / name).write_bytes(b"0123456789")
    monkeypatch.setattr(api, "MEDIA", tmp_path)
    monkeypatch.setattr(api, "PUBLIC_BASE", "https://files.example.com")
    monkeypatch.setattr(api, "SIGNING_KEY", "test-only")
    client = TestClient(api.app)
    expiry = int(time.time()) + 30
    path = signed_path("test-only", name, expiry)
    result = client.get(path, headers={"Range": "bytes=2-4"})
    assert result.status_code == 206 and result.content == b"234"
    assert result.headers["cache-control"] == "private, no-store"
    assert client.head(path).status_code == 200
    assert client.get(signed_path("test-only", name, expiry - 60)).status_code == 404
    assert client.get(signed_path("invalid", name, expiry)).status_code == 404


def test_capped_direct_video_recovers_from_extractor_access_error(tmp_path, capsys):
    url = "https://example.com/sample.mp4"
    target = tmp_path / "scaled-1.mp4"
    with patch.object(adapters.resource, "setrlimit"), patch.object(
            adapters, "ytdlp", AsyncMock(return_value=([], "inaccessible"))), patch.object(
            adapters, "direct", AsyncMock(return_value=(target, ""))) as direct:
        assert asyncio.run(adapters.extract(url, "480", tmp_path)) == ""
        assert direct.await_args.args[1:] == (url, tmp_path, 1, adapters.MAX_BYTES, "480")
        assert capsys.readouterr().out.strip() == str(target)
        direct.reset_mock()
        assert asyncio.run(adapters.extract("https://example.com/post", "480", tmp_path)) == "inaccessible"
        direct.assert_not_awaited()


@pytest.mark.parametrize(("location", "normalized"), [
    ("/story.php?story_fbid=1036787588820442&id=100055189422916",
     "https://www.facebook.com/watch/?v=1036787588820442"),
    ("https://example.com/story.php?story_fbid=1036787588820442", None),
    ("/login/?next=story.php", None),
])
def test_facebook_share_video_normalization(location, normalized):
    url = "https://www.facebook.com/share/v/1DejwDxrT5/"

    async def check():
        transport = httpx.MockTransport(lambda _: httpx.Response(302, headers={"location": location}))
        async with httpx.AsyncClient(transport=transport) as client:
            assert await adapters.facebook_video_url(client, url) == (normalized or url)

    asyncio.run(check())


def test_instagram_embed_uses_public_post_image():
    url = "https://www.instagram.com/p/example/?tracking=1"
    photo = "https://scontent.example.cdninstagram.com/post.jpg"

    def serve(request):
        assert str(request.url) == "https://www.instagram.com/p/example/embed/"
        return httpx.Response(200, text=f"""
            <img class="EmbeddedMediaImage" src="https://example.com/not-instagram.jpg">
            <img class="ProfileImage" src="https://scontent.example.cdninstagram.com/profile.jpg">
            <img class="EmbeddedMediaImage" src="{photo}">
        """)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            assert await adapters.instagram_embed_photo(client, url) == [photo]

    asyncio.run(check())


def test_telegram_embed_uses_only_requested_post_attachments():
    photo = "https://cdn4.cdn-telegram.org/file/photo.jpg"
    video = "https://cdn4.telesco.pe/file/video.mp4"

    def serve(request):
        assert str(request.url) == "https://t.me/officialproreact/3?embed=1"
        return httpx.Response(200, text=f"""
            <div data-post="officialproreact/2">
              <video class="tgme_widget_message_video" src="{video}?other=1"></video>
            </div>
            <div data-post="officialproreact/3">
              <a class="tgme_widget_message_photo_wrap" style="background-image:url('{photo}')"></a>
              <div><video class="tgme_widget_message_video" src="{video}"></video></div>
            </div>
        """)

    async def check():
        async with httpx.AsyncClient(transport=httpx.MockTransport(serve)) as client:
            assert await adapters.telegram_embed_media(client, "https://t.me/s/officialproreact/3") == [photo, video]
            assert await adapters.telegram_embed_media(client, "https://t.me/c/1234/3") == []

    asyncio.run(check())
