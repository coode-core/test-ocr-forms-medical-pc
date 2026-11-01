"""Compare critical cells read by two or more OCR models after normalization."""

from __future__ import annotations

from dataclasses import replace

from plan_cuidado.domain.tracker import TrackerRow


def _reading(row: TrackerRow, key: str) -> str:
    value = row.answer(key)
    if value:
        return repr(value)
    flag = row.ocr_flags.get(key)
    return f"(vacío: {flag})" if flag else "(vacío)"


def _majority(values: list[str]) -> str | None:
    """The non-empty value read by more than half of the readers, if any."""
    filled = [v for v in values if v]
    best = max(set(filled), key=filled.count, default=None)
    return best if best is not None and filled.count(best) * 2 > len(values) else None


def apply_consensus(
    primary: list[TrackerRow], primary_model: str, others: dict[str, list[TrackerRow]], critical: list[str]
) -> list[TrackerRow]:
    """Record a disagreement (purple) for each critical cell where another model's normalized value
    differs from the primary's. The cell keeps the value most readers agree on (strict majority, so
    with three or more readers) or else the primary's. Rows another model did not read are not compared."""
    if not others:
        return primary
    by_model = {m: {r.source_row: r for r in rows} for m, rows in others.items()}
    out = []
    for row in primary:
        disagreements = dict(row.ocr_disagreements)
        answers = dict(row.answers)
        for key in critical:
            mine = row.answer(key)
            readings = []
            values = [mine]
            for model, rows in by_model.items():
                other = rows.get(row.source_row)
                if other is None:
                    continue
                theirs = other.answer(key)
                values.append(theirs)
                if theirs != mine and (theirs or mine):
                    readings.append(f"{model}: {_reading(other, key)}")
            if readings:
                chosen = _majority(values) or mine
                note = "Los modelos de OCR no coinciden. " + "; ".join(
                    [f"{primary_model}: {_reading(row, key)}", *readings]
                ) + ". Verifique con la planilla."
                disagreements[key] = {"value": chosen, "note": note}
                if chosen != mine:
                    answers[key] = chosen
        changed = disagreements != row.ocr_disagreements or answers != row.answers
        out.append(replace(row, ocr_disagreements=disagreements, answers=answers) if changed else row)
    return out
