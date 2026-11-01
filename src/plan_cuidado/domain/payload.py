"""Build the formResponse POST payload and pre-filled review URLs."""

from __future__ import annotations

from urllib.parse import urlencode

from plan_cuidado.domain.schema import FormSchema, Question, QuestionType, viewform_url
from plan_cuidado.domain.tracker import TrackerRow
from plan_cuidado.domain.validate import split_multi

Payload = list[tuple[str, str]]


def _entries(q: Question, value: str, for_prefill: bool) -> Payload:
    name = f"entry.{q.entry_id}"
    if q.type is QuestionType.CHECKBOX:
        return [(name, v) for v in split_multi(value)]
    if q.type is QuestionType.DATE and not for_prefill:
        y, m, d = value.split("-")
        parts = [(f"{name}_month", str(int(m))), (f"{name}_day", str(int(d)))]
        if q.include_year:
            parts.insert(0, (f"{name}_year", y))
        return parts
    if q.type is QuestionType.TIME and not for_prefill:
        h, mi = value.split(":")[:2]
        return [(f"{name}_hour", h.zfill(2)), (f"{name}_minute", mi.zfill(2))]
    if q.has_other and q.options and value not in q.options:
        return [(name, "__other_option__"), (f"{name}.other_option_response", value)]
    return [(name, value)]


def build_submission(row: TrackerRow, schema: FormSchema) -> Payload:
    payload: Payload = []
    for q in schema.questions:
        value = row.answer(q.key)
        if value:
            payload.extend(_entries(q, value, for_prefill=False))
    payload.append(("fvv", "1"))
    payload.append(("pageHistory", ",".join(str(i) for i in range(schema.section_count))))
    return payload


def prefilled_url(row: TrackerRow, schema: FormSchema) -> str:
    params: Payload = [("usp", "pp_url")]
    for q in schema.questions:
        value = row.answer(q.key)
        if value:
            params.extend(_entries(q, value, for_prefill=True))
    return f"{viewform_url(schema)}?{urlencode(params)}"
