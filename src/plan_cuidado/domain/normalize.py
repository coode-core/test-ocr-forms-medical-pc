"""Pure normalizers that turn raw OCR strings into exact form values."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date

from rapidfuzz import fuzz, process


def fold(text: str) -> str:
    """Uppercase, strip accents and collapse whitespace (for comparisons only)."""
    text = "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))
    return " ".join(text.upper().split())


def normalize_text(raw: str | None) -> str:
    return " ".join((raw or "").split()).upper()


def normalize_digits(raw: str | None) -> str:
    return re.sub(r"\D", "", raw or "")


_DATE_RE = re.compile(r"^\s*(\d{1,2})\s*[-/.]\s*(\d{1,2})\s*[-/.]\s*(\d{2}|\d{4})\s*$")
_ISO_RE = re.compile(r"^\s*(\d{4})-(\d{2})-(\d{2})\s*$")


def parse_date(raw: str | None, pivot_year: int | None = None) -> str | None:
    """Parse dd-mm-yy / dd/mm/yyyy (or ISO) into ISO 'YYYY-MM-DD'.

    Two-digit years are placed in the century that keeps them <= pivot_year + 1.
    """
    if not raw:
        return None
    pivot = pivot_year or date.today().year
    iso = _ISO_RE.match(raw)
    if iso:
        y, m, d = (int(g) for g in iso.groups())
    else:
        match = _DATE_RE.match(raw)
        if not match:
            return None
        d, m, y = (int(g) for g in match.groups())
        if y < 100:
            century = (pivot // 100) * 100
            y = century + y
            if y > pivot + 1:
                y -= 100
    try:
        return date(y, m, d).isoformat()
    except ValueError:
        return None


@dataclass(frozen=True)
class MatchResult:
    value: str | None
    reason: str | None = None
    score: float | None = None


_INT_RE = re.compile(r"\d+")


def _first_int(text: str) -> int | None:
    found = _INT_RE.search(text)
    return int(found.group()) if found else None


def match_option(
    raw: str | None,
    options: list[str],
    fuzzy_threshold: float = 85,
    aliases: dict[str, str] | None = None,
) -> MatchResult:
    """Match a raw value against the exact option list. Never invents a value.

    Order: exact (folded) -> abbreviation in parentheses "(CC)" -> number
    (codes like 02 -> T02 / MTC04 / EBS 02) -> fuzzy (rapidfuzz).
    """
    if raw is None or not raw.strip():
        return MatchResult(None, "vacío")
    folded_opts = {fold(o): o for o in options}
    key = fold(raw)
    compact = re.sub(r"[^A-Z0-9]", "", key)
    if aliases:
        alias_map = {fold(k): v for k, v in aliases.items()}
        alias = alias_map.get(compact) or alias_map.get(key)
        if alias:
            key = fold(alias)
            compact = re.sub(r"[^A-Z0-9]", "", key)

    if key in folded_opts:
        return MatchResult(folded_opts[key], score=100)

    if compact.isalpha() and 1 < len(compact) <= 4:
        hits = [o for f, o in folded_opts.items() if f"({compact})" in f.replace(" ", "")]
        if len(hits) == 1:
            return MatchResult(hits[0], score=100)

    if re.fullmatch(r"[A-Z]{0,4}\d+", compact):
        number = _first_int(compact)
        if number is not None:
            prefix = re.sub(r"\d", "", compact)
            hits = [o for o in options if _first_int(o) == number]
            if prefix:
                hits = [o for o in hits if re.sub(r"[^A-Z]", "", fold(o)).startswith(prefix)] or hits
            if len(hits) == 1:
                return MatchResult(hits[0], score=100)
            if len(hits) > 1:
                return MatchResult(None, f"ambiguo: {', '.join(hits)}")
            if compact.isdigit():
                return MatchResult(None, f"código {raw!r} no existe en las opciones")

    best = process.extractOne(key, list(folded_opts), scorer=fuzz.WRatio)
    if best is None:
        return MatchResult(None, "sin opciones")
    label, score, _ = best
    if score >= fuzzy_threshold:
        return MatchResult(folded_opts[label], score=score)
    return MatchResult(None, f"sin coincidencia (mejor: {folded_opts[label]!r}, {score:.0f})", score=score)
