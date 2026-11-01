"""Use case: OCR every PDF in the Drive inbox into tracker rows.

Per page: one low-resolution call for the header (and rotation), then the patient table is
read in overlapping horizontal strips, merged by printed row number; missing row numbers are
re-read one by one and, if still unread, become NEEDS_REVIEW placeholder rows. Optional
consensus models re-read the page and critical cells where they disagree are flagged.

Pages are read in parallel (all pages of a file and the next files), bounded by
`ocr.page_workers` and a global cap of OpenRouter requests in flight; the tracker and Drive are
still written one file at a time, in inbox order, after all pages of the file are done.
"""

from __future__ import annotations

import hashlib
import json
import threading
from collections import deque
from collections.abc import Callable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import date

from plan_cuidado.domain.consensus import apply_consensus
from plan_cuidado.domain.household import apply_household_check
from plan_cuidado.domain.mapping import FieldMapping, OcrConfig
from plan_cuidado.domain.ocr import (
    HEADER_PROMPT,
    ExtractedRow,
    PageExtraction,
    SourceRef,
    flagged_ratio,
    header_answers,
    header_json_schema,
    parse_extraction,
    row_prompt,
    rows_from_extraction,
    rows_json_schema,
    strip_prompt,
    unread_row_placeholder,
)
from plan_cuidado.domain.schema import FormSchema
from plan_cuidado.domain.strips import merge_strips, missing_row_numbers, requery_band, strip_bounds, table_geometry
from plan_cuidado.domain.tracker import Status, TrackerRow, dedupe_new_rows
from plan_cuidado.ports import (
    DriveFile,
    DriveStore,
    OcrCache,
    PageRenderer,
    TrackerStore,
    TransientError,
    VisionClient,
)

MAX_CONSECUTIVE_TRANSIENT = 2  # two files in a row without network: stop instead of waiting on retries


@dataclass
class OcrSettings:
    model: str
    fallback_model: str | None = None
    pivot_year: int | None = None
    consensus_models: list[str] = field(default_factory=list)
    page_workers: int | None = None  # None = ocr.page_workers of field_mapping.yaml


@dataclass
class PageStats:
    """Counters of one page, filled by its worker thread and added to the report in order."""

    pages_cascaded: int = 0
    consensus_errors: int = 0
    disagreements: int = 0
    rows_requeried: int = 0
    rows_unread: int = 0

    def add_to(self, report: OcrReport) -> None:
        for name in ("pages_cascaded", "consensus_errors", "disagreements", "rows_requeried", "rows_unread"):
            setattr(report, name, getattr(report, name) + getattr(self, name))


@dataclass
class OcrReport:
    files_processed: int = 0
    files_failed: int = 0
    files_skipped: int = 0
    files_transient: list[str] = field(default_factory=list)  # left in 01_inbox (no network)
    stopped_early: bool = False
    rows_added: int = 0
    rows_duplicated: int = 0
    rows_requeried: int = 0  # missed by the strips, recovered by the single-row call
    rows_unread: int = 0  # placeholders "fila no leída"
    disagreements: int = 0
    consensus_errors: int = 0
    pages_cascaded: int = 0
    usage: list[dict] = field(default_factory=list)


@dataclass
class PageRead:
    extraction: PageExtraction
    missing: list[int] = field(default_factory=list)  # still unread after the re-query
    requeried: list[int] = field(default_factory=list)  # recovered by the re-query


def consensus_read_options(mapping: FieldMapping, primary: PageRead) -> dict:
    """How a consensus model reads the page after the primary model (PagePipeline.read kwargs)."""
    if not mapping.ocr.consensus_critical_only:
        return {}
    return {"columns": mapping.critical_columns(), "header": primary.extraction}


class PagePipeline:
    """Reads one page image with one model: header, strips, merge and re-query (cached).

    Thread-safe: several pages and models may be read at once; at most
    `layout.max_concurrent_requests` vision requests are in flight across all of them."""

    def __init__(self, vision: VisionClient, renderer: PageRenderer, layout: OcrConfig, cache: OcrCache | None = None):
        self.vision = vision
        self.renderer = renderer
        self.layout = layout
        self.cache = cache
        self.usage: list[dict] = []
        self._usage_lock = threading.Lock()
        self._requests = threading.BoundedSemaphore(layout.max_concurrent_requests)

    def _call(self, image: bytes, model: str, prompt: str, schema: dict, key, variant: str) -> PageExtraction:
        # The exact request is part of the key: a new prompt, resolution or crop is read again.
        digest = hashlib.sha256(image + prompt.encode() + json.dumps(schema, sort_keys=True).encode()).hexdigest()
        variant = f"{variant}_{digest[:10]}"
        cached = self.cache.get(key[0], key[1], model, variant) if self.cache and key else None
        if cached is None:
            with self._requests:
                response = self.vision.extract(image, model, prompt, schema)
            cached = {"content": response.content, "model": response.model, "usage": response.usage}
            with self._usage_lock:
                self.usage.append({"model": model, **(response.usage or {})})
            if self.cache and key:
                self.cache.put(key[0], key[1], model, cached, variant)
        return parse_extraction(cached["content"])

    def _header(self, image: bytes, model: str, key, variant: str) -> PageExtraction:
        small = self.renderer.crop(image, 0.0, 1.0, self.layout.header_max_side)
        return self._call(small, model, HEADER_PROMPT, header_json_schema(), key, variant)

    def read(
        self, image: bytes, model: str, key: tuple[str, int] | None = None,
        columns: list[str] | None = None, header: PageExtraction | None = None,
    ) -> PageRead:
        """`columns`: only these OCR columns are asked for (all by default). `header`: a header
        already read (rotation and table box) by another model, so this model skips its own."""
        lay = self.layout
        tag = ""
        if header is not None:
            if header.rotation_degrees in (90, 180, 270):
                image = self.renderer.rotate(image, header.rotation_degrees)
                tag = f"rot{header.rotation_degrees}_"
        else:
            header = self._header(image, model, key, "header")
            if header.rotation_degrees in (90, 180, 270):
                degrees = header.rotation_degrees
                image = self.renderer.rotate(image, degrees)
                tag = f"rot{degrees}_"
                header = self._header(image, model, key, f"{tag}header")
        schema = rows_json_schema(columns)

        geo = table_geometry(header.table, lay)

        def crop_rows(top: float, bottom: float) -> bytes:
            return self.renderer.crop(image, top, bottom, lay.strip_max_side, geo.left, geo.right, geo.titles)

        bounds = strip_bounds(lay.strips, lay.strip_overlap, geo.top, geo.bottom)

        def read_strip(i: int, top: float, bottom: float) -> list[ExtractedRow]:
            variant = f"{tag}strip{i}of{len(bounds)}_{top:.3f}-{bottom:.3f}"
            return self._call(crop_rows(top, bottom), model, strip_prompt(i, len(bounds), columns), schema, key, variant).rows

        # The strips are independent requests: in flight together, a page takes as long as its slowest strip.
        with ThreadPoolExecutor(max_workers=len(bounds)) as pool:
            futures = [pool.submit(read_strip, i, top, bottom) for i, (top, bottom) in enumerate(bounds, start=1)]
            strips: list[list[ExtractedRow]] = [f.result() for f in futures]
        # a header from another model carries its rows: realign this model's strips on them
        merged = merge_strips(strips, lay.max_rows, reference=header.rows or None)
        if not merged.rows:
            raise ValueError("el modelo no encontró filas en la tabla")

        rows = {r.row_number: r for r in merged.rows}
        missing = missing_row_numbers(merged.rows, lay.expected_rows)
        requeried: list[int] = []
        if lay.requery_missing:
            for number in missing:
                top, bottom = requery_band(number, merged.seen_in, bounds)
                crop = crop_rows(top, bottom)
                variant = f"{tag}row{number}_{top:.3f}-{bottom:.3f}"
                found = [r for r in self._call(crop, model, row_prompt(number, columns), schema, key, variant).rows
                         if r.row_number == number]
                if found:
                    rows[number] = max(found, key=lambda r: (not r.empty, r.mean_confidence, r.filled))
                    requeried.append(number)
        still = [n for n in missing if n not in rows]
        extraction = PageExtraction(
            rotation_degrees=header.rotation_degrees, header=header.header, table=header.table,
            rows=[rows[n] for n in sorted(rows)],
        )
        return PageRead(extraction=extraction, missing=still, requeried=[n for n in requeried if not rows[n].empty])


@dataclass
class _FileJob:
    """One inbox file in flight: its page reads (or why it could not be opened)."""

    file: DriveFile
    skip: bool = False
    error: Exception | None = None
    pages: list[Future] = field(default_factory=list)
    stats: list[PageStats] = field(default_factory=list)

    def pending(self) -> list[Future]:
        return [f for f in self.pages if not f.done()]

    def cancel_rest_if_failed(self) -> None:
        # one page failed: the file will not be written, so pages not started yet are not paid for
        if any(f.done() and not f.cancelled() and f.exception() is not None for f in self.pages):
            for f in self.pages:
                f.cancel()


class OcrService:
    def __init__(
        self,
        drive: DriveStore,
        tracker: TrackerStore,
        vision: VisionClient,
        renderer: PageRenderer,
        cache: OcrCache,
        schema: FormSchema,
        mapping: FieldMapping,
        settings: OcrSettings,
        today: Callable[[], date] = date.today,
        log: Callable[[str], None] = lambda message: None,
    ):
        self.drive = drive
        self.tracker = tracker
        self.renderer = renderer
        self.schema = schema
        self.mapping = mapping
        self.settings = settings
        self.today = today
        self.log = log
        self._log_lock = threading.Lock()
        self.pipeline = PagePipeline(vision, renderer, mapping.ocr, cache)

    def _say(self, message: str) -> None:
        with self._log_lock:  # worker threads log too: one whole line at a time
            self.log(message)

    @property
    def page_workers(self) -> int:
        return max(1, self.settings.page_workers or self.mapping.ocr.page_workers)

    def _models(self) -> list[str]:
        models = [self.settings.model]
        if self.settings.fallback_model and self.settings.fallback_model != self.settings.model:
            models.append(self.settings.fallback_model)
        return models

    def _to_rows(self, read: PageRead, source: SourceRef) -> list[TrackerRow]:
        return rows_from_extraction(read.extraction, self.schema, self.mapping, source, pivot_year=self.settings.pivot_year)

    def _primary(self, image: bytes, source: SourceRef, stats: PageStats) -> tuple[str, PageRead, list[TrackerRow]]:
        best: tuple[tuple, str, PageRead, list[TrackerRow]] | None = None
        last_error: Exception | None = None
        for attempt, model in enumerate(self._models()):
            if attempt:
                stats.pages_cascaded += 1
                self._say(f"  {source.file_name} página {source.page}: reintentando con {model}")
            try:
                read = self.pipeline.read(image, model, key=(source.file_id, source.page))
                rows = self._to_rows(read, source)
            except TransientError:
                raise  # no network: another model would fail the same way
            except Exception as exc:  # invalid JSON, schema mismatch, no rows
                last_error = exc
                self._say(f"  {source.file_name} página {source.page}: error con {model}: {exc}")
                continue
            ratio = flagged_ratio(rows)
            score = (len(read.missing), ratio)
            if best is None or score < best[0]:
                best = (score, model, read, rows)
            if not read.missing and ratio <= self.mapping.cascade_flag_ratio:
                break
        if best is None:
            raise RuntimeError(f"página {source.page}: {last_error}")
        return best[1], best[2], best[3]

    def _consensus(
        self, image: bytes, source: SourceRef, primary_model: str, primary: PageRead, rows: list[TrackerRow],
        stats: PageStats,
    ):
        others: dict[str, list[TrackerRow]] = {}
        options = consensus_read_options(self.mapping, primary)
        models = [m for m in dict.fromkeys(self.settings.consensus_models) if m != primary_model]
        if not models:
            return rows
        # Independent readers: in flight together, the page waits only for the slowest one.
        with ThreadPoolExecutor(max_workers=len(models)) as pool:
            futures = {
                m: pool.submit(self.pipeline.read, image, m, key=(source.file_id, source.page), **options) for m in models
            }
            for model, future in futures.items():
                try:
                    others[model] = self._to_rows(future.result(), source)
                except TransientError:
                    raise
                except Exception as exc:
                    stats.consensus_errors += 1
                    self._say(
                        f"  {source.file_name} página {source.page}: el modelo de consenso {model} falló ({exc}); "
                        f"se usa solo {primary_model}"
                    )
        return apply_consensus(rows, primary_model, others, self.mapping.ocr.critical_fields)

    def _page_rows(self, file: DriveFile, page: int, total: int, image: bytes, stats: PageStats) -> list[TrackerRow]:
        """Runs in a worker thread: touches only its own `stats`, never the report or the stores."""
        source = SourceRef(file_id=file.id, file_name=file.name, link=file.link, page=page)
        model, read, rows = self._primary(image, source, stats)
        rows = self._consensus(image, source, model, read, rows, stats)
        rows = apply_household_check(read.extraction, rows, self.mapping)
        stats.disagreements += sum(len(r.ocr_disagreements) for r in rows)
        stats.rows_requeried += len(read.requeried)
        if read.missing:
            header = header_answers(read.extraction, self.schema, self.mapping, self.settings.pivot_year)
            rows += [unread_row_placeholder(source, n, header) for n in read.missing]
            stats.rows_unread += len(read.missing)
            self._say(f"  {file.name} página {page}: filas no leídas {read.missing} (quedan en NEEDS_REVIEW)")
        self._say(f"  {file.name}: página {page}/{total} lista")
        return sorted(rows, key=lambda r: r.source_row or 0)

    def _start(self, file: DriveFile, done: set[str], pool: ThreadPoolExecutor) -> _FileJob:
        """Downloads and renders one file (main thread: Drive and pdfium are not thread-safe) and
        queues its pages on the pool."""
        if file.id in done:
            return _FileJob(file, skip=True)
        try:
            images = self.renderer.render_pages(self.drive.download(file.id))
        except Exception as exc:
            return _FileJob(file, error=exc)
        self._say(f"Procesando {file.name} ({len(images)} página{'s' if len(images) != 1 else ''})")
        job = _FileJob(file, stats=[PageStats() for _ in images])
        job.pages = [
            pool.submit(self._page_rows, file, page, len(images), image, job.stats[page - 1])
            for page, image in enumerate(images, start=1)
        ]
        return job

    @staticmethod
    def _outcome(job: _FileJob) -> list[TrackerRow]:
        """Rows of a finished file in page order, or the error that keeps it from being written.
        A network error wins over any other: the file stays in 01_inbox and is retried (cached)."""
        if job.error is not None:
            raise job.error
        errors = [f.exception() for f in job.pages if not f.cancelled() and f.exception() is not None]
        transient = [e for e in errors if isinstance(e, TransientError)]
        if transient or errors:
            raise (transient or errors)[0]
        rows = [row for f in job.pages for row in f.result()]
        if not rows:
            raise RuntimeError("No se extrajeron filas de pacientes del PDF")
        return rows

    def run(self) -> OcrReport:
        report = OcrReport()
        existing = self.tracker.read_rows()
        # A file is "done" while it has unsubmitted rows. Files left with only SUBMITTED rows
        # (after `pc reset`) are read again; their submitted rows are kept by row_id dedupe.
        done = {r.source_file_id for r in existing if r.source_file_id and r.status is not Status.SUBMITTED}
        month = self.today().strftime("%Y-%m")
        consecutive_transient = 0
        workers = self.page_workers
        inbox = iter(self.drive.list_inbox())
        queue: deque[_FileJob] = deque()
        exhausted = False

        def fill() -> None:
            # keep about two pages per worker queued so no worker idles while a file is written
            nonlocal exhausted
            while not exhausted and sum(len(j.pending()) for j in queue) < 2 * workers:
                file = next(inbox, None)
                if file is None:
                    exhausted = True
                else:
                    queue.append(self._start(file, done, pool))

        pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ocr-page")
        interrupted = True
        try:
            while True:
                fill()
                if not queue:
                    break
                job = queue[0]
                while job.pending():
                    for j in queue:
                        j.cancel_rest_if_failed()
                    in_flight = [f for j in queue for f in j.pending()]
                    if in_flight:
                        wait(in_flight, return_when=FIRST_COMPLETED)
                    fill()
                queue.popleft()
                for stats, future in zip(job.stats, job.pages):
                    if future.done() and not future.cancelled():
                        stats.add_to(report)
                if job.skip:
                    # Already in the tracker (e.g. a previous run stopped before moving it).
                    self.drive.move_to_processed(job.file.id, month)
                    report.files_skipped += 1
                    continue
                try:
                    rows = self._outcome(job)
                except TransientError as exc:
                    report.files_transient.append(job.file.name)
                    consecutive_transient += 1
                    self._say(f"  {job.file.name}: sin conexión ({exc}); el archivo queda en 01_inbox")
                    if consecutive_transient >= MAX_CONSECUTIVE_TRANSIENT:
                        report.stopped_early = True
                        self._say("  Se detiene el proceso: parece que no hay conexión a internet.")
                        break
                    continue
                except Exception as exc:
                    consecutive_transient = 0
                    self.drive.move_to_failed(job.file.id, f"{job.file.name}: {exc}")
                    report.files_failed += 1
                    self._say(f"  {job.file.name} falló: {exc}")
                    continue
                consecutive_transient = 0
                kept, skipped = dedupe_new_rows(existing, rows, self.mapping.dedupe_keys)
                if kept:
                    self.tracker.append_rows(kept)
                existing.extend(kept)
                self.drive.move_to_processed(job.file.id, month)
                report.files_processed += 1
                report.rows_added += len(kept)
                report.rows_duplicated += len(skipped)
                self._say(f"  {job.file.name}: {len(kept)} fila(s) nuevas en el tracker")
            interrupted = False
        finally:
            # stopped early or interrupted: files still queued stay in 01_inbox, unread pages are not paid
            for j in queue:
                for f in j.pages:
                    f.cancel()
            # Ctrl-C: do not block here on requests already in flight (their answers still reach the cache)
            pool.shutdown(wait=not interrupted, cancel_futures=True)
        report.usage = list(self.pipeline.usage)
        return report
