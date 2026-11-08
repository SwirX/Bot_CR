"""Normalize English and French answers for onboarding roles."""

import unicodedata

_FEMALE = frozenset({"f", "female", "females", "girl", "girls", "woman", "women",
                     "femme", "femmes", "fille", "filles", "feminin", "feminine", "femelle"})
_MALE = frozenset({"m", "male", "males", "boy", "boys", "man", "men", "homme",
                   "hommes", "garcon", "garcons", "masculin"})


def normalize_gender(value: str) -> str | None:
    normalized = unicodedata.normalize("NFKD", value.strip().casefold())
    answer = "".join(character for character in normalized
                     if not unicodedata.combining(character))
    if answer in _FEMALE:
        return "female"
    if answer in _MALE:
        return "male"
    return None
