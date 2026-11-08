import hashlib
import hmac
import ipaddress
import re
import time
from urllib.parse import urlsplit, urlunsplit

DOMAIN = re.compile(r"(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?", re.I)
FILENAME = re.compile(r"[a-f0-9]{32}\.[a-z0-9]{2,5}")
QUALITIES = {"original": "Original / best", "1080": "Up to 1080p", "720": "Up to 720p",
             "480": "Up to 480p", "mp3": "MP3", "album": "All media / playlist"}
MEDIA_TYPES = {
    "image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "image/gif": "gif",
    "video/mp4": "mp4", "video/webm": "webm", "video/quicktime": "mov",
    "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/ogg": "ogg",
    "audio/wav": "wav", "audio/flac": "flac", "video/x-matroska": "mkv",
}
EXTENSIONS = set(MEDIA_TYPES.values()) | {"m4v", "opus", "aac", "m4a", "oga", "3gp", "ts", "flv"}


def hostname(value):
    host = value.rstrip(".").encode("idna").decode("ascii").lower()
    if not DOMAIN.fullmatch(host) or host.endswith((".local", ".localhost", ".internal", ".test", ".onion")):
        raise ValueError("Public DNS hostname required")
    return host


def public_url(value):
    if len(value) > 2048 or any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in value):
        raise ValueError("Malformed URL")
    parsed = urlsplit(value)
    ports = {"https": 443, "http": 80}
    if (parsed.scheme not in ports or not parsed.hostname or parsed.username is not None
            or parsed.port not in (None, ports[parsed.scheme])):
        raise ValueError("Use a public HTTP or HTTPS link")
    host = hostname(parsed.hostname)
    return urlunsplit((parsed.scheme, host, parsed.path or "/", parsed.query, ""))


def allowed_address(value, denied=()):
    ip = ipaddress.ip_address(value)
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped or ip.sixtofour or ip.teredo or ip not in ipaddress.ip_network("2000::/3"):
            return False
    return ip.is_global and not ip.is_multicast and not any(ip in network for network in denied)


def signed_path(secret, key, expiry):
    mac = hmac.new(secret.encode(), f"{key}:{expiry}".encode(), hashlib.sha256).hexdigest()
    return f"/files/{key}/{expiry}/{mac}"


def valid_signature(secret, key, expiry, supplied):
    if not secret or not FILENAME.fullmatch(key) or expiry <= time.time() or len(supplied) != 64:
        return False
    return hmac.compare_digest(signed_path(secret, key, expiry).rsplit("/", 1)[1], supplied)
