"""Deezer provider: full-length mainstream catalog via an ARL session cookie.

Deezer CDN files are BF-CBC "stripe"-encrypted, so the provider decrypts the
stream to a local file while downloading and hands the Music cog a local
path instead of a URL — ffmpeg cannot undo the striping itself.
"""

import asyncio
import os
import tempfile
import time
from pathlib import Path

import aiohttp

from cogs.music_sources import deezer_radio
from cogs.music_sources.deezer_gw import GwLightClient
from cogs.music_sources.model import Candidate, Playable, SourceUnavailable

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


def _session_arl() -> str:
    """The configured ARL, or a clean 'source disabled' failure."""
    arl = (os.environ.get(ARL_ENV) or "").strip()
    if not arl:
        raise SourceUnavailable(
            "deezer", "no_config",
            "DEEZER_ARL is not set — the Deezer source is disabled.")
    return arl


async def _build_playable(client: GwLightClient, track: dict) -> Playable | None:
    """Trade one search/radio hit for a decrypted local audio file.

    Returns ``None`` when the track can't be streamed (e.g. regional blocks),
    so a radio sweep can skip the bad apples and keep the rest.
    """
    try:
        track_id = int(track["SNG_ID"])
        stream_url = await client.stream_url(track_id, client.format)
        extension = "flac" if client.format == "FLAC" else "mp3"
        destination = _CACHE_DIR / f"track-{track_id}.{extension}"
        await client.download_decrypted(stream_url, track_id, str(destination))
    except SourceUnavailable:
        return None
    except aiohttp.ClientError:
        return None
    except (KeyError, TypeError, ValueError):
        return None
    picture = track.get("ALB_PICTURE") or ""
    return Playable(
        provider="deezer",
        title=track.get("SNG_TITLE") or "",
        stream_url=stream_url,
        local_path=str(destination),
        artist=_artists_of(track),
        webpage_url=f"https://www.deezer.com/track/{track_id}",
        duration=int(track["DURATION"]) if track.get("DURATION") else None,
        thumbnail=(f"https://e-cdn-images.dzcdn.net/images/cover/{picture}/"
                   "250x250-000000-80-0-0.jpg") if picture else "",
    )


async def candidates(query: str, limit: int = 5) -> list[Candidate]:
    """Metadata-only search hits for the picker — no download/decrypt yet.

    The picker needs titles, artists and durations before anyone commits to a
    stream; paying the decrypt round-trip for five tracks just to list them
    would also hammer the Deezer CDN. The full track dict is kept on each
    Candidate's payload so :func:`materialize` can stream the pick.
    """
    if "://" in query:
        return []
    _sweep_cache()
    try:
        async with aiohttp.ClientSession() as session:
            client = GwLightClient(_session_arl(), session)
            await client.login()
            tracks = await client.search(query, limit=limit)
    except SourceUnavailable:
        raise
    except aiohttp.ClientError as exc:
        raise SourceUnavailable(
            "deezer", "api_error", f"Deezer unreachable: {exc}") from exc
    return [_track_to_candidate(t) for t in tracks[:limit] if t.get("SNG_ID")]


def _track_to_candidate(track: dict) -> Candidate:
    picture = track.get("ALB_PICTURE") or ""
    track_id = track.get("SNG_ID")
    return Candidate(
        provider="deezer",
        title=track.get("SNG_TITLE") or "",
        artist=_artists_of(track),
        duration=int(track["DURATION"]) if track.get("DURATION") else None,
        webpage_url=f"https://www.deezer.com/track/{track_id}",
        thumbnail=(f"https://e-cdn-images.dzcdn.net/images/cover/{picture}/"
                   "250x250-000000-80-0-0.jpg") if picture else "",
        payload=track,
    )


async def materialize(candidate: Candidate) -> Playable:
    """Turn a picked candidate into a streamable Playable (download+decrypt)."""
    if candidate.provider != "deezer" or not candidate.payload:
        raise SourceUnavailable(
            "deezer", "bad_candidate", "Not a Deezer candidate.")
    _sweep_cache()
    try:
        async with aiohttp.ClientSession() as session:
            client = GwLightClient(_session_arl(), session)
            await client.login()
            playable = await _build_playable(client, candidate.payload)
    except SourceUnavailable:
        raise
    except aiohttp.ClientError as exc:
        raise SourceUnavailable(
            "deezer", "api_error", f"Deezer unreachable: {exc}") from exc
    if playable is None:
        raise SourceUnavailable(
            "deezer", "no_stream",
            f"Couldn't fetch a stream for {candidate.title!r}.")
    return playable


async def resolve(query: str) -> Playable:
    """Search Deezer for the query and deliver a decrypted local audio file."""
    if "://" in query:
        raise SourceUnavailable(
            "deezer", "bad_url", "Deezer searches by title, not by URL.")
    _sweep_cache()
    try:
        async with aiohttp.ClientSession() as session:
            client = GwLightClient(_session_arl(), session)
            await client.login()
            tracks = await client.search(query)
            if not tracks:
                raise SourceUnavailable(
                    "deezer", "not_found",
                    f"No Deezer results for {query!r}.")
            playable = await _build_playable(client, tracks[0])
    except SourceUnavailable:
        raise
    except aiohttp.ClientError as exc:
        raise SourceUnavailable(
            "deezer", "api_error", f"Deezer unreachable: {exc}") from exc
    if playable is None:
        raise SourceUnavailable(
            "deezer", "no_stream",
            f"Couldn't fetch a stream for {query!r}.")
    return playable


async def radio_tracks(seed: str, limit: int = 8) -> list[Playable]:
    """Similar-track radio around a title, each delivered decrypted locally.

    Used by ``/queue auto``. gw-light's ``song.getRadio`` is dead
    (GATEWAY_ERROR on every payload), so the feed comes from
    api.deezer.com's artist radio instead and streams through the same
    ARL + BF_CBC_STRIPE path.
    """
    _sweep_cache()
    async with aiohttp.ClientSession() as session:
        client = GwLightClient(_session_arl(), session)
        await client.login()
        hits = await client.search(seed)
        if not hits:
            return []
        seed_id = int(hits[0]["SNG_ID"])
        related = await deezer_radio.radio_track_dicts(session, seed_id, limit)
        gate = asyncio.Semaphore(4)  # gw-light dislikes bursts

        async def _worker(track: dict) -> Playable | None:
            async with gate:
                return await _build_playable(client, track)

        results = await asyncio.gather(
            *(asyncio.ensure_future(_worker(t)) for t in related),
            return_exceptions=True)
    return [r for r in results if isinstance(r, Playable) and r.local_path]