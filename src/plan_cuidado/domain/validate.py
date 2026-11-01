"""Row validation: required, options, format rules, conditionals and OCR flags."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from plan_cuidado.domain.mapping import FieldMapping
from plan_cuidado.domain.rules import check_rule
from plan_cuidado.domain.schema import FormSchema, QuestionType
from plan_cuidado.domain.tracker import Status, TrackerRow

CHECKBOX_SEPARATOR = ";"


class IssueKind(StrEnum):
    REQUIRED = "required"  # red
    NOT_IN_OPTIONS = "not_in_options"  # orange
    INVALID = "invalid"  # orange
    LOW_CONFIDENCE = "low_confidence"  # yellow
    DISAGREEMENT = "disagreement"  # purple: OCR models read a critical cell differently


@dataclass(frozen=True)
class Issue:
    row_id: str
    key: str
    kind: IssueKind
    message: str


def split_multi(value: str) -> list[str]:
    return [v.strip() for v in value.split(CHECKBOX_SEPARATOR) if v.strip()]


def validate_row(row: TrackerRow, schema: FormSchema, mapping: FieldMapping) -> tuple[list[Issue], Status]:
    issues: list[Issue] = []
    answers = {k: (v or "").strip() for k, v in row.answers.items()}
    conditional_required = {k for c in mapping.conditionals if c.when.holds(answers) for k in c.require}
    expected = {
        c.default.field: c.default.value for c in mapping.conditionals if c.default and c.when.holds(answers)
    }

    def add(key: str, kind: IssueKind, message: str) -> None:
        issues.append(Issue(row.row_id, key, kind, message))

    for q in schema.questions:
        value = answers.get(q.key, "")
        disagreement = row.ocr_disagreements.get(q.key)
        # Purple wins over every other mark until the user edits the cell (or clears the note).
        if disagreement and value == str(disagreement.get("value") or "").strip():
            add(q.key, IssueKind.DISAGREEMENT, str(disagreement.get("note") or "Los modelos de OCR no coinciden"))
            continue
        if not value:
            if q.key in row.ocr_flags:
                add(q.key, IssueKind.LOW_CONFIDENCE, row.ocr_flags[q.key])
            elif q.required or q.key in conditional_required:
                add(q.key, IssueKind.REQUIRED, "Obligatoria sin respuesta")
            continue
        if q.options and not q.has_other:
            values = split_multi(value) if q.type is QuestionType.CHECKBOX else [value]
            bad = [v for v in values if v not in q.options]
            if bad:
                add(q.key, IssueKind.NOT_IN_OPTIONS, f"Valor fuera de las opciones: {', '.join(bad)}")
                continue
        if rule := mapping.rules.get(q.key):
            error = check_rule(rule, value, ficha_pattern=mapping.ficha_pattern)
            if error:
                add(q.key, IssueKind.INVALID, error.capitalize())
                continue
        if q.key in expected and value != expected[q.key]:
            add(q.key, IssueKind.INVALID, f"Según las respuestas anteriores debería ser {expected[q.key]!r}")

    return issues, (Status.READY if not issues else Status.NEEDS_REVIEW)
