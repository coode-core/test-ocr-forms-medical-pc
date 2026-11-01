"""Use case: put already processed PDFs back in 01_inbox to read them again.

SUBMITTED tracker rows are never touched: they stay, and the next `pc ocr` skips them by
row_id while adding the other rows again.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from plan_cuidado.domain.tracker import Status, TrackerRow
from plan_cuidado.ports import DriveFile, DriveStore, OcrCache, TrackerStore


@dataclass
class ResetItem:
    file: DriveFile
    delete_row_ids: list[str] = field(default_factory=list)
    kept_submitted: int = 0


@dataclass
class ResetPlan:
    items: list[ResetItem] = field(default_factory=list)


@dataclass
class ResetReport:
    files_moved: int = 0
    rows_deleted: int = 0
    rows_kept_submitted: int = 0
    cache_entries_cleared: int = 0


def _matches(file: DriveFile, target: str) -> bool:
    name = file.name.lower()
    wanted = target.strip().lower()
    return file.id == target.strip() or name == wanted or name == f"{wanted}.pdf"


def _item(file: DriveFile, rows: list[TrackerRow]) -> ResetItem:
    mine = [r for r in rows if r.source_file_id == file.id]
    return ResetItem(
        file=file,
        delete_row_ids=[r.row_id for r in mine if r.status is not Status.SUBMITTED],
        kept_submitted=sum(r.status is Status.SUBMITTED for r in mine),
    )


def plan_reset(
    drive: DriveStore, tracker: TrackerStore, file: str | None = None, all_unsubmitted: bool = False
) -> ResetPlan:
    """`file` (name or Drive id) or `all_unsubmitted` (every processed/failed PDF without
    SUBMITTED rows). Raises LookupError if `file` is not in 02_processed/03_failed."""
    if not file and not all_unsubmitted:
        raise ValueError("Indique --file o --all-unsubmitted")
    done = drive.list_done()
    rows = tracker.read_rows()
    if file:
        found = [f for f in done if _matches(f, file)]
        if not found:
            raise LookupError(file)
        return ResetPlan([_item(f, rows) for f in found])
    items = [_item(f, rows) for f in done]
    return ResetPlan([i for i in items if i.kept_submitted == 0])


def apply_reset(
    plan: ResetPlan, drive: DriveStore, tracker: TrackerStore, cache: OcrCache | None, clear_cache: bool
) -> ResetReport:
    report = ResetReport()
    ids = [row_id for item in plan.items for row_id in item.delete_row_ids]
    if ids:
        # Rows first: if the move then fails, the PDF stays where it was and `pc reset` can be re-run.
        report.rows_deleted = tracker.delete_rows(ids)
    for item in plan.items:
        drive.move_to_inbox(item.file.id)
        report.files_moved += 1
        report.rows_kept_submitted += item.kept_submitted
        if clear_cache and cache is not None:
            report.cache_entries_cleared += cache.clear(item.file.id)
    return report
