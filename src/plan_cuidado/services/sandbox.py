"""Use case: clone the form, submit a few rows to the clone and compare answers."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from plan_cuidado.domain.mapping import FieldMapping
from plan_cuidado.domain.payload import build_submission, prefilled_url
from plan_cuidado.domain.sandbox import build_forms_api_requests, compare_answers, response_to_answers
from plan_cuidado.domain.schema import FormSchema, parse_form_html, to_form_response_url
from plan_cuidado.domain.tracker import Status
from plan_cuidado.domain.validate import validate_row
from plan_cuidado.ports import FormSubmitter, TrackerStore


class FormsApi(Protocol):
    def create_form(self, title: str, requests: list[dict]) -> tuple[str, str]: ...
    def question_titles(self, form_id: str) -> dict[str, str]: ...
    def list_responses(self, form_id: str) -> list[dict]: ...


@dataclass
class SandboxReport:
    form_id: str = ""
    responder_url: str = ""
    submitted: int = 0
    failed: int = 0
    mismatches: list[tuple[str, str, str, str]] = field(default_factory=list)


def run_sandbox(
    forms_api: FormsApi,
    tracker: TrackerStore,
    schema: FormSchema,
    mapping: FieldMapping,
    fetch_html: Callable[[str], str],
    submitter: FormSubmitter,
    rows_limit: int = 3,
    delay_seconds: float = 1.5,
    sleep: Callable[[float], None] = time.sleep,
) -> SandboxReport:
    rows = [r for r in tracker.read_rows() if r.status is Status.READY and not validate_row(r, schema, mapping)[0]]
    rows = rows[:rows_limit]
    if not rows:
        raise RuntimeError("No hay filas READY para probar. Ejecute `pc validate` primero.")
    form_id, responder_url = forms_api.create_form(f"[SANDBOX] {schema.title}", build_forms_api_requests(schema))
    report = SandboxReport(form_id=form_id, responder_url=responder_url)
    clone = parse_form_html(fetch_html(responder_url))
    url = to_form_response_url(responder_url)
    sent = []
    for i, row in enumerate(rows):
        if i:
            sleep(delay_seconds)
        result = submitter.submit(url, build_submission(row, clone), prefilled_url(row, clone))
        if result.ok:
            report.submitted += 1
            sent.append(row)
        else:
            report.failed += 1
    titles = forms_api.question_titles(form_id)
    responses = forms_api.list_responses(form_id)
    for row, response in zip(sent, responses):
        for key, expected, got in compare_answers(row, response_to_answers(response, titles)):
            report.mismatches.append((row.row_id, key, expected, got))
    return report
