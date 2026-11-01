"""Use cases: fill static/derived values and validate tracker rows."""

from __future__ import annotations

from dataclasses import dataclass, field

from plan_cuidado.domain.mapping import FieldMapping
from plan_cuidado.domain.schema import FormSchema
from plan_cuidado.domain.static_fill import apply_static_and_derived
from plan_cuidado.domain.tracker import Status, transition
from plan_cuidado.domain.validate import Issue, validate_row
from plan_cuidado.ports import TrackerStore


def fill_static(tracker: TrackerStore, schema: FormSchema, mapping: FieldMapping, static_data: dict) -> int:
    static_data = {**mapping.static_defaults, **static_data}
    changed = []
    for row in tracker.read_rows():
        new = apply_static_and_derived(row, schema, mapping, static_data)
        if new != row:
            changed.append(new)
    if changed:
        tracker.update_rows(changed)
    return len(changed)


@dataclass
class ValidateReport:
    ready: int = 0
    needs_review: int = 0
    submitted: int = 0
    issues: list[Issue] = field(default_factory=list)


def validate_all(tracker: TrackerStore, schema: FormSchema, mapping: FieldMapping) -> ValidateReport:
    report = ValidateReport()
    changed = []
    reviewed = []
    for row in tracker.read_rows():
        if row.status is Status.SUBMITTED:
            report.submitted += 1
            continue
        issues, status = validate_row(row, schema, mapping)
        new = transition(row, status)
        if new != row:
            changed.append(new)
        reviewed.append(new)
        report.issues.extend(issues)
        if status is Status.READY:
            report.ready += 1
        else:
            report.needs_review += 1
    if changed:
        tracker.update_rows(changed)
    tracker.apply_review_marks(reviewed, report.issues)
    return report
