import asyncio
import ipaddress
import time
from unittest.mock import AsyncMock, patch

import pytest

from prodownloader import proxy
from prodownloader.security import (
    allowed_address,
    public_url,
    signed_path,
    valid_signature,
)


@pytest.mark.parametrize("url", [
    "file:///etc/passwd", "http://localhost", "http://internal.local", "https://127.0.0.1",
    "http://2130706433", "http://[::1]", "http://example.com:22", "http://user@example.com",
    "http://user:pass@example.com", "http://example.com\\@other.com", "http://example.com/\r\n",
])
def test_reject_unsafe_urls(url):
    with pytest.raises(ValueError):
        public_url(url)


@pytest.mark.parametrize("ip", [
    "127.0.0.1", "169.254.169.254", "10.0.0.1", "100.64.0.1", "224.1.2.3",
    "::ffff:8.8.8.8", "fc00::1", "fe80::1", "2002:7f00:1::", "ff02::1",
])
def test_reject_unsafe_addresses(ip):
    assert not allowed_address(ip)


def test_normalize_and_expire():
    assert public_url("https://WWW.Example.com:443/a?id=1#page") == "https://www.example.com/a?id=1"
    assert allowed_address("8.8.8.8")
    assert not allowed_address("8.8.8.8", [ipaddress.ip_network("8.8.8.0/24")])
    expiry = int(time.time()) + 60
    filename = "a" * 32 + ".mp4"
    path = signed_path("secret", filename, expiry)
    sig = path.split("/")[-1]
    assert valid_signature("secret", filename, expiry, sig)
    assert not valid_signature("wrong", filename, expiry, sig)
    assert not valid_signature("secret", filename, expiry - 1, sig)
    assert not valid_signature("secret", filename, 1, sig)
    assert not valid_signature("secret", "../" + filename, expiry, sig)


def test_proxy_pins_address_and_rejects_mixed_results():
    async def check():
        loop = asyncio.get_running_loop()
        public = (0, 0, 0, "", ("8.8.8.8", 443))
        private = (0, 0, 0, "", ("10.0.0.1", 443))
        dial = AsyncMock(return_value=("reader", "writer"))
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[public])), patch(
                "prodownloader.proxy.asyncio.open_connection", dial):
            assert await proxy.connect("example.com", 443) == ("reader", "writer")
            dial.assert_awaited_once_with("8.8.8.8", 443)
        dial.reset_mock()
        with patch.object(loop, "getaddrinfo", AsyncMock(return_value=[public, private])), patch(
                "prodownloader.proxy.asyncio.open_connection", dial):
            with pytest.raises(ValueError):
                await proxy.connect("example.com", 443)
            dial.assert_not_called()
    asyncio.run(check())
