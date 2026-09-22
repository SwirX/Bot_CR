"""Public internet-radio stations as a fixed, keyless music source."""

from cogs.music_sources.model import Playable, SourceUnavailable

STATIONS = {
    "groovesalad": ("SomaFM Groove Salad", "https://ice1.somafm.com/groovesalad-128-mp3"),
    "dronezone": ("SomaFM Drone Zone", "https://ice1.somafm.com/dronezone-128-mp3"),
    "bootliquor": ("SomaFM Boot Liquor", "https://ice1.somafm.com/bootliquor-128-mp3"),
    "secretagent": ("SomaFM Secret Agent", "https://ice1.somafm.com/secretagent-128-mp3"),
    "indiepop": ("SomaFM Indie Pop Rocks!", "https://ice1.somafm.com/indiepop-128-mp3"),
}


def resolve(station_key: str) -> Playable:
    """Resolve a station key to a live-stream Playable."""
    entry = STATIONS.get(station_key.strip().lower())
    if entry is None:
        choices = ", ".join(STATIONS)
        raise SourceUnavailable(
            "radio", "not_found",
            f"Unknown station. Choose one of: {choices}")
    title, url = entry
    return Playable(provider="radio", title=f"{title} (live radio)", stream_url=url)