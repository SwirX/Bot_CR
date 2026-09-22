"""Shared value types for the music source providers."""

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Playable:
    """A provider-resolved item with a directly streamable URL."""

    provider: str
    title: str
    stream_url: str
    artist: str = ""
    webpage_url: str = ""
    video_id: str = ""
    duration: int | None = None
    thumbnail: str = ""
    headers: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SourceFailure:
    """Why a single provider could not deliver audio for the query."""

    provider: str
    reason_code: str
    message: str


class SourceUnavailable(Exception):
    """A provider signals it cannot deliver audio for this query."""

    def __init__(self, provider: str, reason_code: str, message: str):
        super().__init__(message)
        self.provider = provider
        self.reason_code = reason_code
        self.message = message


class AllSourcesFailed(Exception):
    """Every provider failed; individual failures guide the user-facing hint."""

    def __init__(self, failures: list[SourceFailure]):
        super().__init__("; ".join(f"{f.provider}:{f.reason_code}" for f in failures))
        self.failures = failures

    def best_hint(self) -> str:
        # A bot-block or missing-stream is more actionable than "no results":
        # it says the source itself refused, not that the query was bad.
        for reason_code in ("blocked", "no_stream"):
            for failure in self.failures:
                if failure.reason_code == reason_code:
                    return failure.message
        if self.failures:
            return self.failures[-1].message
        return ""