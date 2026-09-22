"""Internet radio: bundled SomaFM keys plus world-wide name search.

``/radio`` plays any station on Earth by name through the community
radio-browser.info directory; a handful of SomaFM stations stay keyed so a
known name resolves instantly and offline. Both paths yield
``Playable(provider="radio")``, which the lyrics surfaces refuse because a
live stream has no lyrics to look up.
"""

import logging

import aiohttp

from cogs.music_sources.model import Playable, SourceUnavailable

LOG = logging.getLogger("music.radio")

STATIONS = {
    "groovesalad": ("SomaFM Groove Salad", "https://ice1.somafm.com/groovesalad-128-mp3"),
    "dronezone": ("SomaFM Drone Zone", "https://ice1.somafm.com/dronezone-128-mp3"),
    "bootliquor": ("SomaFM Boot Liquor", "https://ice1.somafm.com/bootliquor-128-mp3"),
    "secretagent": ("SomaFM Secret Agent", "https://ice1.somafm.com/secretagent-128-mp3"),
    "indiepop": ("SomaFM Indie Pop Rocks!", "https://ice1.somafm.com/indiepop-128-mp3"),
}

_MIRRORS = ("de1", "at1", "nl1")
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}


def _relevance(name: str, wanted: str) -> int:
    """How well a station name matches the query, 0..3."""
    name = name.strip().lower()
    wanted = wanted.strip().lower()
    if not name or not wanted:
        return 0
    if name == wanted:
        return 3
    # Space/punctuation-insensitive check so "radiomars" matches "Radio Mars FM".
    compact_name = "".join(ch for ch in name if ch.isalnum())
    compact_wanted = "".join(ch for ch in wanted if ch.isalnum())
    if wanted in name or compact_wanted in compact_name:
        return 2
    if all(token in compact_name for token in wanted.split() if token):
        return 1
    return 0


def pick_station(stations: list[dict], query: str) -> dict | None:
    """Best directory entry for a name: relevance first, popularity tiebreak.

    Entries without a usable stream URL are ignored entirely.
    """
    wanted = query.strip().lower()

    def _rank(station: dict) -> tuple[int, int]:
        stream = station.get("url_resolved") or station.get("url")
        name = station.get("name") or ""
        if not stream or not name.strip():
            return (-1, 0)
        return (_relevance(name, wanted), int(station.get("clickcount") or 0))

    ranked = [s for s in stations if _rank(s)[0] > 0]
    if not ranked:
        return None
    return max(ranked, key=_rank)


def _prefix_variants(query: str) -> list[str]:
    """Fallback search terms for concatenated names like ``radiomars``.

    The directory tokenizes names, so a no-space query only matches stations
    whose name keeps that exact token ("hitradio" works, "radiomars" misses
    "Radio Mars"). Retrying without a leading "radio" recovers the common
    concat pattern without guessing word boundaries.
    """
    compact = query.strip().lower()
    if compact.startswith("radio") and len(compact) > 8:
        return [compact[5:]]
    return []


def _build_station_playable(station: dict) -> Playable:
    name = station.get("name") or "Unknown station"
    country = station.get("country") or ""
    suffix = f" — {country} (live radio)" if country else " (live radio)"
    return Playable(
        provider="radio",
        title=f"{name}{suffix}",
        stream_url=station.get("url_resolved") or station.get("url") or "",
        thumbnail=station.get("favicon") or "",
        headers=dict(_HEADERS),
    )


async def _directory_search(session: aiohttp.ClientSession, query: str) -> list[dict]:
    params = {"name": query, "limit": 25, "hidebroken": "true",
              "order": "clickcount", "reverse": "true"}
    for mirror in _MIRRORS:
        url = f"https://{mirror}.api.radio-browser.info/json/stations/search"
        try:
            async with session.get(
                    url, params=params, headers=_HEADERS,
                    timeout=aiohttp.ClientTimeout(total=20)) as response:
                if response.status < 400:
                    return await response.json(content_type=None)
        except (aiohttp.ClientError, ValueError):
            LOG.warning("radio-browser mirror %s failed for %r", mirror, query)
    return []


async def resolve_station(
        query: str, session: aiohttp.ClientSession | None = None) -> Playable:
    """A Playable for the station: bundled SomaFM key or directory search."""
    key = query.strip().lower()
    entry = STATIONS.get(key)
    if entry is not None:
        title, url = entry
        return Playable(provider="radio", title=f"{title} (live radio)",
                        stream_url=url, headers=dict(_HEADERS))
    owns_session = session is None
    session = session or aiohttp.ClientSession()
    try:
        station = pick_station(await _directory_search(session, query), query)
        if station is None:
            for alt in _prefix_variants(query):
                station = pick_station(await _directory_search(session, alt), alt)
                if station is not None:
                    break
    finally:
        if owns_session:
            await session.close()
    if station is None:
        raise SourceUnavailable(
            "radio", "not_found",
            f"No station found for {query!r}. Try a more specific name "
            f"(e.g. `/radio hitradio`) or one of the built-ins: "
            f"{', '.join(STATIONS)}.")
    return _build_station_playable(station)