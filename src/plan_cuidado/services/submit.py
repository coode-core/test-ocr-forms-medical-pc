"""Use case: submit READY rows to the Google Form, one row at a time."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from plan_cuidado.domain.mapping import FieldMapping
from plan_cuidado.domain.payload import build_submission, prefilled_url
from plan_cuidado.domain.schema import FormSchema, form_response_url, to_form_response_url
from plan_cuidado.domain.tracker import Status, transition
from plan_cuidado.domain.validate import validate_row
from plan_cuidado.ports import FormSubmitter, TrackerStore


@dataclass
class SubmitOptions:
    dry_run: bool = False
    limit: int | None = None
    delay_seconds: float = 1.5
    form_url: str | None = None


@dataclass
class SubmitReport:
    submitted: int = 0
    failed: int = 0
    skipped_invalid: int = 0
    previews: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def submit_ready(
    tracker: TrackerStore,
    schema: FormSchema,
    mapping: FieldMapping,
    submitter: FormSubmitter,
    options: SubmitOptions,
    target_schema: FormSchema | None = None,
    sleep: Callable[[float], None] = time.sleep,
    now: Callable[[], str] = _now,
    log: Callable[[str], None] = lambda message: None,
) -> SubmitReport:
    target = target_schema or schema
    url = to_form_response_url(options.form_url) if options.form_url else form_response_url(target)
    report = SubmitReport()
    ready = [r for r in tracker.read_rows() if r.status is Status.READY]
    if options.limit is not None:
        ready = ready[: options.limit]
    sent_any = False
    for row in ready:
        issues, status = validate_row(row, schema, mapping)
        if status is not Status.READY:
            tracker.update_rows([transition(row, Status.NEEDS_REVIEW)])
            report.skipped_invalid += 1
            log(f"{row.row_id}: cambió desde la validación, vuelve a revisión ({len(issues)} problemas)")
            continue
        preview = prefilled_url(row, target)
        if options.dry_run:
            report.previews.append(preview)
            continue
        if sent_any:
            sleep(options.delay_seconds)
        sent_any = True
        try:
            result = submitter.submit(url, build_submission(row, target), preview)
            ok, detail = result.ok, result.detail
        except Exception as exc:
            ok, detail = False, f"{type(exc).__name__}: {exc}"
        if ok:
            tracker.update_rows([transition(row, Status.SUBMITTED, submitted_at=now(), error="")])
            report.submitted += 1
            log(f"{row.row_id}: enviado")
        else:
            tracker.update_rows([transition(row, Status.FAILED, error=detail)])
            report.failed += 1
            log(f"{row.row_id}: falló ({detail})")
    return report
