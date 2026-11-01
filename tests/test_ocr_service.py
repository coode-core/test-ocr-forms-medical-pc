import re
import threading
import time
from datetime import date

import pytest

from plan_cuidado.domain.mapping import OcrConfig
from plan_cuidado.domain.ocr import HEADER_PROMPT
from plan_cuidado.domain.tracker import Status, TrackerRow
from plan_cuidado.ports import TransientError
from plan_cuidado.services.ocr import OcrService, OcrSettings, PagePipeline
from tests.fakes import FakeDrive, FakeRenderer, FakeTracker, FakeVision, MemoryCache, ScriptedPage
from tests.test_ocr_mapping import GOOD_ROW, cell

PRIMARY, FALLBACK, SECOND = "primary/model", "fallback/model", "second/model"
HEADER = {"ebs": cell("02"), "colaborador": cell("Colaboradora Prueba")}


def numbered(n, doc=None, **overrides):
    row = dict(GOOD_ROW, row_number=n, empty=False, numero_documento=cell(doc or f"10000000{n:02d}"))
    row.update(overrides)
    return row


def blank(n):
    return dict({k: cell(None, 0.0) for k in GOOD_ROW}, row_number=n, empty=True)


def sheet(count=6, blanks=(), **kwargs):
    rows = [blank(n) if n in blanks else numbered(n) for n in range(1, count + 1)]
    return ScriptedPage(HEADER, rows, **kwargs)


def layout(**changes):
    return OcrConfig(**{"strips": 3, "expected_rows": 6, **changes})


def make(schema, mapping, drive, vision, tracker=None, pages=1, cache=None, fallback=None, consensus=(), **lay):
    mapping = mapping.model_copy(update={"ocr": layout(**lay)})
    return OcrService(
        drive=drive,
        tracker=tracker or FakeTracker(),
        vision=vision,
        renderer=FakeRenderer(pages),
        cache=cache or MemoryCache(),
        schema=schema,
        mapping=mapping,
        settings=OcrSettings(
            model=PRIMARY, fallback_model=fallback, pivot_year=2026, consensus_models=list(consensus)
        ),
        today=lambda: date(2026, 8, 15),
    )


# --------------------------------------------------------------------------- strips + merge


def test_header_once_then_three_strips_merged_by_row_number(schema, mapping):
    page = sheet(6)
    vision = FakeVision({PRIMARY: page})
    tracker = FakeTracker()
    report = make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, tracker).run()
    images = [img for img, _ in vision.calls]
    assert images[0] == b"page1|crop0.000-1.000"  # header: whole page, low resolution
    assert len(images) == 4 and all(b"|crop" in i for i in images[1:])
    assert [r.source_row for r in tracker.rows] == [1, 2, 3, 4, 5, 6]  # overlap rows deduped
    assert report.rows_added == 6 and report.rows_unread == 0
    assert tracker.rows[0].answers["equipo_basico_en_salud"] == "EBS 02"


def test_blank_printed_rows_are_not_missing(schema, mapping):
    vision = FakeVision({PRIMARY: sheet(6, blanks={5, 6})})
    tracker = FakeTracker()
    make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, tracker).run()
    assert [r.source_row for r in tracker.rows] == [1, 2, 3, 4]
    assert not any("Return only row" in p for p in vision.by_model[PRIMARY].prompts)


def test_missing_row_is_requeried_with_targeted_crop(schema, mapping):
    page = sheet(6, skip={3})
    vision = FakeVision({PRIMARY: page})
    tracker = FakeTracker()
    report = make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, tracker).run()
    assert any("Return only row No. 3" in p for p in page.prompts)
    assert [r.source_row for r in tracker.rows] == [1, 2, 3, 4, 5, 6]
    assert report.rows_requeried == 1 and report.rows_unread == 0


def test_unreadable_row_becomes_needs_review_placeholder(schema, mapping):
    vision = FakeVision({PRIMARY: sheet(6, skip={3}, unreadable={3})})
    tracker = FakeTracker()
    report = make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, tracker).run()
    placeholder = next(r for r in tracker.rows if r.source_row == 3)
    assert placeholder.status is Status.NEEDS_REVIEW
    assert "fila no leída" in placeholder.error
    assert placeholder.source_page == 1 and placeholder.source_file_id == "F1"
    assert placeholder.row_id == "F1-p1-r3"
    assert report.rows_unread == 1


def test_trailing_rows_missing_up_to_expected_count_are_requeried(schema, mapping):
    # the model never mentions rows 5-6; the re-query says they are blank
    page = ScriptedPage(HEADER, [numbered(n) for n in range(1, 5)] + [blank(5), blank(6)], skip={5, 6})
    vision = FakeVision({PRIMARY: page})
    tracker = FakeTracker()
    report = make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, tracker).run()
    assert report.rows_unread == 0 and len(tracker.rows) == 4


def test_requery_can_be_disabled(schema, mapping):
    page = sheet(6, skip={3})
    vision = FakeVision({PRIMARY: page})
    report = make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, requery_missing=False).run()
    assert not any("Return only row" in p for p in page.prompts)
    assert report.rows_unread == 1


def test_rotation_reported_by_header_rotates_before_cropping_strips(schema, mapping):
    vision = FakeVision({PRIMARY: sheet(6, rotation=180)})
    tracker = FakeTracker()
    make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, tracker).run()
    images = [img for img, _ in vision.calls]
    assert images[1] == b"page1-rot180|crop0.000-1.000"
    assert all(i.startswith(b"page1-rot180") for i in images[2:])
    assert len(tracker.rows) == 6


def test_cache_avoids_paying_twice(schema, mapping):
    cache = MemoryCache()
    vision = FakeVision({PRIMARY: sheet(6)})
    make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, cache=cache).run()
    calls = len(vision.calls)
    make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, cache=cache).run()
    assert len(vision.calls) == calls


# --------------------------------------------------------------------------- cascade / files


def test_cascade_to_fallback_model_on_bad_page(schema, mapping):
    bad_rows = [dict({k: cell("???", 0.2) for k in GOOD_ROW}, row_number=n, empty=False) for n in range(1, 7)]
    vision = FakeVision({PRIMARY: ScriptedPage(HEADER, bad_rows), FALLBACK: sheet(6)})
    tracker = FakeTracker()
    report = make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, tracker, fallback=FALLBACK).run()
    assert report.pages_cascaded == 1
    assert tracker.rows[0].answers["barrio"] == "CIUDADELA MAIPORE"


def test_file_without_rows_fails(schema, mapping):
    drive = FakeDrive({"F1": b"pdf"})
    vision = FakeVision({PRIMARY: ScriptedPage(HEADER, [])})
    make(schema, mapping, drive, vision).run()
    assert drive.failed and "filas" in drive.failed[0][1]


def test_failed_file_goes_to_failed_with_error(schema, mapping):
    drive = FakeDrive({"F1": b"broken", "F2": b"pdf"})
    report = make(schema, mapping, drive, FakeVision({PRIMARY: sheet(6)})).run()
    assert report.files_failed == 1 and report.files_processed == 1
    assert "PDF dañado" in drive.failed[0][1]


def test_skips_files_already_in_tracker_but_moves_them(schema, mapping):
    drive = FakeDrive({"F1": b"pdf"})
    tracker = FakeTracker([TrackerRow(row_id="x", source_file_id="F1")])
    vision = FakeVision({PRIMARY: sheet(6)})
    report = make(schema, mapping, drive, vision, tracker).run()
    assert report.files_skipped == 1 and vision.calls == []
    assert drive.processed == [("F1", "2026-08")]


def test_file_with_only_submitted_rows_is_reprocessed_without_duplicates(schema, mapping):
    # after `pc reset`, submitted rows stay in the tracker and the PDF is back in the inbox
    kept = TrackerRow(row_id="F1-p1-r2", status=Status.SUBMITTED, source_file_id="F1", answers={"x": "1"})
    tracker = FakeTracker([kept])
    report = make(schema, mapping, FakeDrive({"F1": b"pdf"}), FakeVision({PRIMARY: sheet(6)}), tracker).run()
    assert report.files_processed == 1
    assert [r.row_id for r in tracker.rows].count("F1-p1-r2") == 1
    assert len(tracker.rows) == 6


def test_rows_are_extracted_status(schema, mapping):
    tracker = FakeTracker()
    make(schema, mapping, FakeDrive({"F1": b"pdf"}), FakeVision({PRIMARY: sheet(6)}), tracker).run()
    assert tracker.rows[0].status is Status.EXTRACTED


# --------------------------------------------------------------------------- transient network errors


def test_network_failure_leaves_file_in_inbox_and_continues(schema, mapping):
    drive = FakeDrive({"F1": TransientError("sin conexión"), "F2": b"pdf"})
    report = make(schema, mapping, drive, FakeVision({PRIMARY: sheet(6)})).run()
    assert "F1" in drive.inbox and drive.failed == []
    assert report.files_transient == ["F1.pdf"] and report.files_processed == 1
    assert report.stopped_early is False


def test_vision_network_failure_does_not_cascade_and_stops_after_two_files(schema, mapping):
    drive = FakeDrive({"F1": b"pdf", "F2": b"pdf", "F3": b"pdf"})
    vision = FakeVision({PRIMARY: [TransientError("timeout")], FALLBACK: sheet(6)})
    report = make(schema, mapping, drive, vision, fallback=FALLBACK).run()
    assert {m for _, m in vision.calls} == {PRIMARY}  # the fallback would fail the same way
    assert set(drive.inbox) == {"F1", "F2", "F3"} and drive.failed == []
    assert report.files_transient == ["F1.pdf", "F2.pdf"] and report.stopped_early is True


# --------------------------------------------------------------------------- consensus


def test_consensus_flags_disagreeing_critical_cells_only(schema, mapping):
    other_rows = [numbered(n) for n in range(1, 7)]
    other_rows[1] = dict(other_rows[1], telefono=cell("3101234560"), barrio=cell("ARIZONA"))
    vision = FakeVision({PRIMARY: sheet(6), SECOND: ScriptedPage(HEADER, other_rows)})
    tracker = FakeTracker()
    report = make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, tracker, consensus=[SECOND]).run()
    row2 = next(r for r in tracker.rows if r.source_row == 2)
    assert row2.answers["telefono"] == "3101234567"  # primary value kept
    assert set(row2.ocr_disagreements) == {"telefono"}  # barrio is not critical
    assert "3101234560" in row2.ocr_disagreements["telefono"]["note"]
    assert all(not r.ocr_disagreements for r in tracker.rows if r.source_row != 2)
    assert report.disagreements == 1


def test_consensus_results_are_cached_per_model(schema, mapping):
    cache = MemoryCache()
    vision = FakeVision({PRIMARY: sheet(6), SECOND: sheet(6)})
    make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, cache=cache, consensus=[SECOND]).run()
    assert {k[2] for k in cache.data} == {PRIMARY, SECOND}


def test_consensus_model_error_is_reported_but_page_kept(schema, mapping):
    vision = FakeVision({PRIMARY: sheet(6), SECOND: [ValueError("modelo no disponible")]})
    tracker = FakeTracker()
    report = make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, tracker, consensus=[SECOND]).run()
    assert len(tracker.rows) == 6 and report.consensus_errors == 1


def test_pipeline_reports_usage_per_call(schema, mapping):
    pipeline = PagePipeline(FakeVision({PRIMARY: sheet(6)}), FakeRenderer(), layout())
    result = pipeline.read(b"page1", PRIMARY)
    assert [r.row_number for r in result.extraction.rows] == [1, 2, 3, 4, 5, 6]
    assert len(pipeline.usage) == 4
    assert result.missing == []


def test_pipeline_rejects_page_without_any_row(schema, mapping):
    with pytest.raises(ValueError):
        PagePipeline(FakeVision({PRIMARY: ScriptedPage(HEADER, [])}), FakeRenderer(), layout()).read(b"p", PRIMARY)


def test_row_15_skipped_mid_page_is_recovered_by_requery(schema, mapping):
    """Real case: on a 25-row sheet the model skipped No. 15 (first row dated 10-08-26),
    in the middle of a strip, not on a cut. The targeted re-query must recover it."""
    rows = [numbered(n, fecha_plan=cell("06-08-26" if n < 15 else "10-08-26")) for n in range(1, 26)]
    rows[14] = numbered(
        15, doc="1000000015", fecha_plan=cell("10-08-26"), nombre=cell("Ana Maria Prueba"),
        tipo_doc=cell("CC"), ficha=cell("0202000015"), integrante=cell("1"),
    )
    page = ScriptedPage(HEADER, rows, skip={15})
    tracker = FakeTracker()
    report = make(schema, mapping, FakeDrive({"F1": b"pdf"}), FakeVision({PRIMARY: page}), tracker, expected_rows=25).run()
    assert [p for p in page.prompts if "Return only row" in p] and "No. 15" in page.prompts[-1]
    skipped = next(r for r in tracker.rows if r.source_row == 15)
    assert skipped.status is Status.EXTRACTED and not skipped.error
    assert skipped.answers["nombre_del_usuario"] == "ANA MARIA PRUEBA"
    assert skipped.answers["numero_de_documento_de_identidad"] == "1000000015"
    assert skipped.answers["codigo_de_ficha"] == "0202000015"
    assert skipped.answers["fecha_de_visita"] == "2026-08-10"
    assert len(tracker.rows) == 25 and report.rows_requeried == 1 and report.rows_unread == 0


def test_pipeline_cache_is_reused_only_for_the_same_image(schema, mapping):
    cache = MemoryCache()
    vision = FakeVision({PRIMARY: sheet(6)})
    PagePipeline(vision, FakeRenderer(), layout(), cache).read(b"page1", PRIMARY, key=("F1", 1))
    paid = len(vision.calls)
    PagePipeline(vision, FakeRenderer(), layout(), cache).read(b"page1", PRIMARY, key=("F1", 1))
    assert len(vision.calls) == paid
    # another render (resolution, crop geometry, rotation...) of the same page is read again
    PagePipeline(vision, FakeRenderer(), layout(), cache).read(b"page1-hires", PRIMARY, key=("F1", 1))
    assert len(vision.calls) == 2 * paid


def test_strips_are_cropped_to_the_located_table_with_the_titles_on_top(schema, mapping):
    box = {"titles_top": 200, "titles_bottom": 250, "left": 100, "right": 900, "last_row_bottom": 850}

    class Located(ScriptedPage):
        def __call__(self, image, prompt):
            out = super().__call__(image, prompt)
            return dict(out, table=box) if prompt == HEADER_PROMPT else out

    from plan_cuidado.domain.ocr import HEADER_PROMPT

    vision = FakeVision({PRIMARY: Located(HEADER, [numbered(n) for n in range(1, 7)])})
    PagePipeline(vision, FakeRenderer(), layout()).read(b"page1", PRIMARY)
    strips = [img.decode() for img, _ in vision.calls[1:]]
    assert len(strips) == 3
    assert all("|cols0.09" in s and "|titles0.19" in s for s in strips)
    assert strips[0].split("|crop")[1].startswith("0.24")  # first strip starts right under the titles


def test_without_a_located_table_the_configured_band_is_used(schema, mapping):
    vision = FakeVision({PRIMARY: sheet(6)})
    PagePipeline(vision, FakeRenderer(), layout(table_top=0.15, table_bottom=0.95)).read(b"page1", PRIMARY)
    strips = [img.decode() for img, _ in vision.calls[1:]]
    assert strips[0].split("|crop")[1].startswith("0.150") and "|titles" not in strips[0]


def test_consensus_can_read_only_the_critical_columns_reusing_the_primary_header(schema, mapping):
    other_rows = [numbered(n) for n in range(1, 7)]
    other_rows[1] = dict(other_rows[1], telefono=cell("3101234560"))
    vision = FakeVision({PRIMARY: sheet(6), SECOND: ScriptedPage(HEADER, other_rows)})
    tracker = FakeTracker()
    make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, tracker, consensus=[SECOND],
         consensus_critical_only=True).run()
    second = [s for m, s in vision.schemas if m == SECOND]
    assert len(second) == 3  # no header call of its own: same rotation and table crop as the primary
    columns = set(second[0]["properties"]["rows"]["items"]["properties"]) - {"row_number", "empty"}
    assert columns == set(mapping.critical_columns())
    row2 = next(r for r in tracker.rows if r.source_row == 2)
    assert set(row2.ocr_disagreements) == {"telefono"}
    assert all(not r.ocr_disagreements for r in tracker.rows if r.source_row != 2)


@pytest.mark.parametrize("enabled", [True, False])
def test_member_with_a_ficha_no_earlier_row_has_is_marked_purple(schema, mapping, enabled):
    rows = [numbered(n) for n in range(1, 7)]
    rows[1] = dict(rows[1], integrante=cell("2"), ficha=cell("0202099999"))  # rows share 0202012345 otherwise
    vision = FakeVision({PRIMARY: ScriptedPage(HEADER, rows)})
    tracker = FakeTracker()
    make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, tracker, household_check=enabled).run()
    marked = {r.source_row: set(r.ocr_disagreements) for r in tracker.rows if r.ocr_disagreements}
    assert marked == ({2: {"codigo_de_ficha"}} if enabled else {})


def test_strips_of_a_page_are_read_concurrently(schema, mapping):
    import threading

    page = sheet(6)
    barrier = threading.Barrier(3, timeout=5)

    def handler(image, prompt):
        if "strip" in prompt:
            barrier.wait()  # breaks (BrokenBarrierError) unless the three strips are in flight together
        return page(image, prompt)

    pipeline = PagePipeline(FakeVision({PRIMARY: handler}), FakeRenderer(), layout())
    result = pipeline.read(b"page1", PRIMARY)
    assert [r.row_number for r in result.extraction.rows] == [1, 2, 3, 4, 5, 6]


def test_consensus_models_read_the_page_concurrently(schema, mapping):
    import threading

    THIRD = "third/model"
    barrier = threading.Barrier(2, timeout=5)

    def reader():
        page = sheet(6)

        def handler(image, prompt):
            if "strip 1 of" in prompt:
                barrier.wait()  # both consensus models must be reading at the same time
            return page(image, prompt)

        return handler

    vision = FakeVision({PRIMARY: sheet(6), SECOND: reader(), THIRD: reader()})
    tracker = FakeTracker()
    report = make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, tracker, consensus=[SECOND, THIRD]).run()
    assert report.consensus_errors == 0 and len(tracker.rows) == 6


# --------------------------------------------------------------------------- pages in parallel


def page_sheet(page, count=6, file=0, **kwargs):
    """A sheet whose documents are unique to this page (rows of different pages are not duplicates)."""
    rows = [numbered(n, doc=f"1{file}{page:02d}0000{n:02d}") for n in range(1, count + 1)]
    return ScriptedPage(HEADER, rows, **kwargs)


class Recorder:
    """Drive + tracker writes in one ordered log, to check ordering and 'move after all pages'."""

    def __init__(self, drive, tracker):
        self.events: list[tuple] = []
        for name in ("move_to_processed", "move_to_failed"):
            original = getattr(drive, name)
            setattr(drive, name, self._wrap(name, original))
        original_append = tracker.append_rows
        tracker.append_rows = lambda rows: (self.events.append(("append", [r.row_id for r in rows])), original_append(rows))

    def _wrap(self, name, original):
        def call(file_id, *args):
            self.events.append((name, file_id))
            return original(file_id, *args)

        return call


class PerPage:
    """Answers each page image with its own scripted reader. Pages are b"page<N>..." (keys N) or,
    with FileRenderer, b"page<N>@<pdf>..." (keys (pdf, N))."""

    def __init__(self, pages: dict, before=None):
        self.pages, self.before = pages, before

    def __call__(self, image, prompt):
        m = re.match(rb"page(\d+)(?:@(\w+))?", image)
        number = int(m.group(1))
        key = (m.group(2).decode(), number) if m.group(2) else number
        if self.before:
            self.before(number, prompt)
        return self.pages[key](image, prompt)


class FileRenderer(FakeRenderer):
    """Page images that also name their PDF, so two files never look alike."""

    def render_pages(self, pdf):
        return [f"page{i}@{pdf.decode()}".encode() for i in range(1, self.pages + 1)]


def test_pages_of_a_file_are_read_concurrently(schema, mapping):
    barrier = threading.Barrier(3, timeout=5)

    def before(number, prompt):
        if prompt == HEADER_PROMPT:
            barrier.wait()  # breaks unless the three pages are in flight together

    vision = FakeVision({PRIMARY: PerPage({n: page_sheet(n, 6) for n in (1, 2, 3)}, before)})
    tracker = FakeTracker()
    report = make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, tracker, pages=3, page_workers=3).run()
    assert report.files_processed == 1 and len(tracker.rows) == 18


def test_pages_of_different_files_are_read_concurrently(schema, mapping):
    barrier = threading.Barrier(2, timeout=5)

    def before(number, prompt):
        if prompt == HEADER_PROMPT:
            barrier.wait()  # one page per file: both files must be read at the same time

    vision = FakeVision({PRIMARY: PerPage({1: sheet(6)}, before)})
    drive = FakeDrive({"F1": b"pdf", "F2": b"pdf"})
    report = make(schema, mapping, drive, vision, pages=1, page_workers=2).run()
    assert report.files_processed == 2


def test_page_worker_count_is_respected(schema, mapping):
    lock = threading.Lock()
    active, peak = set(), [0]

    def before(number, prompt):
        if prompt == HEADER_PROMPT:
            with lock:
                active.add(number)
                peak[0] = max(peak[0], len(active))
            time.sleep(0.05)
            with lock:
                active.discard(number)

    vision = FakeVision({PRIMARY: PerPage({n: page_sheet(n, 6) for n in range(1, 7)}, before)})
    make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, pages=6, page_workers=2).run()
    assert peak[0] == 2


def test_concurrent_requests_are_capped(schema, mapping):
    lock = threading.Lock()
    in_flight, peak = [0], [0]

    def before(number, prompt):
        with lock:
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
        time.sleep(0.02)
        with lock:
            in_flight[0] -= 1

    vision = FakeVision({PRIMARY: PerPage({n: page_sheet(n, 6) for n in range(1, 5)}, before)})
    make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, pages=4, page_workers=4, max_concurrent_requests=2).run()
    assert peak[0] == 2  # 4 pages x 3 strips would put 12 requests in flight without the cap


def test_rows_and_writes_keep_file_page_row_order_whatever_finishes_first(schema, mapping):
    # later pages (and the second file) answer first
    def before(number, prompt):
        time.sleep(0.01 * (4 - number))

    pages = {(f, n): page_sheet(n, 3, file=i) for i, f in enumerate(("F1", "F2")) for n in (1, 2, 3)}
    vision = FakeVision({PRIMARY: PerPage(pages, before)})
    drive, tracker = FakeDrive({"F1": b"F1", "F2": b"F2"}), FakeTracker()
    recorder = Recorder(drive, tracker)
    service = make(schema, mapping, drive, vision, tracker, pages=3, page_workers=4, expected_rows=3)
    service.renderer = service.pipeline.renderer = FileRenderer(3)
    service.run()
    expected = [f"{f}-p{p}-r{r}" for f in ("F1", "F2") for p in (1, 2, 3) for r in (1, 2, 3)]
    assert [r.row_id for r in tracker.rows] == expected
    assert [e[0] for e in recorder.events] == ["append", "move_to_processed", "append", "move_to_processed"]
    assert recorder.events[1] == ("move_to_processed", "F1") and recorder.events[3] == ("move_to_processed", "F2")


def test_file_is_moved_only_after_all_its_pages_are_done(schema, mapping):
    finished = []

    def before(number, prompt):
        if number == 3:
            time.sleep(0.1)  # the slowest page
        if "strip 3 of" in prompt:
            finished.append(number)

    drive, tracker = FakeDrive({"F1": b"pdf"}), FakeTracker()
    vision = FakeVision({PRIMARY: PerPage({n: page_sheet(n, 6) for n in (1, 2, 3)}, before)})
    original = drive.move_to_processed
    seen = []
    drive.move_to_processed = lambda fid, month: (seen.append(sorted(finished)), original(fid, month))
    make(schema, mapping, drive, vision, tracker, pages=3, page_workers=3).run()
    assert seen == [[1, 2, 3]]
    assert len(tracker.rows) == 18


def test_a_failing_page_fails_its_file_without_touching_the_others(schema, mapping):
    # F1 has two pages (the second one unreadable), F2 one good page
    pages = {1: sheet(6), 2: ScriptedPage(HEADER, [], fail=ValueError("respuesta inválida"))}
    drive, tracker = FakeDrive({"F1": b"two", "F2": b"one"}), FakeTracker()
    service = make(schema, mapping, drive, FakeVision({PRIMARY: PerPage(pages)}), tracker, page_workers=4)

    class Renderer(FakeRenderer):
        def render_pages(self, pdf):
            return [f"page{i}".encode() for i in range(1, {b"two": 3, b"one": 2}[pdf])]

    service.renderer = service.pipeline.renderer = Renderer()
    report = service.run()
    assert report.files_failed == 1 and report.files_processed == 1
    assert [f for f, _ in drive.failed] == ["F1"] and "página 2" in drive.failed[0][1]
    assert {r.source_file_id for r in tracker.rows} == {"F2"} and len(tracker.rows) == 6


def test_transient_error_on_one_page_keeps_the_file_in_the_inbox(schema, mapping):
    pages = {1: sheet(6), 2: ScriptedPage(HEADER, [], fail=TransientError("timeout"))}
    vision = FakeVision({PRIMARY: PerPage(pages)})
    drive, tracker = FakeDrive({"F1": b"pdf"}), FakeTracker()
    report = make(schema, mapping, drive, vision, tracker, pages=2, page_workers=2).run()
    assert "F1" in drive.inbox and drive.failed == [] and tracker.rows == []
    assert report.files_transient == ["F1.pdf"]


def test_report_counters_and_usage_add_up_across_parallel_pages(schema, mapping):
    pages = {n: page_sheet(n, 6, skip={3}, unreadable={3} if n == 2 else ()) for n in (1, 2, 3)}
    vision = FakeVision({PRIMARY: PerPage(pages)})
    tracker = FakeTracker()
    report = make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, tracker, pages=3, page_workers=3).run()
    assert report.rows_requeried == 2 and report.rows_unread == 1
    assert len(report.usage) == len(vision.calls) == 3 * 5  # header + 3 strips + 1 re-query per page
    assert len(tracker.rows) == 18


def test_progress_is_logged_per_page(schema, mapping):
    messages = []
    vision = FakeVision({PRIMARY: PerPage({n: page_sheet(n, 6) for n in (1, 2)})})
    service = make(schema, mapping, FakeDrive({"F1": b"pdf"}), vision, pages=2, page_workers=2)
    service.log = messages.append
    service.run()
    assert any("página 1/2 lista" in m for m in messages) and any("página 2/2 lista" in m for m in messages)
