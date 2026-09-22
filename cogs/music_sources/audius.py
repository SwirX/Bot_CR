"""Audius provider: free, keyless, full-length track streams via the public API.

The fallback source for cloud-hosted bots: no cookies or keys are required and
tracks stream from Audius' own CDN, so the YouTube bot-blocks that datacenter
IPs hit do not apply. Catalog skews indie/electronic rather than top-40.
"""

import aiohttp

from cogs.music_sources.model import Candidate, Playable, SourceUnavailable

DISCOVERY = "https://discoveryprovider.audius.co"
APP_NAME = "botcr"


async def resolve(query: str) -> Playable:
    """Search Audius for the query and resolve its best match to a stream URL."""
    if "://" in query:
        raise SourceUnavailable(
            "audius", "bad_url",
            "Audius searches by track title, not by URL.")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                    f"{DISCOVERY}/v1/tracks/search",
                    params={"query": query, "app_name": APP_NAME},
                    timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status >= 400:
                    raise SourceUnavailable(
                        "audius", "api_error",
                        f"Audius search returned HTTP {response.status}.")
                body = await response.json(content_type=None)
            tracks = body.get("data") or []
            if not tracks:
                raise SourceUnavailable(
                    "audius", "not_found",
                    f"No Audius tracks for {query!r}.")
            top = tracks[0]
            track_id = top.get("id")
            async with session.get(
                    f"{DISCOVERY}/v1/tracks/{track_id}/stream",
                    params={"app_name": APP_NAME},
                    timeout=aiohttp.ClientTimeout(total=30)) as stream_response:
                if stream_response.status >= 400:
                    raise SourceUnavailable(
                        "audius", "no_stream",
                        "Audius refused playback for the best match.")
                stream_url = str(stream_response.url)
    except aiohttp.ClientError as exc:
        raise SourceUnavailable(
            "audius", "api_error",
            f"Audius unreachable: {exc}") from exc
    user = top.get("user") or {}
    artwork = (top.get("artwork") or {}).get("150x150") or ""
    return Playable(
        provider="audius",
        title=top.get("title") or query,
        stream_url=stream_url,
        artist=user.get("name") or "",
        webpage_url=f"https://audius.co/{user.get('handle') or ''}",
        duration=_parse_seconds(top.get("duration")),
        thumbnail=artwork,
    )


async def candidates(query: str, limit: int = 5) -> list[Candidate]:
    """Metadata-only search hits for the picker — no stream URL fetched yet."""
    if "://" in query:
        return []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                    f"{DISCOVERY}/v1/tracks/search",
                    params={"query": query, "app_name": APP_NAME},
                    timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status >= 400:
                    raise SourceUnavailable(
                        "audius", "api_error",
                        f"Audius search returned HTTP {response.status}.")
                body = await response.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise SourceUnavailable(
            "audius", "api_error", f"Audius unreachable: {exc}") from exc
    tracks = body.get("data") or []
    return [_track_to_candidate(t) for t in tracks[:limit] if t.get("id")]


def _track_to_candidate(track: dict) -> Candidate:
    user = track.get("user") or {}
    artwork = (track.get("artwork") or {}).get("150x150") or ""
    return Candidate(
        provider="audius",
        title=track.get("title") or "",
        artist=user.get("name") or "",
        duration=_parse_seconds(track.get("duration")),
        webpage_url=f"https://audius.co/{user.get('handle') or ''}",
        thumbnail=artwork,
        payload={"id": track.get("id")},
    )


async def materialize(candidate: Candidate) -> Playable:
    """Resolve the candidate's Audius stream URL on demand."""
    if candidate.provider != "audius" or not candidate.payload.get("id"):
        raise SourceUnavailable(
            "audius", "bad_candidate", "Not an Audius candidate.")
    track_id = candidate.payload["id"]
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                    f"{DISCOVERY}/v1/tracks/{track_id}/stream",
                    params={"app_name": APP_NAME},
                    timeout=aiohttp.ClientTimeout(total=30)) as stream_response:
                if stream_response.status >= 400:
                    raise SourceUnavailable(
                        "audius", "no_stream",
                        "Audius refused playback for that track.")
                stream_url = str(stream_response.url)
    except aiohttp.ClientError as exc:
        raise SourceUnavailable(
            "audius", "api_error", f"Audius unreachable: {exc}") from exc
    return Playable(
        provider="audius",
        title=candidate.title,
        stream_url=stream_url,
        artist=candidate.artist,
        webpage_url=candidate.webpage_url,
        duration=candidate.duration,
        thumbnail=candidate.thumbnail,
    )


def _parse_seconds(value) -> int | None:
    """Audius reports durations as 'H:MM:SS.ffffff'; normalize to seconds."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        parts = [float(piece) for piece in str(value).split(":")]
    except ValueError:
        return None
    if len(parts) == 3:
        return int(parts[0] * 3600 + parts[1] * 60 + parts[2])
    if len(parts) == 2:
        return int(parts[0] * 60 + parts[1])
    return int(parts[0]) if parts else None