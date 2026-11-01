from plan_cuidado.domain.tracker import Status, TrackerRow
from plan_cuidado.ports import SubmitResult
from plan_cuidado.services.submit import SubmitOptions, submit_ready
from tests.fakes import FakeSubmitter, FakeTracker
from tests.test_validate import complete_answers


def rows(schema, n, status=Status.READY):
    return [TrackerRow(row_id=f"r{i}", status=status, answers=complete_answers(schema)) for i in range(n)]


def run(schema, mapping, tracker, submitter, **opts):
    sleeps = []
    report = submit_ready(
        tracker, schema, mapping, submitter, SubmitOptions(**opts), sleep=sleeps.append, now=lambda: "2026-08-15T10:00:00"
    )
    return report, sleeps


def test_submits_ready_rows_and_marks_each_immediately(schema, mapping):
    tracker = FakeTracker(rows(schema, 3))
    submitter = FakeSubmitter()
    report, sleeps = run(schema, mapping, tracker, submitter, delay_seconds=1.5)
    assert report.submitted == 3
    assert all(r.status is Status.SUBMITTED and r.submitted_at for r in tracker.rows)
    assert tracker.update_calls == [["r0"], ["r1"], ["r2"]]
    assert sleeps == [1.5, 1.5]
    url, payload = submitter.sent[0]
    assert url.endswith("/formResponse")
    assert ("entry.136813167", "0202012345") in payload


def test_never_resubmits_submitted_rows(schema, mapping):
    tracker = FakeTracker(rows(schema, 2, Status.SUBMITTED))
    submitter = FakeSubmitter()
    report, _ = run(schema, mapping, tracker, submitter)
    assert submitter.sent == [] and report.submitted == 0


def test_only_ready_rows_are_sent(schema, mapping):
    data = rows(schema, 1) + [
        TrackerRow(row_id="nr", status=Status.NEEDS_REVIEW, answers=complete_answers(schema)),
        TrackerRow(row_id="ex", status=Status.EXTRACTED, answers=complete_answers(schema)),
        TrackerRow(row_id="fa", status=Status.FAILED, answers=complete_answers(schema)),
    ]
    submitter = FakeSubmitter()
    run(schema, mapping, FakeTracker(data), submitter)
    assert len(submitter.sent) == 1


def test_limit(schema, mapping):
    submitter = FakeSubmitter()
    report, _ = run(schema, mapping, FakeTracker(rows(schema, 5)), submitter, limit=2)
    assert len(submitter.sent) == 2 and report.submitted == 2


def test_dry_run_sends_nothing_and_changes_nothing(schema, mapping):
    tracker = FakeTracker(rows(schema, 2))
    submitter = FakeSubmitter()
    report, _ = run(schema, mapping, tracker, submitter, dry_run=True)
    assert submitter.sent == []
    assert len(report.previews) == 2 and "usp=pp_url" in report.previews[0]
    assert all(r.status is Status.READY for r in tracker.rows)


def test_failure_marks_row_failed_and_continues(schema, mapping):
    tracker = FakeTracker(rows(schema, 3))
    submitter = FakeSubmitter([SubmitResult(False, "HTTP 400"), RuntimeError("red caída"), SubmitResult(True)])
    report, _ = run(schema, mapping, tracker, submitter)
    assert report.failed == 2 and report.submitted == 1
    assert tracker.get("r0").status is Status.FAILED and tracker.get("r0").error == "HTTP 400"
    assert "red caída" in tracker.get("r1").error
    assert tracker.get("r2").status is Status.SUBMITTED


def test_row_edited_into_invalid_state_is_sent_back_to_review(schema, mapping):
    bad = TrackerRow(row_id="b", status=Status.READY, answers={"barrio": "X"})
    tracker = FakeTracker([bad])
    submitter = FakeSubmitter()
    report, _ = run(schema, mapping, tracker, submitter)
    assert submitter.sent == [] and report.skipped_invalid == 1
    assert tracker.get("b").status is Status.NEEDS_REVIEW


def test_form_url_override_uses_target_schema(schema, mapping):
    target = schema.model_copy(
        update={
            "public_id": "e/CLONE",
            "questions": [q.model_copy(update={"entry_id": q.entry_id + 1}) for q in schema.questions],
        }
    )
    submitter = FakeSubmitter()
    submit_ready(
        FakeTracker(rows(schema, 1)), schema, mapping, submitter,
        SubmitOptions(form_url="https://docs.google.com/forms/d/e/CLONE/viewform?usp=sf_link"),
        target_schema=target, sleep=lambda s: None,
    )
    url, payload = submitter.sent[0]
    assert url == "https://docs.google.com/forms/d/e/CLONE/formResponse"
    assert ("entry.136813168", "0202012345") in payload
