"""Recognise a pasted music link and describe what it points at.

Why this module exists
----------------------
``/play`` used to hand whatever a member typed straight to yt-dlp behind a
``^https?://`` regex. That is a working SSRF: any member could make the host
fetch ``http://169.254.169.254/...`` or a ``localhost`` admin port and map the
private network by timing. So YouTube playback was removed.

Members kept pasting links anyway — they are how people share music — so the
links are handled *without* ever fetching a URL the member supplied. This
module only ever classifies text against a fixed host allowlist and hands back
a validated identifier; every network call in the music stack builds its own
URL from a hard-coded endpoint plus that identifier.

Two consequences worth stating plainly:

* An unrecognised host is **not** a fetch attempt. ``/play http://10.0.0.1/``
  is treated as ordinary search text and finds nothing.
* Nothing here resolves audio. YouTube *stream* extraction is still blocked
  from this host's datacenter IP ("Sign in to confirm you're not a bot"), and
  is deliberately not attempted. What works from here is YouTube's public
  oEmbed metadata endpoint, which is not bot-gated — that gives us the real
  title and artist for a pasted link, and Deezer then plays that song. Members
  hear the song they pasted; the link is genuinely parsed.

Everything in this module is pure and hermetic (no I/O) except the two
``async def`` resolvers at the bottom, which are what the bot actually calls.
"""

from dataclasses import dataclass
import re
from urllib.parse import parse_qs, urlsplit

__all__ = [
    "LinkIntent", "ParsedTrack", "classify", "find_url", "clean_title",
    "artist_from_channel", "query_for", "fetch_parsed",
]


@dataclass(frozen=True)
class LinkIntent:
    """A recognised link, reduced to a validated identifier."""

    provider: str        # "youtube" | "deezer"
    kind: str            # "track" | "playlist" | "unsupported"
    identifier: str      # validated video id / numeric track id
    webpage_url: str     # canonical, built from the identifier — never echoed text
    label: str           # short human label for messages

    @property
    def playable(self) -> bool:
        return self.kind == "track"


@dataclass(frozen=True)
class ParsedTrack:
    """Title/artist recovered from a link, ready to search the audio providers."""

    title: str
    artist: str
    webpage_url: str      # the canonical link, for display
    provider: str         # which link service it came from
    source_url: str = ""  # the link as the member pasted it

    @property
    def query(self) -> str:
        return query_for(self.title, self.artist)


# ── host allowlist ───────────────────────────────────────────────────
# Exact host matches only. No suffix matching: "evil-youtube.com" and
# "youtube.com.evil.test" must both fail, which is why this is a set
# membership test on the parsed hostname rather than a regex or endswith().
YOUTUBE_HOSTS = frozenset({
    "youtube.com", "www.youtube.com", "m.youtube.com",
    "music.youtube.com", "gaming.youtube.com",
    "youtu.be", "www.youtu.be",
})
DEEZER_HOSTS = frozenset({
    "deezer.com", "www.deezer.com", "deezer.page.link",
})

# YouTube ids are exactly 11 URL-safe base64 characters. Validating the shape
# is what makes it safe to interpolate into our own oEmbed URL.
_VIDEO_ID = re.compile(r"\A[A-Za-z0-9_-]{11}\Z")
_NUMERIC_ID = re.compile(r"\A[0-9]{1,12}\Z")

_YOUTUBE_ID_PATHS = ("shorts", "embed", "live", "v")
# A bare link inside a sentence: "listen to this https://youtu.be/abc123".
_URL_TOKEN = re.compile(r"https?://[^\s<>\"'`]+")


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _normalise(raw: str) -> str:
    """Turn pasted text into a URL, or return "" when there isn't one.

    Accepts Discord's ``<https://…>`` autolink form and bare ``youtu.be/abc``
    (no scheme), both of which members paste constantly. Refuses everything
    else — in particular a non-http scheme like ``file://`` or ``gopher://``.
    """
    text = raw.strip().strip("<>").strip()
    if not text:
        return ""
    if text.startswith("//"):
        text = "https:" + text
    elif "://" not in text:
        if re.match(r"\A[\w.-]+\.[a-z]{2,}/", text, re.I):
            text = "https://" + text
        else:
            return ""
    scheme = urlsplit(text).scheme.lower()
    return text if scheme == "http" or scheme == "https" else ""


def find_url(query: str) -> str:
    """The first http(s) URL in ``query``, or "" if there isn't one."""
    whole = _normalise(query)
    if whole:
        return whole
    match = _URL_TOKEN.search(query)
    return _normalise(match.group(0)) if match else ""


def _classify_url(url: str) -> LinkIntent | None:
    """Map an already-normalised URL onto a LinkIntent, or None."""
    host = _host(url)
    if not host:
        return None
    parts = urlsplit(url)
    if host in YOUTUBE_HOSTS:
        return _youtube(parts)
    if host in DEEZER_HOSTS:
        return _deezer(parts)
    return None


def _youtube(parts) -> LinkIntent | None:
    segments = [s for s in parts.path.split("/") if s]
    playlist = parse_qs(parts.query).get("list", [""])[0]

    video_id = ""
    if host_is_short(parts.hostname):
        if segments:
            video_id = segments[0]
    elif segments and segments[0].lower() == "watch":
        video_id = parse_qs(parts.query).get("v", [""])[0]
    elif len(segments) >= 2 and segments[0].lower() in _YOUTUBE_ID_PATHS:
        video_id = segments[1]

    if _VIDEO_ID.match(video_id):
        return LinkIntent(
            provider="youtube", kind="track", identifier=video_id,
            webpage_url=f"https://www.youtube.com/watch?v={video_id}",
            label=f"YouTube video {video_id}")
    if playlist:
        # We don't fetch playlists (that would mean walking an arbitrary
        # member-supplied list server-side); say so plainly instead.
        return LinkIntent(
            provider="youtube", kind="playlist", identifier=playlist,
            webpage_url=f"https://www.youtube.com/playlist?list={playlist}",
            label="a YouTube playlist")
    return None


def host_is_short(hostname: str | None) -> bool:
    """youtu.be is the only host whose *first* path segment is the video id."""
    return (hostname or "").lower() in ("youtu.be", "www.youtu.be")


def _deezer(parts) -> LinkIntent | None:
    segments = [s for s in parts.path.split("/") if s]
    track_id = ""
    if len(segments) >= 2 and segments[0].lower() == "track":
        track_id = segments[1]
    elif len(segments) == 1:
        track_id = segments[0]  # deezer.com/3135556 short form
    if not _NUMERIC_ID.match(track_id):
        return None
    return LinkIntent(
        provider="deezer", kind="track", identifier=track_id,
        webpage_url=f"https://www.deezer.com/track/{track_id}",
        label=f"Deezer track {track_id}")


def classify(query: str) -> LinkIntent | None:
    """Recognise ``query`` as a music link.

    Returns ``None`` for anything that is not a recognised music link —
    including unrecognised hosts, which is what keeps arbitrary URLs from ever
    reaching a network call.
    """
    url = find_url(query)
    if not url:
        return None
    return _classify_url(url)


# ── title / artist recovery ──────────────────────────────────────────
# "(Official Video)", "[4K Remaster]", "(Lyrics)" and friends are noise for a
# catalog search: they push the real title out of the top match. Parenthetical
# segments are dropped only when they are *mostly* noise, so "(Acoustic)" or
# "(Live at Wembley)" survive and still steer the search.
_LEFT = r"(?<![A-Za-z0-9])"
_RIGHT = r"(?![A-Za-z0-9])"
_NOISE_ALT = (r"(?:official(?:ly)?|music|visuali[sz]er|lyric|lyrics|"
              r"audio|video|m/?v|hd|hq|4k|remaster(?:ed)?|explicit|clean|"
              r"full|album|with|colour\s*coded|color\s*coded|cc|version)")
# ASCII-aware boundaries, not \b: \b is Unicode-aware, so in "【公式MV】" the
# CJK character counts as a word character and there is no boundary before
# "MV" — the noise word then survives. Non-ASCII text around the term is
# exactly the case these brackets come from.
_NOISE_WORDS = re.compile(_LEFT + _NOISE_ALT + _RIGHT, re.I)
_CONNECTORS = re.compile(
    r"(?<![A-Za-z0-9])(?:and|or|with|from|out|the|a|an|in|on)(?![A-Za-z0-9])",
    re.I)
_BRACKETED = re.compile(r"[\(\[\{【]([^\)\]\}】]*)[\)\]\}】]")
# Un-bracketed trailing noise: "...GANGNAM STYLE(강남스타일) M/V". Only the tail
# is stripped, never the head, so a title that *starts* with a noise word
# ("Video Killed the Radio Star") survives intact.
_TRAILING_NOISE = re.compile(
    r"(?:\s*[-–—|]\s*|\s+)" + _NOISE_ALT +
    r"(?:\s*[-–—|]?\s*" + _NOISE_ALT + r")*\s*$", re.I)


def _is_noise(inner: str) -> bool:
    """True when a bracketed group carries no searchable information.

    Judge the group as a whole, not word by word: "(Official Music Video)"
    has no standalone "music" in it, and "(Remastered 2011)" is noise even
    though "2011" is not itself a noise word. So strip every noise word and
    connector, then ask whether any *letter* is left. Letters are the test
    rather than "any residue" so that a bare year ("(Remastered 2011)") and
    CJK script with no ASCII noise word ("【公式MV】") both count as noise.
    """
    residue = _NOISE_WORDS.sub(" ", inner)
    residue = _CONNECTORS.sub(" ", residue)
    return not re.search(r"[A-Za-z]", residue)


def clean_title(title: str) -> str:
    """Strip marketing noise from a video/track title."""
    text = " ".join((title or "").split())

    def _drop(match: re.Match) -> str:
        return " " if _is_noise(match.group(1)) else match.group(0)

    previous = None
    while previous != text:          # bracketed groups nest: "(Live (Remix))"
        previous = text
        text = _BRACKETED.sub(_drop, text)
    text = " ".join(text.split())    # dropping a group leaves a double space
    stripped = _TRAILING_NOISE.sub("", text).strip(" -–—|·,")
    text = stripped or text          # never reduce a title to nothing
    text = re.sub(r"\s*[-–—|]\s*$", "", text)   # trailing dangling separator
    return text.strip(" -–—|·,") or (title or "").strip()


def artist_from_channel(author: str) -> str:
    """Turn an uploader/channel name into a plausible artist name.

    YouTube oEmbed reports the *channel*, not the artist, and auto-generated
    topic channels are by far the most common case for music links:
    ``"Ed Sheeran - Topic"`` -> ``"Ed Sheeran"``. Keeping the " - Topic"
    suffix would search for a channel and lose the track.
    """
    name = " ".join((author or "").split())
    name = re.sub(r"\s*-\s*Topic\s*$", "", name, flags=re.I)
    name = re.sub(r"\s*VEVO\s*$", "", name, flags=re.I)
    name = re.sub(r"\s*Official\s*$", "", name, flags=re.I)
    name = re.sub(r"VEVO$", "", name, flags=re.I)
    return name.strip(" -–—|·") or (author or "").strip()


def _squash(text: str) -> str:
    """Letters and digits only, lowercased — for comparing names loosely."""
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def _same_name(left: str, right: str) -> bool:
    """Whether two names plausibly denote the same artist.

    Compared on squashed text because channel handles and artist names differ
    cosmetically: ``"RickAstley"`` vs ``"Rick Astley"``, ``"DaftPunk"`` vs
    ``"Daft Punk"``. A containment test either way (``"PSY"`` inside
    ``"officialpsy"``) catches the auto-generated channel case without
    demanding an exact match. Three characters minimum, so ``"The"`` or
    ``"Ed"`` can't match half the catalog.
    """
    a, b = _squash(left), _squash(right)
    if len(a) < 3 or len(b) < 3:
        return False
    return a == b or a in b or b in a


def query_for(title: str, artist: str) -> str:
    """The provider search string for a parsed link."""
    clean = clean_title(title)
    name = artist_from_channel(artist)
    if name and "-" in clean:
        head, _, rest = clean.partition("-")
        # "Ed Sheeran - Shape of You (Official Video)" with channel "Ed Sheeran":
        # the channel is already the title's own prefix, so don't search
        # "Ed Sheeran Ed Sheeran - Shape of You".
        if rest.strip() and _same_name(head, name):
            clean = rest.strip()
    parts = [p for p in (name, clean) if p]
    return " ".join(parts).strip()


# ── resolvers (the only I/O in this module) ───────────────────────────
_OEMBED = "https://www.youtube.com/oembed"
_DEEZER_TRACK = "https://api.deezer.com/track/{track_id}"
_HEADERS = {
    # YouTube's oEmbed endpoint rejects the default aiohttp agent.
    "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
}
_TIMEOUT = 8


async def fetch_parsed(intent: LinkIntent, session) -> ParsedTrack:
    """Resolve a recognised link to a title + artist to search for.

    ``session`` is an open :class:`aiohttp.ClientSession`. Both requests go to
    a hard-coded host with the validated identifier interpolated, so no
    member-supplied text ever reaches a URL.
    """
    import aiohttp

    if intent.provider == "youtube":
        url = _OEMBED
        params = {"url": intent.webpage_url, "format": "json"}
        title_key, artist_key = "title", "author_name"
    else:
        url = _DEEZER_TRACK.format(track_id=intent.identifier)
        params = None
        title_key, artist_key = "title", "artist"

    timeout = aiohttp.ClientTimeout(total=_TIMEOUT)
    try:
        async with session.get(url, params=params, headers=_HEADERS,
                               timeout=timeout) as response:
            if response.status >= 400:
                raise _unavailable(intent, f"HTTP {response.status}")
            body = await response.json(content_type=None)
    except aiohttp.ClientError as exc:
        raise _unavailable(intent, f"unreachable ({exc})") from exc
    except ValueError as exc:
        raise _unavailable(intent, "unreadable response") from exc

    if not isinstance(body, dict):
        raise _unavailable(intent, "unexpected response")
    title = str(body.get(title_key) or "").strip()
    if not title:
        raise _unavailable(intent, "no title in the response")
    raw_artist = body.get(artist_key) or ""
    if isinstance(raw_artist, dict):
        raw_artist = raw_artist.get("name") or ""
    return ParsedTrack(
        title=title, artist=str(raw_artist).strip(),
        webpage_url=intent.webpage_url, provider=intent.provider,
        source_url=intent.webpage_url)


def _unavailable(intent: LinkIntent, detail: str):
    from cogs.music_sources.model import SourceUnavailable
    return SourceUnavailable(
        intent.provider, "link_unreadable",
        f"Couldn't read that {intent.label} — {detail}. "
        "Paste the song title instead and I'll find it.")