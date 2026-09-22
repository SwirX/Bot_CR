"""YouTube / YouTube-Music provider: search metadata + yt-dlp stream extraction.

Isolated from the Music cog so the source registry can fall back to another
provider the moment YouTube bot-blocks the host network (the norm on cloud
datacenter IPs, where "sign in to confirm you're not a bot" appears).
"""

import asyncio
import dataclasses
import logging
import os
import re
import threading

try:
    import yt_dlp
except ImportError:  # pragma: no cover - exercised at deploy time
    yt_dlp = None

try:
    from ytmusicapi import YTMusic
except ImportError:  # pragma: no cover - exercised at deploy time
    YTMusic = None

from cogs.music_sources.model import Playable, SourceUnavailable

LOG = logging.getLogger("bot.music.youtube")

URL_RE = re.compile(r"^https?://", re.I)

# "Sign in to confirm you're not a bot" on datacenter IPs.
_BOT_BLOCKED = re.compile(r"sign in to confirm you.*not a bot", re.I)
COOKIES_HINT = ("YouTube refused to stream from this server's network, which "
                "flags datacenter IPs even with login cookies. The bot already "
                "tried its fallback sources — try a query the other providers "
                "carry.")

# Authenticated-but-frameless: the cookie account is too new/trust-less for
# YouTube to hand out stream formats yet.
_NO_FORMATS = re.compile(r"requested format is not available", re.I)
ACCOUNT_HINT = ("YouTube let us in but offered no stream for that video. This "
                "usually means the YouTube account behind the cookies isn't "
                "trusted yet — watch a few videos while logged in as it, then "
                "re-export cookies.txt and restart the bot.")

# ytmusicapi is not thread-safe, and its calls run via asyncio.to_thread.
_YT_MUSIC: "YTMusic | None" = None
_YT_MUSIC_LOCK = threading.Lock()

YTDL_OPTS = {
    "format": "bestaudio/best",
    "noplaylist": True,
    "quiet": True,
    "no_warnings": True,
    "extract_flat": False,
}


def _ytmusic() -> "YTMusic":
    global _YT_MUSIC
    if _YT_MUSIC is None:
        _YT_MUSIC = YTMusic()
    return _YT_MUSIC


def _ytdl_opts() -> dict:
    """Base yt-dlp options (+ cookiefile from ``YT_COOKIES_FILE``)."""
    opts = dict(YTDL_OPTS)
    cookies = os.environ.get("YT_COOKIES_FILE")
    if cookies:
        opts["cookiefile"] = cookies
    return opts


def extract_audio(url: str) -> Playable:
    """Blocking yt-dlp stream extraction for a video URL (run via to_thread)."""
    if yt_dlp is None:
        raise SourceUnavailable(
            "youtube", "missing_dep",
            "yt-dlp is not installed — run the bot from its venv.")
    if not URL_RE.match(url):
        raise SourceUnavailable("youtube", "bad_url",
                                f"not a resolvable URL: {url!r}")
    try:
        with yt_dlp.YoutubeDL(_ytdl_opts()) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        if _BOT_BLOCKED.search(str(exc)):
            raise SourceUnavailable("youtube", "blocked", COOKIES_HINT) from exc
        if _NO_FORMATS.search(str(exc)):
            raise SourceUnavailable("youtube", "no_stream", ACCOUNT_HINT) from exc
        raise SourceUnavailable(
            "youtube", "extract_failed",
            f"Couldn't extract a stream: {exc}") from exc
    if info.get("entries"):
        info = info["entries"][0]
    if not info or not info.get("url"):
        raise SourceUnavailable(
            "youtube", "no_stream",
            "No playable audio source found for that URL.")
    return Playable(
        provider="youtube",
        title=info.get("title") or url,
        stream_url=info["url"],
        webpage_url=info.get("webpage_url") or info.get("original_url") or url,
        video_id=info.get("id") or "",
        duration=info.get("duration"),
        thumbnail=info.get("thumbnail") or "",
        artist=info.get("artist") or info.get("channel") or info.get("uploader") or "",
        headers=info.get("http_headers") or {},
    )


async def resolve(query: str) -> Playable:
    """Resolve a user query or direct URL to a playable YouTube stream."""
    if URL_RE.match(query):
        return await asyncio.to_thread(extract_audio, query)
    return await asyncio.to_thread(resolve_song_query, query)


def resolve_song_query(query: str) -> Playable:
    """Search YTMusic for the query, then extract its stream (blocking)."""
    if YTMusic is None:
        raise SourceUnavailable(
            "youtube", "missing_dep",
            "ytmusicapi is not installed — run the bot from its venv.")
    with _YT_MUSIC_LOCK:
        results = _ytmusic().search(query, filter="songs", limit=1)
    if not results:
        raise SourceUnavailable(
            "youtube", "not_found",
            f"No YouTube Music results for {query!r}.")
    result = results[0]
    video_id = result.get("videoId")
    if not video_id:
        raise SourceUnavailable(
            "youtube", "no_stream",
            "YouTube Music returned no video for that query.")
    artists = ", ".join(a.get("name") for a in (result.get("artists") or [])
                        if a.get("name"))
    secs = result.get("duration_seconds")
    thumbs = result.get("thumbnails") or []
    try:
        playable = extract_audio(f"https://www.youtube.com/watch?v={video_id}")
    except SourceUnavailable:
        raise  # keep the existing youtube-specific diagnosis (blocked, etc.)
    return dataclasses.replace(
        playable,
        title=result.get("title") or playable.title,
        artist=artists or playable.artist,
        duration=int(secs) if secs else playable.duration,
        thumbnail=thumbs[-1].get("url") if thumbs else playable.thumbnail,
        webpage_url=f"https://www.youtube.com/watch?v={video_id}",
        video_id=video_id,
    )


def radio_seed_ids(video_id: str, limit: int) -> list[str]:
    """Nearest-neighbour video IDs for a track's YouTube Music radio."""
    if YTMusic is None:
        raise SourceUnavailable(
            "youtube", "missing_dep",
            "ytmusicapi is not installed — run the bot from its venv.")
    with _YT_MUSIC_LOCK:
        data = _ytmusic().get_watch_playlist(videoId=video_id, radio=True, limit=limit)
    seeds: list[str] = []
    for entry in data.get("tracks") or []:
        vid = entry.get("videoId")
        if vid and vid not in seeds:
            seeds.append(vid)
        if len(seeds) >= limit:
            break
    return seeds


async def resolve_parallel(video_ids: list[str]) -> list[Playable]:
    """Stream-extract several videos in parallel (radio queue generation)."""
    results = await asyncio.gather(
        *(asyncio.to_thread(extract_audio, f"https://www.youtube.com/watch?v={vid}")
          for vid in video_ids),
        return_exceptions=True,
    )
    playables: list[Playable] = []
    for vid, result in zip(video_ids, results):
        if isinstance(result, Exception):
            LOG.warning("Auto-queue: failed to resolve %s: %s", vid, result)
            continue
        playables.append(dataclasses.replace(result, video_id=vid))
    return playables