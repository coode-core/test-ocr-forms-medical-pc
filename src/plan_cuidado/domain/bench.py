"""Score OCR extractions against a hand-transcribed ground-truth page.

Ground truth JSON: {"page": 1, "header": {field: text}, "rows": [{"row": <printed No.>,
<ocr column>: text, ...}], "uncertain": [...]} (`uncertain` is only a note for the human).
Rows are matched by printed row number ("row"), or by order when it is missing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from datetime import date

from plan_cuidado.domain.mapping import FieldMapping
from plan_cuidado.domain.normalize import fold
from plan_cuidado.domain.ocr import ExtractedRow, PageExtraction, normalize_value
from plan_cuidado.domain.schema import FormSchema
from plan_cuidado.domain.tracker import TrackerRow
from plan_cuidado.domain.validate import validate_row

_NOT_FIELDS = {"row", "row_number", "empty"}


def _canon(value: str | None) -> str:
    return re.sub(r"[^A-Z0-9]", "", fold(value or ""))


@dataclass
class FieldScore:
    correct: int = 0
    total: int = 0

    @property
    def accuracy(self) -> float:
        return self.correct / self.total if self.total else 1.0


@dataclass
class PageScore:
    fields: dict[str, FieldScore] = field(default_factory=dict)
    critical_columns: list[str] = field(default_factory=list)
    missing_rows: list[int] = field(default_factory=list)  # rows with data in the truth, not read

    @staticmethod
    def _sum(scores) -> FieldScore:
        scores = list(scores)
        return FieldScore(sum(s.correct for s in scores), sum(s.total for s in scores))

    @property
    def overall(self) -> float:
        return self._sum(self.fields.values()).accuracy

    @property
    def critical(self) -> FieldScore:
        return self._sum(s for name, s in self.fields.items() if name in self.critical_columns)


def _add(score: PageScore, name: str, truth: str | None, predicted: str | None) -> None:
    t, p = _canon(truth), _canon(predicted)
    entry = score.fields.setdefault(name, FieldScore())
    if not t and not p:
        return
    entry.total += 1
    entry.correct += int(t == p)


def _truth_rows(truth: dict) -> dict[int, dict]:
    return {int(r.get("row") or i): r for i, r in enumerate(truth.get("rows") or [], start=1)}


def _predicted_rows(predicted: PageExtraction) -> dict[int, ExtractedRow]:
    return {r.row_number or i: r for i, r in enumerate(predicted.rows, start=1)}


def _value(row: ExtractedRow | None, name: str) -> str | None:
    cell = row.cells.get(name) if row is not None and not row.empty else None
    return cell.value if cell else None


def score_page(truth: dict, predicted: PageExtraction, critical_columns: list[str] | None = None) -> PageScore:
    score = PageScore(critical_columns=list(critical_columns or []))
    for name, value in (truth.get("header") or {}).items():
        cell = predicted.header.get(name)
        _add(score, name, value, cell.value if cell else None)
    pred = _predicted_rows(predicted)
    for number, truth_row in sorted(_truth_rows(truth).items()):
        row = pred.get(number)
        values = {k: v for k, v in truth_row.items() if k not in _NOT_FIELDS}
        if any(_canon(v) for v in values.values()) and (row is None or row.empty):
            score.missing_rows.append(number)
        for name, value in values.items():
            _add(score, name, value, _value(row, name))
    return score


@dataclass
class ConsensusStats:
    compared: int = 0  # critical cells with a value in the truth
    disagreements: int = 0  # the two models differ -> a human checks it (purple)
    residual_errors: int = 0  # both models agree but are wrong -> slips through

    @property
    def disagreement_rate(self) -> float:
        return self.disagreements / self.compared if self.compared else 0.0

    @property
    def residual_rate(self) -> float:
        return self.residual_errors / self.compared if self.compared else 0.0


def consensus_stats(truth: dict, a: PageExtraction, b: PageExtraction, critical_columns: list[str]) -> ConsensusStats:
    stats = ConsensusStats()
    rows_a, rows_b = _predicted_rows(a), _predicted_rows(b)
    for number, truth_row in _truth_rows(truth).items():
        for name in critical_columns:
            t = _canon(truth_row.get(name))
            if not t:
                continue
            stats.compared += 1
            va, vb = _canon(_value(rows_a.get(number), name)), _canon(_value(rows_b.get(number), name))
            if va != vb:
                stats.disagreements += 1
            elif va != t:
                stats.residual_errors += 1
    return stats


@dataclass
class CellOutcomes:
    """What happens to the cells of one column once they reach the tracker."""

    ok: int = 0  # right value, no mark
    ok_marked: int = 0  # right value, but painted for review (extra human work)
    caught: int = 0  # wrong or empty, painted (purple/yellow/orange/red): a human fixes it
    silent: int = 0  # wrong or missing with no mark: slips through to the form

    @property
    def total(self) -> int:
        return self.ok + self.ok_marked + self.caught + self.silent

    @property
    def accuracy(self) -> float:
        return (self.ok + self.ok_marked) / self.total if self.total else 1.0


@dataclass
class RowsScore:
    fields: dict[str, CellOutcomes] = field(default_factory=dict)
    critical_columns: list[str] = field(default_factory=list)
    missing_rows: list[int] = field(default_factory=list)  # rows with data in the truth and no tracker row
    silent_cells: list[tuple[str, str]] = field(default_factory=list)  # ("r07", column) of critical cells
    marked_critical: int = 0  # critical cells painted for review

    @property
    def critical(self) -> CellOutcomes:
        out = CellOutcomes()
        for name, s in self.fields.items():
            if name in self.critical_columns:
                out.ok += s.ok
                out.ok_marked += s.ok_marked
                out.caught += s.caught
                out.silent += s.silent
        return out

    @property
    def overall(self) -> float:
        total = sum(s.total for s in self.fields.values())
        return sum(s.ok + s.ok_marked for s in self.fields.values()) / total if total else 1.0


def score_rows(
    truth: dict, rows: list[TrackerRow], schema: FormSchema, mapping: FieldMapping, pivot_year: int | None = None
) -> RowsScore:
    """Score the tracker rows (normalized, validated, consensus applied) against the truth:
    the truth cell goes through the same normalizer, a cell is "marked" when `pc validate`
    paints it (any issue on its question)."""
    pivot = pivot_year or date.today().year
    score = RowsScore(critical_columns=mapping.critical_columns())
    by_row = {r.source_row: r for r in rows}
    for number, truth_row in sorted(_truth_rows(truth).items()):
        row = by_row.get(number)
        marks = {i.key for i in validate_row(row, schema, mapping)[0]} if row is not None else set()
        values = {k: v for k, v in truth_row.items() if k not in _NOT_FIELDS}
        if row is None and any(_canon(v) for v in values.values()):
            score.missing_rows.append(number)
        for name, raw in values.items():
            col = mapping.ocr_columns.get(name)
            question = next((q for t in (col.targets if col and col.enabled else []) if (q := schema.get(t))), None)
            if question is None:
                continue
            expected = normalize_value(raw, col, question.options, mapping, pivot) if _canon(raw) else ""
            if expected is None:  # the truth itself is not a valid form value: not scorable
                continue
            predicted = row.answer(question.key) if row is not None else ""
            if not expected and not predicted:
                continue
            entry = score.fields.setdefault(name, CellOutcomes())
            marked = question.key in marks
            # free text is typed by hand anyway: spacing and punctuation are not errors
            same = _canon(predicted) == _canon(expected) if col.normalizer == "text" else predicted == expected
            if same:
                if marked:
                    entry.ok_marked += 1
                else:
                    entry.ok += 1
            elif marked:
                entry.caught += 1
            else:
                entry.silent += 1
                if name in score.critical_columns:
                    score.silent_cells.append((f"r{number:02d}", name))
            if marked and name in score.critical_columns:
                score.marked_critical += 1
    return score


def summarize_usage(usages: list[dict]) -> dict:
    total: dict = {}
    for usage in usages:
        for key in ("prompt_tokens", "completion_tokens", "cost"):
            if isinstance(usage.get(key), (int, float)):
                total[key] = total.get(key, 0) + usage[key]
    return total
