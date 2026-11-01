"""Tracker rows, columns, status state machine and idempotency helpers."""

from __future__ import annotations

import json
from dataclasses import dataclass, field, replace
from enum import StrEnum

from plan_cuidado.domain.schema import FormSchema


class Status(StrEnum):
    EXTRACTED = "EXTRACTED"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    READY = "READY"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"


_ALLOWED: dict[Status, set[Status]] = {
    Status.EXTRACTED: {Status.NEEDS_REVIEW, Status.READY},
    Status.NEEDS_REVIEW: {Status.NEEDS_REVIEW, Status.READY},
    Status.READY: {Status.NEEDS_REVIEW, Status.READY, Status.SUBMITTED, Status.FAILED},
    Status.FAILED: {Status.NEEDS_REVIEW, Status.READY, Status.FAILED},
    Status.SUBMITTED: set(),
}


class InvalidTransition(ValueError):
    pass


@dataclass(frozen=True)
class TrackerRow:
    row_id: str
    status: Status = Status.EXTRACTED
    answers: dict[str, str] = field(default_factory=dict)
    source_file_name: str = ""
    source_file_id: str = ""
    source_file_link: str = ""
    source_page: int | None = None
    source_row: int | None = None
    ocr_confidence: float | None = None
    ocr_flags: dict[str, str] = field(default_factory=dict)
    # Critical cells where the consensus models disagree: key -> {"value": primary, "note": readings}
    ocr_disagreements: dict[str, dict] = field(default_factory=dict)
    submitted_at: str = ""
    error: str = ""

    def answer(self, key: str) -> str:
        return (self.answers.get(key) or "").strip()


def transition(row: TrackerRow, new: Status, **changes) -> TrackerRow:
    if new not in _ALLOWED[row.status]:
        raise InvalidTransition(f"{row.row_id}: {row.status} -> {new}")
    return replace(row, status=new, **changes)


# Control columns. `ocr_flags` (JSON key -> reason) keeps per-cell OCR review notes;
# `ocr_disagreements` (JSON key -> {value, note}) keeps the readings of disagreeing OCR models.
CONTROL_COLUMNS = [
    "row_id",
    "status",
    "source_file_link",
    "source_page",
    "source_row",
    "source_file_name",
    "source_file_id",
    "ocr_confidence",
    "ocr_flags",
    "ocr_disagreements",
    "submitted_at",
    "error",
]
_LEADING_CONTROL = ["row_id", "status", "source_file_link", "source_page", "source_row"]


@dataclass(frozen=True)
class Column:
    key: str
    header: str
    is_question: bool


def build_columns(schema: FormSchema) -> list[Column]:
    lead = [Column(k, k, False) for k in _LEADING_CONTROL]
    questions = [Column(q.key, q.label, True) for q in schema.questions]
    trail = [Column(k, k, False) for k in CONTROL_COLUMNS if k not in _LEADING_CONTROL]
    cols = [*lead, *questions, *trail]
    seen: set[str] = set()
    for c in cols:
        if c.header in seen:
            raise ValueError(f"Duplicated column header: {c.header}")
        seen.add(c.header)
    return cols


_JSON_COLUMNS = {"ocr_flags", "ocr_disagreements"}


def _json_dict(raw: str) -> dict:
    try:
        value = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _control_value(row: TrackerRow, key: str) -> str:
    value = getattr(row, key)
    if key in _JSON_COLUMNS:
        return json.dumps(value, ensure_ascii=False) if value else ""
    if value is None:
        return ""
    return str(value)


def row_to_values(row: TrackerRow, columns: list[Column]) -> list[str]:
    return [row.answers.get(c.key, "") if c.is_question else _control_value(row, c.key) for c in columns]


def _to_int(value: str) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _to_float(value: str) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


def values_to_row(headers: list[str], values: list, schema: FormSchema) -> TrackerRow:
    by_header = {c.header: c for c in build_columns(schema)}
    answers: dict[str, str] = {}
    control: dict[str, str] = {}
    for i, header in enumerate(headers):
        col = by_header.get(header)
        if col is None:
            continue
        raw = values[i] if i < len(values) else ""
        value = "" if raw is None else str(raw)
        if col.is_question:
            if value != "":
                answers[col.key] = value
        else:
            control[col.key] = value
    flags = _json_dict(control.get("ocr_flags", ""))
    disagreements = {k: v for k, v in _json_dict(control.get("ocr_disagreements", "")).items() if isinstance(v, dict)}
    status_raw = control.get("status", "") or Status.EXTRACTED.value
    try:
        status = Status(status_raw.strip().upper())
    except ValueError:
        status = Status.NEEDS_REVIEW
    return TrackerRow(
        row_id=control.get("row_id", ""),
        status=status,
        answers=answers,
        source_file_name=control.get("source_file_name", ""),
        source_file_id=control.get("source_file_id", ""),
        source_file_link=control.get("source_file_link", ""),
        source_page=_to_int(control.get("source_page", "")),
        source_row=_to_int(control.get("source_row", "")),
        ocr_confidence=_to_float(control.get("ocr_confidence", "")),
        ocr_flags=flags,
        ocr_disagreements=disagreements,
        submitted_at=control.get("submitted_at", ""),
        error=control.get("error", ""),
    )


def make_row_id(file_id: str, page: int, row: int) -> str:
    return f"{file_id}-p{page}-r{row}"


def processed_file_ids(rows: list[TrackerRow]) -> set[str]:
    return {r.source_file_id for r in rows if r.source_file_id}


def _dedupe_key(row: TrackerRow, keys: list[str]) -> tuple[str, ...] | None:
    values = tuple(row.answer(k) for k in keys)
    return values if all(values) else None


def dedupe_new_rows(
    existing: list[TrackerRow], new: list[TrackerRow], keys: list[str]
) -> tuple[list[TrackerRow], list[TrackerRow]]:
    seen_ids = {r.row_id for r in existing}
    seen_keys = {k for r in existing if (k := _dedupe_key(r, keys))}
    kept: list[TrackerRow] = []
    skipped: list[TrackerRow] = []
    for row in new:
        key = _dedupe_key(row, keys)
        if row.row_id in seen_ids or (key and key in seen_keys):
            skipped.append(row)
            continue
        kept.append(row)
        seen_ids.add(row.row_id)
        if key:
            seen_keys.add(key)
    return kept, skipped


def row_to_header_values(row: TrackerRow, headers: list[str], schema: FormSchema) -> list[str | None]:
    """Values aligned to the Sheet's actual header order. Unknown headers -> None (untouched)."""
    by_header = {c.header: c for c in build_columns(schema)}
    out: list[str | None] = []
    for header in headers:
        col = by_header.get(header)
        if col is None:
            out.append(None)
        elif col.is_question:
            out.append(row.answers.get(col.key, ""))
        else:
            out.append(_control_value(row, col.key))
    return out
