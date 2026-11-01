"""Format rules. Each returns None when valid or a Spanish error message."""

from __future__ import annotations

import re
from collections.abc import Callable

DEFAULT_FICHA_PATTERN = r"^02020\d+$"


def _phone(value: str, **_: object) -> str | None:
    if re.fullmatch(r"3\d{9}", value):
        return None
    return "teléfono debe tener 10 dígitos y empezar por 3"


def _numeric(value: str, **_: object) -> str | None:
    return None if value.isdigit() else "debe contener solo números"


def _ficha(value: str, ficha_pattern: str | None = None, **_: object) -> str | None:
    pattern = ficha_pattern or DEFAULT_FICHA_PATTERN
    return None if re.fullmatch(pattern, value) else f"código de ficha no cumple el patrón {pattern}"


def _iso_date(value: str, **_: object) -> str | None:
    return None if re.fullmatch(r"\d{4}-\d{2}-\d{2}", value) else "fecha debe tener formato AAAA-MM-DD"


RULES: dict[str, Callable[..., str | None]] = {
    "phone": _phone,
    "numeric": _numeric,
    "ficha": _ficha,
    "iso_date": _iso_date,
}


def check_rule(name: str, value: str, ficha_pattern: str | None = None) -> str | None:
    return RULES[name](value, ficha_pattern=ficha_pattern)
