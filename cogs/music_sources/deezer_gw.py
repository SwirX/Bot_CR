"""Deezer grey-web API client + BF-CBC "stripe" stream decryption.

The ARL session cookie authenticates the grey-web (gw-light) API, the only
surface that yields full-length track tokens. CDN files are Blowfish-CBC
"stripe"-encrypted: every third 2048-byte chunk only, with a fresh cipher per
chunk and a key derived from the track id.

Flow mirrors the maintained reference client (orpheusdl-deezer, MIT):
getUserData -> api_token (checkForm) + license_token; song.getData for the
per-track TRACK_TOKEN; media.deezer.com/v1/get_url exchanges it for a CDN URL.
"""

import hashlib
import logging
from random import randint
from typing import AsyncIterable, AsyncIterator

import aiohttp

from cogs.music_sources.model import SourceUnavailable

LOG = logging.getLogger("music.deezer_gw")

try:
    from Crypto.Cipher import Blowfish
except ImportError:  # pragma: no cover - exercised at deploy time
    Blowfish = None

GW_LIGHT_URL = "https://www.deezer.com/ajax/gw-light.php"
MEDIA_URL = "https://media.deezer.com/v1/get_url"
# Static constant of Deezer's client-side striping scheme, shared by every
# open-source Deezer downloader (Deemix/Freezer/orpheusdl).
_BF_SECRET = b"g4el58wc0zvf9na1"
_BF_IV = b"\x00\x01\x02\x03\x04\x05\x06\x07"
CHUNK_BYTES = 2048
_FORMAT_PREFERENCE = ("FLAC", "MP3_320", "MP3_128")

_HEADERS = {
    "accept": "*/*",
    "origin": "https://www.deezer.com",
    "referer": "https://www.deezer.com/",
    "sec-fetch-mode": "same-origin",
    "sec-fetch-site": "same-origin",
    "content-type": "text/plain;charset=UTF-8",
    "user-agent": (
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}


def blowfish_key(track_id: int) -> bytes:
    """Stripe key: ASCII md5-hex bytes of the id xor'ed with the secret."""
    digest = hashlib.md5(str(track_id).encode()).hexdigest().encode("ascii")
    return bytes(digest[i] ^ digest[i + 16] ^ _BF_SECRET[i] for i in range(16))


async def decrypt_chunks(
        chunks: AsyncIterable[bytes], track_id: int) -> AsyncIterator[bytes]:
    """Decrypt every third full 2048-byte chunk; pass everything else through.

    Only safe when every item is already aligned to the file's 2048-byte
    stripe (e.g. slicing an in-memory download). For network streams use
    :func:`decrypt_stream`, which is immune to transport chunk sizes.
    """
    if Blowfish is None:
        raise SourceUnavailable(
            "deezer", "missing_dep",
            "pycryptodome is not installed — run the bot from its venv.")
    key = blowfish_key(track_id)
    index = 0
    async for chunk in chunks:
        if index % 3 == 0 and len(chunk) == CHUNK_BYTES:
            # Blowfish state must reset per chunk; the IV is fixed.
            cipher = Blowfish.new(key, Blowfish.MODE_CBC, _BF_IV)
            chunk = cipher.decrypt(chunk)
        index += 1
        yield chunk


async def decrypt_stream(
        reader: "aiohttp.StreamReader", track_id: int) -> AsyncIterator[bytes]:
    """Decrypt a striped CDN stream regardless of transport chunking.

    The stripe is defined in file byte offsets: every third 2048-byte block
    counted from the start of the file. ``iter_chunked`` yields whatever the
    TCP buffer currently holds, so transport parcel sizes must not leak into
    the cipher — accumulate raw bytes here and slice strictly on 2048-byte
    boundaries. That keeps ``index % 3`` aligned with the file for the whole
    download (a drifted index scrambled everything past the first short
    chunk, the cause of the 128k stutter).
    """
    if Blowfish is None:
        raise SourceUnavailable(
            "deezer", "missing_dep",
            "pycryptodome is not installed — run the bot from its venv.")
    key = blowfish_key(track_id)
    buf = b""
    index = 0
    while True:
        if len(buf) < CHUNK_BYTES:
            piece = await reader.read(CHUNK_BYTES - len(buf))
            if not piece:
                break
            buf += piece
            continue
        block = buf[:CHUNK_BYTES]
        buf = buf[CHUNK_BYTES:]
        if index % 3 == 0:
            # Blowfish state must reset per chunk; the IV is fixed.
            cipher = Blowfish.new(key, Blowfish.MODE_CBC, _BF_IV)
            block = cipher.decrypt(block)
        index += 1
        yield block
    if buf:
        yield buf


class GwLightClient:
    """Authenticated gateway to Deezer's grey-web API."""

    _TOKENLESS_METHODS = ("deezer.getUserData",)

    def __init__(self, arl: str, session: aiohttp.ClientSession):
        self._arl = arl
        self._session = session
        self.api_token = ""
        self.license_token = ""
        self.format = "MP3_128"

    async def _call(self, method: str, payload: dict) -> dict:
        params = {
            "method": method,
            "input": 3,
            "api_version": 1.0,
            "api_token": "" if method in self._TOKENLESS_METHODS else self.api_token,
            "cid": randint(0, 1_000_000_000),
        }
        try:
            async with self._session.post(
                    GW_LIGHT_URL, params=params, json=payload,
                    headers=_HEADERS, cookies={"arl": self._arl},
                    timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status >= 400:
                    raise SourceUnavailable(
                        "deezer", "api_error",
                        f"Deezer gw-light returned HTTP {response.status}.")
                body = await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise SourceUnavailable(
                "deezer", "api_error", f"Deezer unreachable: {exc}") from exc
        if body.get("error"):
            raise SourceUnavailable(
                "deezer", "api_error",
                f"Deezer gw-light {method} failed: {body['error']}")
        return body.get("results") or {}

    async def login(self) -> None:
        data = await self._call("deezer.getUserData", {})
        user = data.get("USER") or {}
        if not user.get("USER_ID"):
            raise SourceUnavailable(
                "deezer", "auth_failed",
                "DEEZER_ARL is invalid or expired — re-export it from deezer.com.")
        self.api_token = data.get("checkForm") or ""
        options = user.get("OPTIONS") or {}
        self.license_token = options.get("license_token") or ""
        for candidate in _FORMAT_PREFERENCE:
            flag = {"FLAC": "web_lossless", "MP3_320": "web_hq"}.get(candidate)
            if flag is None or options.get(flag):
                self.format = candidate
                break
        LOG.info(
            "Deezer session ready: format=%s (hq=%s lossless=%s)",
            self.format, bool(options.get("web_hq")),
            bool(options.get("web_lossless")))

    async def search(self, query: str, limit: int = 20) -> list[dict]:
        data = await self._call("search.music", {
            "query": query, "start": 0, "nb": limit,
            "filter": "ALL", "output": "TRACK"})
        return data.get("data") or []

    async def stream_url(self, track_id: int, format: str) -> str:
        token = (await self._call("song.getData", {
            "sng_id": track_id, "array_default": ["TRACK_TOKEN"]})).get("TRACK_TOKEN")
        if not token:
            raise SourceUnavailable(
                "deezer", "no_stream",
                "Deezer issued no stream token for that track.")
        payload = {
            "license_token": self.license_token,
            "media": [{"type": "FULL",
                       "formats": [{"cipher": "BF_CBC_STRIPE", "format": format}]}],
            "track_tokens": [token],
        }
        try:
            async with self._session.post(
                    MEDIA_URL, json=payload, headers=_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status >= 400:
                    raise SourceUnavailable(
                        "deezer", "api_error",
                        f"Deezer media API returned HTTP {response.status}.")
                body = await response.json(content_type=None)
        except aiohttp.ClientError as exc:
            raise SourceUnavailable(
                "deezer", "api_error", f"Deezer unreachable: {exc}") from exc
        try:
            source = body["data"][0]["media"][0]["sources"][0]
        except (KeyError, IndexError, TypeError):
            raise SourceUnavailable(
                "deezer", "no_stream", "Deezer returned no media source.")
        return source["url"]

    async def download_decrypted(
            self, url: str, track_id: int, destination: str) -> None:
        """Stream the striped CDN file and write its decrypted audio locally."""
        try:
            async with self._session.get(
                    url, timeout=aiohttp.ClientTimeout(total=300)) as response:
                if response.status >= 400:
                    raise SourceUnavailable(
                        "deezer", "api_error",
                        f"Deezer CDN returned HTTP {response.status}.")
                with open(destination, "wb") as output:
                    async for chunk in decrypt_stream(response.content, track_id):
                        output.write(chunk)
        except aiohttp.ClientError as exc:
            raise SourceUnavailable(
                "deezer", "api_error", f"Deezer unreachable: {exc}") from exc