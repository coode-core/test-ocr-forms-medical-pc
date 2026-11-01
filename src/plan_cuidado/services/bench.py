"""Use case: compare OCR models (and a consensus pair) against one hand-transcribed page.

Each model runs the same path as `pc ocr`: the strip pipeline (header, strips, re-query),
normalization into tracker rows, unread-row placeholders and, for a pair, the consensus marks.
Responses are cached per PDF content, so re-running a benchmark does not pay again.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from plan_cuidado.domain.bench import (
    ConsensusStats,
    PageScore,
    RowsScore,
    consensus_stats,
    score_page,
    score_rows,
    summarize_usage,
)
from plan_cuidado.domain.consensus import apply_consensus
from plan_cuidado.domain.household import apply_household_check
from plan_cuidado.domain.mapping import FieldMapping
from plan_cuidado.domain.ocr import PageExtraction, SourceRef, header_answers, rows_from_extraction, unread_row_placeholder
from plan_cuidado.domain.schema import FormSchema
from plan_cuidado.domain.tracker import TrackerRow
from plan_cuidado.ports import OcrCache, PageRenderer, VisionClient
from plan_cuidado.services.ocr import PagePipeline, consensus_read_options


@dataclass
class BenchResult:
    model: str
    score: PageScore | None = None  # raw transcription vs truth
    rows_score: RowsScore | None = None  # what reaches the tracker (needs the form schema)
    usage: dict = field(default_factory=dict)
    error: str | None = None
    extraction: PageExtraction | None = None
    rows: list[TrackerRow] = field(default_factory=list)


@dataclass
class BenchReport:
    results: list[BenchResult] = field(default_factory=list)
    consensus: ConsensusStats | None = None
    consensus_models: tuple[str, ...] | None = None
    # rows of the first model with the consensus votes and purple marks, as `pc ocr` writes them
    consensus_score: RowsScore | None = None


def _tracker_rows(
    read, schema: FormSchema, mapping: FieldMapping, source: SourceRef, pivot_year, household: bool = True
) -> list[TrackerRow]:
    """Same conversion as OcrService: normalized rows (household ficha check included) plus a
    placeholder per unread row."""
    rows = rows_from_extraction(read.extraction, schema, mapping, source, pivot_year=pivot_year)
    if household:
        rows = apply_household_check(read.extraction, rows, mapping)
    if read.missing:
        header = header_answers(read.extraction, schema, mapping, pivot_year)
        rows += [unread_row_placeholder(source, n, header) for n in read.missing]
    return sorted(rows, key=lambda r: r.source_row or 0)


def run_bench(
    truth: dict,
    pdf: bytes,
    models: list[str],
    vision: VisionClient,
    renderer: PageRenderer,
    mapping: FieldMapping,
    schema: FormSchema | None = None,
    consensus: tuple[str, ...] | None = None,
    cache: OcrCache | None = None,
    pivot_year: int | None = None,
) -> BenchReport:
    pages = renderer.render_pages(pdf)
    page = int(truth.get("page", 1))
    if not 1 <= page <= len(pages):
        raise ValueError(f"La página {page} no existe (el PDF tiene {len(pages)})")
    image = pages[page - 1]
    file_key = "bench-" + hashlib.sha256(pdf).hexdigest()[:16]
    source = SourceRef(file_id=file_key, file_name="bench.pdf", link="", page=page)
    critical = mapping.critical_columns()
    wanted = list(dict.fromkeys([*models, *(consensus or ())]))
    if consensus:  # consensus models read after the first one, as in `pc ocr`
        consensus = tuple(dict.fromkeys(consensus))
        followers = [m for m in consensus[1:] if m in wanted]
        wanted = [m for m in wanted if m not in followers]
        at = wanted.index(consensus[0]) + 1
        wanted[at:at] = followers
    report = BenchReport(consensus_models=consensus)
    by_model: dict[str, BenchResult] = {}
    reads = {}
    for model in wanted:
        pipeline = PagePipeline(vision, renderer, mapping.ocr, cache)
        options = {}
        if consensus and model in consensus[1:] and consensus[0] in reads:
            options = consensus_read_options(mapping, reads[consensus[0]])
        try:
            read = pipeline.read(image, model, key=(file_key, page), **options)
            reads[model] = read
        except Exception as exc:
            result = BenchResult(model, error=f"{type(exc).__name__}: {exc}", usage=summarize_usage(pipeline.usage))
        else:
            result = BenchResult(
                model, score_page(truth, read.extraction, critical), usage=summarize_usage(pipeline.usage),
                extraction=read.extraction,
            )
            if schema is not None:
                result.rows = _tracker_rows(read, schema, mapping, source, pivot_year)
                result.rows_score = score_rows(truth, result.rows, schema, mapping, pivot_year)
        by_model[model] = result
        report.results.append(result)
    if consensus and len(consensus) > 1:
        first, *others = (by_model[m] for m in consensus)
        if first.extraction is not None and all(o.extraction is not None for o in others):
            if len(others) == 1:
                report.consensus = consensus_stats(truth, first.extraction, others[0].extraction, critical)
            if schema is not None:
                voted = apply_consensus(
                    _tracker_rows(reads[first.model], schema, mapping, source, pivot_year, household=False),
                    first.model, {o.model: o.rows for o in others}, mapping.ocr.critical_fields,
                )
                voted = apply_household_check(first.extraction, voted, mapping)
                report.consensus_score = score_rows(truth, voted, schema, mapping, pivot_year)
    return report
