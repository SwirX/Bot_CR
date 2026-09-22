"""Deezer provider: full-length mainstream catalog via an ARL session cookie.

Deezer CDN files are BF-CBC "stripe"-encrypted, so the provider decrypts the
stream to a local file while downloading and hands the Music cog a local
path instead of a URL — ffmpeg cannot undo the striping itself.
"""

import os
import tempfile
import time
from pathlib import Path

import aiohttp

from cogs.music_sources.deezer_gw import GwLightClient
from cogs.music_sources.model import Playable, SourceUnavailable

ARL_ENV = "DEEZER_ARL"
_CACHE_DIR = Path(tempfile.gettempdir()) / "botcr-deezer"
_CACHE_MAX_AGE_SECONDS = 3600


def _sweep_cache() -> None:
    """Drop decrypted files older than the cap so the temp dir stays bounded."""
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cutoff = time.time() - _CACHE_MAX_AGE_SECONDS
    for stale in _CACHE_DIR.glob("track-*"):
        try:
            if stale.stat().st_mtime < cutoff:
                stale.unlink()
        except OSError:
            pass


def _artists_of(track: dict) -> str:
    entries = track.get("ARTISTS")
    if isinstance(entries, list):
        names = [entry.get("ART_NAME") for entry in entries if entry.get("ART_NAME")]
        return ", ".join(names)
    return str(track.get("ART_NAME") or "")


async def resolve(query: str) -> Playable:
    """Search Deezer for the query and deliver a decrypted local audio file."""
    if "://" in query:
        raise SourceUnavailable(
            "deezer", "bad_url", "Deezer searches by title, not by URL.")
    arl = (os.environ.get(ARL_ENV) or "").strip()
    if not arl:
        raise SourceUnavailable(
            "deezer", "no_config",
            "DEEZER_ARL is not set — the Deezer source is disabled.")
    _sweep_cache()
    try:
        async with aiohttp.ClientSession() as session:
            client = GwLightClient(arl, session)
            await client.login()
            tracks = await client.search(query)
            if not tracks:
                raise SourceUnavailable(
                    "deezer", "not_found",
                    f"No Deezer results for {query!r}.")
            top = tracks[0]
            track_id = int(top["SNG_ID"])
            stream_url = await client.stream_url(track_id, client.format)
            extension = "flac" if client.format == "FLAC" else "mp3"
            destination = _CACHE_DIR / f"track-{track_id}.{extension}"
            await client.download_decrypted(stream_url, track_id, str(destination))
    except SourceUnavailable:
        raise
    except aiohttp.ClientError as exc:
        raise SourceUnavailable(
            "deezer", "api_error", f"Deezer unreachable: {exc}") from exc
    except (KeyError, TypeError, ValueError) as exc:
        raise SourceUnavailable(
            "deezer", "api_error",
            f"Unexpected Deezer payload: {exc}") from exc
    picture = top.get("ALB_PICTURE") or ""
    return Playable(
        provider="deezer",
        title=top.get("SNG_TITLE") or query,
        stream_url=stream_url,
        local_path=str(destination),
        artist=_artists_of(top),
        webpage_url=f"https://www.deezer.com/track/{track_id}",
        duration=int(top["DURATION"]) if top.get("DURATION") else None,
        thumbnail=(f"https://e-cdn-images.dzcdn.net/images/cover/{picture}/"
                   "250x250-000000-80-0-0.jpg") if picture else "",
    )