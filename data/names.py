"""Name normalization + fuzzy word matching for the club member registry.

The bot builds cursive nicknames (Unicode Mathematical Bold Script, see
``cogs/onboarding.to_cursive``) and staff store them via ``/fixname``, so a
real name can arrive fully cursive (e.g. ``𝓕𝓪𝓽𝓲𝓶𝓪 𝓑𝓸𝓾𝔃𝓪𝓻𝓫𝓲𝓪``). Matching
against the club ``members`` table therefore:

* ``decursive`` — maps the bold-script codepoints back to plain Latin;
* ``normalize_name`` — lowercases, collapses spaces, drops punctuation;
* ``name_similarity`` — order-insensitive word overlap (so ``Bouzarbia Fatima``
  matches ``Fatima Bouzarbia``) with single-letter initials handled;
* ``best_match`` / ``top_matches`` — against the live member list.

Pure functions, no network — unit-tested in ``scripts/test_names.py``.
"""

import unicodedata

# Unicode Mathematical Bold Script offsets (the only style to_cursive emits).
# 𝓐 U+1D4D0 .. 𝓩 U+1D4E9 (capitals), 𝓪 U+1D4EA .. 𝓩 U+1D503 (lowercase).
_BOLD_SCRIPT_CAPS = 0x1D4D0 - ord("A")
_BOLD_SCRIPT_LOW = 0x1D4EA - ord("a")

# Auto-link confidence (heuristic matches below this are ignored).
AUTO_LINK_THRESHOLD = 0.72
# Staff command /linkmember accepts weaker matches but still requires a clear winner.
LINK_SEARCH_THRESHOLD = 0.5


def has_cursive(text: str | None) -> bool:
    """True when the text contains any Mathematical Bold Script letter."""
    return any(0x1D4D0 <= ord(ch) <= 0x1D503 for ch in str(text or ""))


def decursive(text: str) -> str:
    """Map Unicode Mathematical Bold Script letters back to plain Latin.

    Capitals and lowercase are handled in separate, fully-assigned ranges; any
    other character (Arabic script, digits, spaces) passes through unchanged.
    """
    out = []
    for ch in str(text or ""):
        code = ord(ch)
        if 0x1D4D0 <= code <= 0x1D4E9:
            out.append(chr(code - _BOLD_SCRIPT_CAPS))
        elif 0x1D4EA <= code <= 0x1D503:
            out.append(chr(code - _BOLD_SCRIPT_LOW))
        else:
            out.append(ch)
    return "".join(out)


def normalize_name(text: str) -> str:
    """Lowercase, strip diacritics, drop punctuation; keep letters/spaces."""
    text = decursive(text)
    chars = []
    for ch in unicodedata.normalize("NFKD", text):
        if unicodedata.category(ch).startswith("M"):
            continue  # combining mark stripped by NFKD — drop it
        if ch.isalpha():
            chars.append(ch.lower())
        elif ch.isspace():
            chars.append(" ")
        # everything else (digits replaced by nothing, punctuation) -> space
        else:
            chars.append(" ")
    return " ".join("".join(chars).split())


def _tokens(name: str) -> list[str]:
    return normalize_name(name).split()


def name_similarity(a: str, b: str) -> float:
    """Order-insensitive word-overlap score in [0, 1].

    A single-letter token matches any word starting with that letter, so
    ``"Fatima B."`` scores well against ``"Fatima Bouzarbia"`` while
    ``"Bouzarbia Fatima"`` still scores 1.0 against ``"Fatima Bouzarbia"``.
    """
    ta = _tokens(a)
    tb = _tokens(b)
    if not ta or not tb:
        return 0.0
    if ta == tb:
        return 1.0
    remaining = list(tb)
    matched = 0
    for word in ta:
        for j, cand in enumerate(remaining):
            if word == cand or (len(word) == 1 and cand.startswith(word)) \
                    or (len(cand) == 1 and word.startswith(cand)):
                matched += 1
                remaining.pop(j)
                break
    return matched / max(len(ta), len(tb))


def best_match(query: str,
               candidates: list[tuple[str, str]],
               threshold: float = AUTO_LINK_THRESHOLD,
               ) -> tuple[str, str, float] | None:
    """Highest-scoring (id, name, score) candidate, or None.

    Rejects queries that are too weak OR ambiguous (best and runner-up within
    0.05 of each other) — a wrong auto-link would burn the 1:1 slot.
    """
    best: tuple[str, str, float] | None = None
    runner: tuple[str, str, float] | None = None
    for key, name in candidates:
        score = name_similarity(query, name)
        if best is None or score > best[2]:
            runner = best
            best = (key, name, score)
        elif runner is None or score > runner[2]:
            runner = (key, name, score)
    if best is None or best[2] < threshold:
        return None
    if runner is not None and best[2] - runner[2] < 0.05:
        return None
    return best


def top_matches(query: str, candidates: list[tuple[str, str]],
                limit: int = 3) -> list[tuple[str, str, float]]:
    """Best few candidates for showing "did you mean" suggestions."""
    scored = [(key, name, name_similarity(query, name))
              for key, name in candidates]
    scored.sort(key=lambda item: item[2], reverse=True)
    return scored[:limit]