"""Derived fields: territory name, age and life course ("curso de vida")."""

from __future__ import annotations

from datetime import date

from plan_cuidado.domain.normalize import fold

# (keyword the option starts with, min age in years, max age in years)
_LIFE_COURSE = [
    ("PRIMERA INFANCIA", 0, 5),
    ("INFANCIA", 6, 11),
    ("ADOLESCENCIA", 12, 17),
    ("JUVENTUD", 18, 28),
    ("ADULTEZ", 29, 59),
    ("VEJEZ", 60, 200),
]


def _parse(iso: str) -> date | None:
    try:
        return date.fromisoformat(iso)
    except (TypeError, ValueError):
        return None


def territory_name(code: str, name_options: list[str]) -> str | None:
    code = (code or "").strip().upper()
    if not code:
        return None
    hits = [o for o in name_options if o.upper().startswith(code) and not o[len(code) : len(code) + 1].isdigit()]
    return hits[0] if len(hits) == 1 else None


def age_in_years(birth_iso: str, reference_iso: str) -> int | None:
    birth, ref = _parse(birth_iso), _parse(reference_iso)
    if not birth or not ref or birth > ref:
        return None
    return ref.year - birth.year - ((ref.month, ref.day) < (birth.month, birth.day))


def life_course_option(birth_iso: str, reference_iso: str, options: list[str]) -> str | None:
    birth, ref = _parse(birth_iso), _parse(reference_iso)
    age = age_in_years(birth_iso, reference_iso)
    if age is None or birth is None or ref is None:
        return None
    if (ref - birth).days < 7:  # first life-course stage starts at 7 days
        return None
    for keyword, low, high in _LIFE_COURSE:
        if low <= age <= high:
            hits = [o for o in options if fold(o).startswith(keyword)]
            return hits[0] if hits else None
    return None
