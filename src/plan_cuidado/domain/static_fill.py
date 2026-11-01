"""Fill empty cells from the "Datos fijos" tab, conditional defaults and derived fields."""

from __future__ import annotations

from dataclasses import replace

from plan_cuidado.domain.derive import age_in_years, life_course_option, territory_name
from plan_cuidado.domain.mapping import FieldMapping
from plan_cuidado.domain.schema import FormSchema
from plan_cuidado.domain.tracker import Status, TrackerRow

STATIC_HEADERS = ["clave", "pregunta", "valor", "obligatoria"]
REMOVED_PREFIX = "[ELIMINADA DEL FORMULARIO] "


def _cell(row: list, i: int) -> str:
    return str(row[i]).strip() if i < len(row) and row[i] is not None else ""


def merge_static_table(existing: list[list], schema: FormSchema, seed: dict[str, str]) -> tuple[list[list[str]], int]:
    """Grid for the "Datos fijos" tab: one row per form question (in form order), keeping the
    values already typed, filling empty ones from `seed` (legacy static_data.json) and keeping
    rows of questions no longer in the form, marked as removed. Returns (grid, seeded count)."""
    values = {_cell(r, 0): _cell(r, 2) for r in existing[1:] if _cell(r, 0)}
    titles = {_cell(r, 0): _cell(r, 1) for r in existing[1:] if _cell(r, 0)}
    grid = [list(STATIC_HEADERS)]
    seeded = 0
    for q in schema.questions:
        value = values.get(q.key, "")
        if not value and seed.get(q.key):
            value = str(seed[q.key]).strip()
            seeded += 1
        grid.append([q.key, q.label, value, "SI" if q.required else "NO"])
    known = set(schema.keys)
    for key, value in values.items():
        if key not in known:
            title = titles.get(key, "")
            grid.append([key, title if title.startswith(REMOVED_PREFIX) else REMOVED_PREFIX + title, value, ""])
    return grid, seeded


def static_from_table(grid: list[list]) -> dict[str, str]:
    """{question key: value} for the non-empty, non-removed rows of the "Datos fijos" tab."""
    return {
        _cell(r, 0): _cell(r, 2)
        for r in grid[1:]
        if _cell(r, 0) and _cell(r, 2) and not _cell(r, 1).startswith(REMOVED_PREFIX)
    }


def static_values(static_data: dict) -> dict[str, str]:
    values = static_data.get("valores", static_data) if isinstance(static_data, dict) else {}
    return {
        k: str(v).strip()
        for k, v in values.items()
        if not k.startswith("_") and isinstance(v, (str, int, float)) and str(v).strip()
    }


def _set_if_empty(answers: dict[str, str], key: str, value: str | None) -> None:
    if value and not (answers.get(key) or "").strip():
        answers[key] = value


def apply_static_and_derived(
    row: TrackerRow, schema: FormSchema, mapping: FieldMapping, static_data: dict
) -> TrackerRow:
    if row.status is Status.SUBMITTED:
        return row
    answers = dict(row.answers)
    known = set(schema.keys)
    for key, value in static_values(static_data).items():
        if key in known:
            _set_if_empty(answers, key, value)

    d = mapping.derived
    if (q := schema.get(d.territory_name)) and answers.get(d.territory_code):
        _set_if_empty(answers, q.key, territory_name(answers[d.territory_code], q.options))
    birth, ref = answers.get(d.birth_date, ""), answers.get(d.reference_date, "")
    if schema.get(d.age):
        age = age_in_years(birth, ref)
        _set_if_empty(answers, d.age, None if age is None else str(age))
    if q := schema.get(d.life_course):
        _set_if_empty(answers, q.key, life_course_option(birth, ref, q.options))

    for cond in mapping.conditionals:
        if cond.default and cond.default.field in known and cond.when.holds(answers):
            _set_if_empty(answers, cond.default.field, cond.default.value)

    return replace(row, answers=answers)
