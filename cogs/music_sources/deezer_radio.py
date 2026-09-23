"""Deezer auto-queue feed for `/queue auto` via the public REST API.

gw-light's `song.getRadio` family was removed server-side (every payload
comes back as ``GATEWAY_ERROR: Undefined or invalid output``), but
api.deezer.com still serves artist radios and top tracks from a datacenter
IP without any cookie. This module fetches track metadata from that feed
and maps it to the gw-light shape ``_build_playable`` already streams
(ARL token exchange + BF_CBC_STRIPE decrypt).
"""

import logging

import aiohttp

LOG = logging.getLogger("music.deezer_radio")

_API_BASE = "https://api.deezer.com"
_HEADERS = {
    "user-agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}
_FEED_LIMIT = 25


def track_id_from_url(url: str) -> int | None:
    """Extract the numeric track id from a Deezer track URL."""
    if "://" not in url:
        return None
    try:
        return int(url.rstrip("/").rsplit("/", 1)[-1])
    except ValueError:
        return None


def _map_api_track(api_track: dict) -> dict:
    """Translate an api.deezer.com track into the gw-light shape."""

    artist = api_track.get("artist") or {}
    album = api_track.get("album") or {}
    name = artist.get("name") or ""
    return {
        "SNG_ID": api_track.get("id"),
        "SNG_TITLE": api_track.get("title") or "",
        "ART_NAME": name,
        "ARTISTS": [{"ART_NAME": name}] if name else [],
        "ALB_PICTURE": album.get("md5_image") or "",
        "DURATION": api_track.get("duration"),
    }


def filter_feed(api_tracks: list[dict], exclude_id: int, limit: int) -> list[dict]:
    """Drop the seed and duplicates, then cap the feed to ``limit``."""
    seen: set[int] = set()
    picked: list[dict] = []
    for track in api_tracks:
        track_id = track.get("id")
        if track_id in (exclude_id, None) or track_id in seen:
            continue
        seen.add(track_id)
        picked.append(track)
        if len(picked) >= limit:
            break
    return picked


async def _api_get(session: aiohttp.ClientSession, path: str) -> dict:
    try:
        async with session.get(
                _API_BASE + path, headers=_HEADERS,
                timeout=aiohttp.ClientTimeout(total=30)) as response:
            if response.status >= 400:
                return {}
            return await response.json(content_type=None)
    except (aiohttp.ClientError, ValueError):
        LOG.warning("api.deezer.com request failed for %s", path)
        return {}


async def radio_track_dicts(
        session: aiohttp.ClientSession, seed_id: int, limit: int = 8) -> list[dict]:
    """Up to ``limit`` gw-light-shaped tracks similar to the seed track.

    Artist radio is the primary feed; the artist's top tracks are the
    fallback when a radio is empty.
    """
    info = await _api_get(session, f"/track/{seed_id}")
    artist_id = (info.get("artist") or {}).get("id")
    if not artist_id:
        return []
    feed = await _api_get(session, f"/artist/{artist_id}/radio")
    api_tracks = feed.get("data") or []
    if not api_tracks:
        top = await _api_get(session, f"/artist/{artist_id}/top?limit={_FEED_LIMIT}")
        api_tracks = top.get("data") or []
    return [
        _map_api_track(track)
        for track in filter_feed(api_tracks, seed_id, limit)]