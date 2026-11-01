import pytest

from plan_cuidado.domain.tracker import Status, TrackerRow
from plan_cuidado.ports import DriveFile
from plan_cuidado.services.reset import apply_reset, plan_reset
from tests.fakes import FakeDrive, FakeTracker, MemoryCache


def drive_with_done():
    return FakeDrive(
        done=[
            DriveFile("A", "a.pdf", location="02_processed/2026-08"),
            DriveFile("B", "b.pdf", location="03_failed"),
            DriveFile("C", "c.pdf", location="02_processed/2026-08"),
        ]
    )


def tracker_rows():
    return FakeTracker(
        [
            TrackerRow(row_id="A-p1-r1", source_file_id="A", status=Status.READY),
            TrackerRow(row_id="A-p1-r2", source_file_id="A", status=Status.SUBMITTED),
            TrackerRow(row_id="C-p1-r1", source_file_id="C", status=Status.NEEDS_REVIEW),
            TrackerRow(row_id="X-p1-r1", source_file_id="X"),
        ]
    )


def test_plan_by_name_or_id():
    drive, tracker = drive_with_done(), tracker_rows()
    by_name = plan_reset(drive, tracker, file="a.pdf")
    by_id = plan_reset(drive, tracker, file="A")
    for plan in (by_name, by_id):
        (item,) = plan.items
        assert item.file.id == "A"
        assert item.delete_row_ids == ["A-p1-r1"]
        assert item.kept_submitted == 1


def test_plan_name_without_extension_and_unknown_file():
    drive, tracker = drive_with_done(), tracker_rows()
    assert plan_reset(drive, tracker, file="c").items[0].file.id == "C"
    with pytest.raises(LookupError):
        plan_reset(drive, tracker, file="nope.pdf")


def test_all_unsubmitted_excludes_files_with_submitted_rows():
    plan = plan_reset(drive_with_done(), tracker_rows(), all_unsubmitted=True)
    assert [i.file.id for i in plan.items] == ["B", "C"]


def test_apply_moves_deletes_and_clears_cache_but_keeps_submitted():
    drive, tracker, cache = drive_with_done(), tracker_rows(), MemoryCache()
    cache.put("A", 1, "m", {"content": "{}"}, "header")
    report = apply_reset(plan_reset(drive, tracker, file="a.pdf"), drive, tracker, cache, clear_cache=True)
    assert drive.moved_to_inbox == ["A"]
    assert [r.row_id for r in tracker.rows] == ["A-p1-r2", "C-p1-r1", "X-p1-r1"]
    assert report.files_moved == 1 and report.rows_deleted == 1 and report.rows_kept_submitted == 1
    assert report.cache_entries_cleared == 1 and cache.data == {}


def test_apply_without_clear_cache_keeps_cache():
    drive, tracker, cache = drive_with_done(), tracker_rows(), MemoryCache()
    cache.put("C", 1, "m", {"content": "{}"})
    apply_reset(plan_reset(drive, tracker, file="C"), drive, tracker, cache, clear_cache=False)
    assert cache.data


def test_plan_requires_a_target():
    with pytest.raises(ValueError):
        plan_reset(drive_with_done(), tracker_rows())
