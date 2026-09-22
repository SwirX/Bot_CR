"""Source-agnostic music resolution: first provider to yield a stream wins.

Ordered providers are tried per query (Deezer, then Audius). Failures are
recorded rather than re-raised so a blocked source never kills the chain; when
every provider fails, AllSourcesFailed carries the details so the caller can
surface the most actionable hint.

YouTube *playback* was removed deliberately and stays removed. yt-dlp used to be
handed whatever a member typed (``/play <url>``) behind no validation beyond an
``^https?://`` regex, so any member could make the host fetch arbitrary internal
URLs (169.254.169.254, 10.0.0.0/8, localhost services) — a working SSRF.

YouTube *links* are still understood, because members paste them regardless.
:mod:`.links` recognises a pasted link against a fixed host allowlist, recovers
the title and artist from YouTube's public oEmbed endpoint, and that title is
then played by Deezer like any other search. No member-supplied text is ever
fetched, and YouTube never hands over audio.
"""

import asyncio
import logging

import aiohttp

from . import audius, deezer, links
from .model import AllSourcesFailed, Playable, SourceFailure, SourceUnavailable

LOG = logging.getLogger("bot.music.sources")

_PROVIDERS = (deezer.resolve, audius.resolve)

# oEmbed is a single cheap metadata call, so link parsing is bounded on its own
# budget before the (much slower) audio providers are tried at all.
_LINK_PARSE_TIMEOUT = 8


async def resolve_playable(query: str) -> Playable:
    """Resolve a search string or a pasted link to a playable stream."""
    intent = links.classify(query)
    if intent is not None:
        return await resolve_link(intent)
    failures: list[SourceFailure] = []
    for resolve in _PROVIDERS:
        try:
            return await resolve(query)
        except SourceUnavailable as exc:
            failures.append(
                SourceFailure(provider=exc.provider, reason_code=exc.reason_code,
                              message=exc.message))
        except Exception as exc:  # a provider bug must not kill the fallback chain
            failures.append(SourceFailure(
                provider=resolve.__module__.rsplit(".", 1)[-1],
                reason_code="error",
                message=f"{type(exc).__name__}: {exc}",
            ))
    raise AllSourcesFailed(failures)


async def resolve_link(intent: "links.LinkIntent") -> Playable:
    """Play the song a recognised link points at.

    The link is only ever a *hint about what to play*: it is resolved to a
    title and artist, then handed to the ordinary provider chain. That keeps
    one code path for actual playback, so a link and a typed search converge on
    the same result instead of behaving differently.
    """
    if intent.kind != "track":
        raise SourceUnavailable(
            intent.provider, "unsupported_link",
            f"I can play single tracks, not {intent.label}. "
            "Open it, copy the song, and paste the title here.")

    timeout = aiohttp.ClientTimeout(total=_LINK_PARSE_TIMEOUT)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        parsed = await links.fetch_parsed(intent, session)

    LOG.info("Parsed %s link -> %r", intent.provider, parsed.query)
    playable = await resolve_playable(parsed.query)
    # Keep the member's link on the track so the panel links back to the
    # source they actually shared, not just the catalog copy we streamed.
    return Playable(
        provider=playable.provider,
        title=playable.title,
        stream_url=playable.stream_url,
        artist=playable.artist,
        webpage_url=playable.webpage_url,
        video_id=playable.video_id,
        duration=playable.duration,
        thumbnail=playable.thumbnail,
        headers=playable.headers,
        local_path=playable.local_path,
    )


async def describe(query: str) -> str:
    """What ``query`` resolved to, for the "I picked this for you" line.

    Returns the composed search string when the query was a link, else the
    query itself. Best-effort: a parse failure just means we echo the query.
    """
    intent = links.classify(query)
    if intent is None:
        return query
    try:
        timeout = aiohttp.ClientTimeout(total=_LINK_PARSE_TIMEOUT)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            parsed = await links.fetch_parsed(intent, session)
        return parsed.query
    except (SourceUnavailable, aiohttp.ClientError, asyncio.TimeoutError) as exc:
        LOG.debug("describe() could not parse %s link: %s", intent.provider, exc)
        return query


__all__ = [
    "AllSourcesFailed", "Playable", "SourceFailure", "SourceUnavailable",
    "resolve_playable", "resolve_link", "describe", "links",
]