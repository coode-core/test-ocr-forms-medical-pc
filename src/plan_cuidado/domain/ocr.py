"""OCR contract (JSON schema + prompt) and mapping of extracted cells to tracker rows."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field, ValidationError, model_validator

from plan_cuidado.domain.mapping import ColumnMap, FieldMapping
from plan_cuidado.domain.normalize import match_option, normalize_digits, normalize_text, parse_date
from plan_cuidado.domain.rules import check_rule
from plan_cuidado.domain.schema import FormSchema
from plan_cuidado.domain.tracker import Status, TrackerRow, make_row_id

OCR_COLUMNS: dict[str, str] = {
    "fecha_plan": "FECHA PLAN DE CUIDADO (dd-mm-aa)",
    "nombre": "NOMBRE COMPLETO",
    "tipo_doc": "TIPO DE DOCUMENTO (CC, TI, RC, CE, PPT, ...)",
    "numero_documento": "NÚMERO DE DOCUMENTO",
    "ficha": "NÚMERO DE FICHA",
    "integrante": "NÚMERO DE INTEGRANTE",
    "direccion": "DIRECCIÓN",
    "telefono": "TELÉFONO",
    "comuna": "COMUNA",
    "barrio": "BARRIO",
    "territorio": "TERRITORIO",
    "microterritorio": "MICROTERRITORIO",
    "observaciones": "OBSERVACIONES",
}

HEADER_FIELDS: dict[str, str] = {
    "colaborador": "NOMBRE DEL COLABORADOR",
    "identificacion": "NÚMERO DE IDENTIFICACIÓN DEL COLABORADOR",
    "cargo": "CARGO",
    "ebs": "EBS Nº",
    "celular": "CELULAR",
    "municipio": "MUNICIPIO",
}

_RULES = (
    "Rules:\n"
    "- Transcribe exactly what is written. Never guess or invent. If a cell is empty, "
    "illegible or crossed out, return null.\n"
    "- Be strict with digits: document numbers, ficha numbers, phone numbers and dates must be copied "
    "digit by digit.\n"
    "- Do not transcribe the signature (FIRMA) column.\n"
)
# What each column holds, so every value lands in its column even where the titles are hard to read.
COLUMN_HINTS: dict[str, str] = {
    "fecha_plan": "date dd-mm-aa, e.g. 06-08-26",
    "nombre": "patient full name",
    "tipo_doc": "short code: CC, TI, RC, CE, CV, PT, PPT, PA...",
    "numero_documento": "document number (6-11 digits, sometimes with letters)",
    "ficha": "family record number, about 10 digits",
    "integrante": "member number, 1-2 digits",
    "direccion": "address, e.g. 'KR 15H 32D-26 SUR', 'CALLE 33A SUR 15H-29'",
    "telefono": "phone, usually 10 digits starting with 3",
    "comuna": "narrow column with a single small number (e.g. 1)",
    "barrio": "neighbourhood name",
    "territorio": "2-digit code (e.g. 02)",
    "microterritorio": "2-digit code (e.g. 04)",
    "observaciones": "free notes in the last column (often empty)",
}
_COLUMNS_TEXT = (
    "Table columns, left to right (one value per column, never move a value to another column; "
    "a cell may be empty):\n"
    "1. No. = the small PRINTED row number in the narrow first column -> row_number\n"
    + "".join(
        f"{i}. {name} = {OCR_COLUMNS[name]}: {COLUMN_HINTS[name]}\n"
        for i, name in enumerate([n for n in OCR_COLUMNS if n != "observaciones"], start=2)
    )
    + f"{len(OCR_COLUMNS) + 1}. FIRMA DE LA PERSONA = a signature or a name written as a signature: do NOT transcribe it\n"
    + f"{len(OCR_COLUMNS) + 2}. observaciones = {OCR_COLUMNS['observaciones']}: {COLUMN_HINTS['observaciones']}\n"
    "Copy every digit; long numbers are written without separators, count the digits carefully.\n"
)
_ROW_RULES = (
    "- row_number is the PRINTED row number in the first column 'No.' (1 to 25), not your own count.\n"
    "- Printed rows that have nothing handwritten: return them with empty true and every value null.\n"
)

HEADER_PROMPT = (
    "You read the printed header of a scanned, handwritten Colombian 'PLAN DE CUIDADO' sheet (Spanish). "
    "Only the header fields above the patient table are needed; return them.\n"
    + _RULES
    + "- rotation_degrees: clockwise rotation needed to read the page upright (0, 90, 180 or 270).\n"
    "Header fields: " + "; ".join(f"{k} = {v}" for k, v in HEADER_FIELDS.items())
)


def _only(columns) -> str:
    if not columns:
        return ""
    return (
        f"Return only these columns: {', '.join(columns)} (plus row_number and empty). The other columns "
        "are described only so you can tell where each of these is.\n"
    )


def strip_prompt(index: int, count: int, columns=None) -> str:
    """Prompt for horizontal strip `index` (1-based) of `count` of the patient table
    (`columns`: a subset of OCR_COLUMNS to return, all by default)."""
    return (
        "You transcribe a scanned, handwritten Colombian 'PLAN DE CUIDADO' patient table (Spanish). "
        f"This image is horizontal strip {index} of {count} of the table (strips overlap a little); "
        "the table's column titles are pasted on top of it, above the rows.\n"
        + _RULES
        + _ROW_RULES
        + "- Return every table row whose printed row number is visible in this strip, top to bottom. "
        "Skip a row only if it is cut by the image edge so that its cells cannot be read.\n"
        + _COLUMNS_TEXT
        + _only(columns)
    )


def row_prompt(row_number: int, columns=None) -> str:
    """Targeted prompt: re-read a single printed row that was missed."""
    return (
        "You transcribe a scanned, handwritten Colombian 'PLAN DE CUIDADO' patient table (Spanish).\n"
        f"Return only row No. {row_number} (the row whose printed number in the first column 'No.' is "
        f"{row_number}); ignore every other row. If that row has nothing handwritten, return it with empty true.\n"
        + _RULES
        + _ROW_RULES
        + _COLUMNS_TEXT
        + _only(columns)
    )


def _cell_schema() -> dict:
    # A plain string per cell: the model's self-reported confidence never flagged its errors
    # (wrong digits came back at 0.95-1.0) and cost a third of the output tokens.
    return {"type": ["string", "null"]}


def _object_of_cells(names) -> dict:
    return {
        "type": "object",
        "properties": {n: _cell_schema() for n in names},
        "required": list(names),
        "additionalProperties": False,
    }


_TABLE_FIELDS = ("titles_top", "titles_bottom", "left", "right", "last_row_bottom")


def header_json_schema() -> dict:
    table = {
        "type": "object",
        "properties": {name: {"type": "integer"} for name in _TABLE_FIELDS},
        "required": list(_TABLE_FIELDS),
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "rotation_degrees": {"type": "integer", "enum": [0, 90, 180, 270]},
            "header": _object_of_cells(HEADER_FIELDS),
            "table": table,
        },
        "required": ["rotation_degrees", "header", "table"],
        "additionalProperties": False,
    }


def rows_json_schema(columns=None) -> dict:
    row = _object_of_cells([c for c in OCR_COLUMNS if c in columns] if columns else OCR_COLUMNS)
    row["properties"] = {"row_number": {"type": "integer"}, "empty": {"type": "boolean"}, **row["properties"]}
    row["required"] = ["row_number", "empty", *row["required"]]
    return {
        "type": "object",
        "properties": {"rows": {"type": "array", "items": row}},
        "required": ["rows"],
        "additionalProperties": False,
    }


class Cell(BaseModel):
    value: str | None = None
    confidence: float | None = None  # only in responses cached before cells became plain strings

    @model_validator(mode="before")
    @classmethod
    def _plain(cls, data):
        if data is None or isinstance(data, (str, int, float)):
            return {"value": None if data is None else str(data)}
        return data

    @property
    def text(self) -> str:
        return (self.value or "").strip()


class ExtractedRow(BaseModel):
    """One table row. The model returns it flat ({row_number, empty, <column>: cell})."""

    row_number: int | None = None
    empty: bool = False
    cells: dict[str, Cell] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _flat(cls, data):
        if isinstance(data, dict) and "cells" not in data:
            data = dict(data)
            number = data.pop("row_number", None)
            empty = data.pop("empty", False)
            return {"row_number": number, "empty": bool(empty), "cells": data}
        return data

    def cell(self, name: str) -> Cell | None:
        return self.cells.get(name)

    @property
    def mean_confidence(self) -> float:
        values = [c.confidence for c in self.cells.values() if c.text and c.confidence is not None]
        return sum(values) / len(values) if values else 0.0

    @property
    def filled(self) -> int:
        """Cells with text (tie-breaker between two readings of the same row)."""
        return sum(1 for c in self.cells.values() if c.text)


class TableBox(BaseModel):
    """Where the model located the patient table, in 0-1000 page coordinates."""

    titles_top: int
    titles_bottom: int
    left: int
    right: int
    last_row_bottom: int


class PageExtraction(BaseModel):
    rotation_degrees: int = 0
    header: dict[str, Cell] = Field(default_factory=dict)
    table: TableBox | None = None
    rows: list[ExtractedRow] = Field(default_factory=list)


def parse_extraction(content: str | dict) -> PageExtraction:
    if isinstance(content, str):
        match = re.search(r"\{.*\}", content, re.S)
        if not match:
            raise ValueError("OCR response contains no JSON object")
        try:
            content = json.loads(match.group(0))
        except json.JSONDecodeError as exc:
            raise ValueError(f"OCR response is not valid JSON: {exc}") from exc
    try:
        return PageExtraction.model_validate(content)
    except ValidationError as exc:
        raise ValueError(f"OCR response does not match the schema: {exc}") from exc


@dataclass(frozen=True)
class SourceRef:
    file_id: str
    file_name: str
    link: str
    page: int


@dataclass(frozen=True)
class _Outcome:
    value: str | None
    flag: str | None


def _normalize(cell: Cell, col: ColumnMap, options: list[str], mapping: FieldMapping, pivot_year: int) -> _Outcome:
    raw = cell.text
    if not raw:
        return _Outcome(None, None)
    if cell.confidence is not None and cell.confidence < mapping.confidence_threshold:
        return _Outcome(None, f"Confianza baja ({cell.confidence:.2f}): OCR leyó {raw!r}")
    if col.normalizer == "date":
        value = parse_date(raw, pivot_year=pivot_year)
        if value is None:
            return _Outcome(None, f"Fecha no válida: OCR leyó {raw!r}")
    elif col.normalizer == "digits":
        value = normalize_digits(raw)
    elif col.normalizer == "choice":
        result = match_option(raw, options, mapping.fuzzy_threshold, mapping.choice_aliases)
        if result.value is None:
            return _Outcome(None, f"No coincide con las opciones ({result.reason}): OCR leyó {raw!r}")
        value = result.value
    elif col.normalizer == "raw":
        value = raw
    else:
        value = normalize_text(raw)
    if col.rule:
        error = check_rule(col.rule, value, ficha_pattern=mapping.ficha_pattern)
        if error:
            return _Outcome(None, f"{error}: OCR leyó {raw!r}")
    return _Outcome(value, None)


def normalize_value(raw: str | None, col: ColumnMap, options: list[str], mapping: FieldMapping, pivot_year: int) -> str | None:
    """The form value a correctly read cell `raw` becomes (None when it is not a valid value)."""
    return _normalize(Cell(value=raw, confidence=1.0), col, options, mapping, pivot_year).value


def _apply(
    cells: dict[str, Cell],
    columns: dict[str, ColumnMap],
    schema: FormSchema,
    mapping: FieldMapping,
    pivot_year: int,
    answers: dict[str, str],
    flags: dict[str, str],
    confidences: list[float],
) -> None:
    for name, col in columns.items():
        if not col.enabled:
            continue
        cell = cells.get(name)
        if cell is None or not cell.text:
            continue
        questions = [q for t in col.targets if (q := schema.get(t))]
        if not questions:
            continue
        outcome = _normalize(cell, col, questions[0].options, mapping, pivot_year)
        if cell.confidence is not None:
            confidences.append(cell.confidence)
        for q in questions:
            if outcome.value is not None:
                answers[q.key] = outcome.value
            elif outcome.flag:
                flags[q.key] = outcome.flag


def _is_blank(cells: dict[str, Cell]) -> bool:
    return not any(c.text for name, c in cells.items() if name != "observaciones")


def rows_from_extraction(
    extraction: PageExtraction,
    schema: FormSchema,
    mapping: FieldMapping,
    source: SourceRef,
    pivot_year: int | None = None,
) -> list[TrackerRow]:
    pivot = pivot_year or date.today().year
    header_answers: dict[str, str] = {}
    header_flags: dict[str, str] = {}
    header_conf: list[float] = []
    _apply(extraction.header, mapping.header, schema, mapping, pivot, header_answers, header_flags, header_conf)

    rows: list[TrackerRow] = []
    for index, extracted in enumerate(extraction.rows, start=1):
        if extracted.empty or _is_blank(extracted.cells):
            continue
        number = extracted.row_number or index
        answers = dict(header_answers)
        flags = dict(header_flags)
        confidences = list(header_conf)
        _apply(extracted.cells, mapping.ocr_columns, schema, mapping, pivot, answers, flags, confidences)
        rows.append(
            TrackerRow(
                row_id=make_row_id(source.file_id, source.page, number),
                status=Status.EXTRACTED,
                answers=answers,
                source_file_name=source.file_name,
                source_file_id=source.file_id,
                source_file_link=source.link,
                source_page=source.page,
                source_row=number,
                ocr_confidence=round(min(confidences), 2) if confidences else None,
                ocr_flags=flags,
            )
        )
    return rows


def flagged_ratio(rows: list[TrackerRow]) -> float:
    """Fraction of mapped cells that were flagged. 1.0 when nothing was extracted."""
    filled = sum(len(r.answers) for r in rows)
    flagged = sum(len(r.ocr_flags) for r in rows)
    total = filled + flagged
    return 1.0 if total == 0 else flagged / total


def header_answers(extraction: PageExtraction, schema: FormSchema, mapping: FieldMapping, pivot_year: int | None = None) -> dict[str, str]:
    """Answers that come from the page header (copied into every row of the page)."""
    answers: dict[str, str] = {}
    _apply(extraction.header, mapping.header, schema, mapping, pivot_year or date.today().year, answers, {}, [])
    return answers


def unread_row_placeholder(source: SourceRef, row_number: int, answers: dict[str, str] | None = None) -> TrackerRow:
    """Tracker row for a printed row the OCR could not read, so a human types it in."""
    return TrackerRow(
        row_id=make_row_id(source.file_id, source.page, row_number),
        status=Status.NEEDS_REVIEW,
        answers=dict(answers or {}),
        source_file_name=source.file_name,
        source_file_id=source.file_id,
        source_file_link=source.link,
        source_page=source.page,
        source_row=row_number,
        error=f"fila no leída (página {source.page}, fila {row_number})",
    )
