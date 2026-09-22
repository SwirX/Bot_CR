"""Source-agnostic music resolution: first provider to yield a stream wins.

Ordered providers are tried per query (Deezer, then Audius). Failures are
recorded rather than re-raised so a blocked source never kills the chain; when
every provider fails, AllSourcesFailed carries the details so the caller can
surface the most actionable hint.

YouTube/YTMusic playback was removed deliberately. yt-dlp was handed whatever
URL a member typed (``/play <url>``) behind no validation beyond an
``^https?://`` regex, so any member could make the host fetch arbitrary
internal URLs (169.254.169.254, 10.0.0.0/8, localhost services) and probe the
private network — blind, since no response body reached Discord, but a working
SSRF. Deezer + Audius cover search and playback without it.
"""

from . import audius, deezer
from .model import AllSourcesFailed, Playable, SourceFailure, SourceUnavailable

_PROVIDERS = (deezer.resolve, audius.resolve)


async def resolve_playable(query: str) -> Playable:
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