"""Small date helpers shared by the club-ops cogs (not a cog)."""

import re
from datetime import date, timedelta

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_IN_RE = re.compile(r"^in (\d+) ?d(ays?)?$")
_SPAN_FULL_RE = re.compile(r"(?:\d+\s*[smhdw]\s*)+", re.I)
_SPAN_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


def parse_span(text: str) -> int:
    """Seconds for a human duration: '45m', '2h', '1d', '1h30m' or '90s'."""
    text = (text or "").strip()
    if not _SPAN_FULL_RE.fullmatch(text):
        raise ValueError(f"Can't parse duration: {text!r}")
    total = 0
    for amount, unit in re.findall(r"(\d+)\s*([smhdw])", text, re.I):
        total += int(amount) * _SPAN_UNITS[unit.lower()]
    return total


def parse_due(text: str) -> str:
    """Parse a due date: YYYY-MM-DD, 'today', 'tomorrow', or 'in Nd'."""
    text = (text or "").strip().lower()
    if _DATE_RE.match(text):
        return text
    today = date.today()
    if text in ("today", "now"):
        return today.isoformat()
    if text in ("tomorrow", "tmr", "tmrw"):
        return (today + timedelta(days=1)).isoformat()
    match = _IN_RE.match(text)
    if match:
        return (today + timedelta(days=int(match.group(1)))).isoformat()
    raise ValueError(f"Can't parse date: {text!r}")


def fmt_date(value) -> str:
    if not value:
        return "—"
    value = str(value)[:10]
    if not _DATE_RE.match(value):
        return value
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return value
    return d.strftime("%b %d")

def days_until(value) -> int | None:
    if not value:
        return None
    value = str(value)[:10]
    if not _DATE_RE.match(value):
        return None
    try:
        d = date.fromisoformat(value)
    except ValueError:
        return None
    return (d - date.today()).days

def is_overdue(value) -> bool:
    days = days_until(value)
    return days is not None and days < 0


def slugify(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.strip().lower()).strip("_")