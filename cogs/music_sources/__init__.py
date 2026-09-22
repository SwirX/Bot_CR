"""Source-agnostic music resolution: first provider to yield a stream wins.

Ordered providers are tried per query (YouTube, then Audius). Failures are
recorded rather than re-raised so a blocked source never kills the chain; when
every provider fails, AllSourcesFailed carries the details so the caller can
surface the most actionable hint.
"""

from . import audius, youtube
from .model import AllSourcesFailed, Playable, SourceFailure, SourceUnavailable

_PROVIDERS = (youtube.resolve, audius.resolve)


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