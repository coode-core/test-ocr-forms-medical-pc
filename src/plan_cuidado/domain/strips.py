"""Split the patient table into overlapping horizontal strips and merge the rows back.

A strip has fewer rows than the whole page, so the model sees each row at a higher
effective resolution and is less likely to skip one. Rows are merged by their printed
row number ("No."), never by position.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from rapidfuzz import fuzz

from plan_cuidado.domain.mapping import OcrConfig
from plan_cuidado.domain.ocr import ExtractedRow, TableBox

Band = tuple[float, float]  # (top, bottom) as fractions of the page height


@dataclass(frozen=True)
class TableGeometry:
    """Rows area [top, bottom] x [left, right] and the column-titles band, as page fractions."""

    top: float
    bottom: float
    left: float = 0.0
    right: float = 1.0
    titles: Band | None = None


_EDGE_PAD = 0.01  # page fraction added left/right of the located table
_TITLES_PAD = 0.005
_MAX_TITLES = 120  # a titles row taller than 12 % of the page is not a titles row


def _plausible(box: TableBox) -> bool:
    values = (box.titles_top, box.titles_bottom, box.left, box.right, box.last_row_bottom)
    return (
        all(0 <= v <= 1000 for v in values)
        and box.titles_top < box.titles_bottom <= box.titles_top + _MAX_TITLES
        and box.right - box.left >= 500
        and box.last_row_bottom - box.titles_bottom >= 200
    )


def table_geometry(box: TableBox | None, layout: OcrConfig) -> TableGeometry:
    """Crop area for the strips: the table the header call located (padded, with half a row
    below the last border because models place it a little high), or the configured band."""
    if not layout.locate_table or box is None or not _plausible(box):
        return TableGeometry(top=layout.table_top, bottom=layout.table_bottom)
    titles_top, titles_bottom, last = box.titles_top / 1000, box.titles_bottom / 1000, box.last_row_bottom / 1000
    row = (last - titles_bottom) / layout.expected_rows if layout.expected_rows else 0.02
    return TableGeometry(
        top=max(0.0, titles_bottom - _TITLES_PAD),
        bottom=min(1.0, last + 0.75 * row),
        left=max(0.0, box.left / 1000 - _EDGE_PAD),
        right=min(1.0, box.right / 1000 + _EDGE_PAD),
        titles=(max(0.0, titles_top - _TITLES_PAD), titles_bottom + _TITLES_PAD),
    )


def strip_bounds(count: int, overlap: float, top: float = 0.0, bottom: float = 1.0) -> list[Band]:
    """`count` bands covering [top, bottom]; neighbours share `overlap` x strip height."""
    if count < 1:
        raise ValueError("strips must be >= 1")
    if not 0.0 <= top < bottom <= 1.0:
        raise ValueError("table_top/table_bottom must satisfy 0 <= top < bottom <= 1")
    if not 0.0 <= overlap < 1.0:
        raise ValueError("strip_overlap must be in [0, 1)")
    base = (bottom - top) / count
    return [
        (max(top, top + i * base - overlap * base), min(bottom, top + (i + 1) * base + overlap * base))
        for i in range(count)
    ]


@dataclass
class MergedRows:
    rows: list[ExtractedRow] = field(default_factory=list)
    # row number -> indexes of the strips where the model reported it
    seen_in: dict[int, list[int]] = field(default_factory=dict)


def _on_edge(position: int, length: int, strip: int, strips: int) -> bool:
    """First row of a lower strip / last row of an upper strip may be cut by the crop."""
    return (position == 0 and strip > 0) or (position == length - 1 and strip < strips - 1)


_IDENTITY_COLUMNS = ("nombre", "numero_documento")
_SAME_PERSON = 80  # rapidfuzz ratio between two readings of the same row
_MAX_SHIFT = 3


def _identity(row: ExtractedRow, columns: tuple[str, ...] = _IDENTITY_COLUMNS) -> str:
    texts = [c.text for name in columns if (c := row.cell(name)) and c.text]
    if not texts:  # unknown columns: compare everything that was read
        texts = [c.text for _, c in sorted(row.cells.items()) if c.text]
    return " ".join(texts).lower()


def _shift(rows: list[ExtractedRow], anchor: dict[int, str], identity=_identity) -> int:
    """Offset to add to this strip's row numbers so its rows line up with the rows
    already merged. Models sometimes count rows instead of reading the printed "No.",
    which shifts a lower strip by one or two; the overlap shows by how much."""
    def similarity(k: int) -> tuple[float, int]:
        ratios = [
            fuzz.ratio(key, other) for r in rows
            if not r.empty and (key := identity(r)) and (other := anchor.get(r.row_number + k))
        ]
        return (sum(ratios) / len(ratios) if ratios else 0.0), len(ratios)

    scores = {k: similarity(k) for k in range(-_MAX_SHIFT, _MAX_SHIFT + 1)}
    # a shift must be backed by as many overlapping rows as "no shift" (at least 2 if possible),
    # so a single coincidence on a strip edge cannot move the whole strip
    needed = max(1, min(2, scores[0][1]))
    candidates = [k for k, (mean, pairs) in scores.items() if pairs >= needed and mean >= _SAME_PERSON]
    if not candidates:
        return 0
    best = max(candidates, key=lambda k: (scores[k][0], -abs(k)))
    return best if scores[best][0] > scores[0][0] else 0


def merge_strips(
    strips: list[list[ExtractedRow]], max_row: int, reference: list[ExtractedRow] | None = None
) -> MergedRows:
    """Dedupe rows across strips by row number, preferring a reading not on a strip edge,
    then a non-empty reading, then the more confident one, then the one with more cells read
    (ties: the earlier strip).
    A strip whose numbering is shifted against the rows already merged is realigned first.
    `reference`: the whole page as another model read it (a consensus reader that returns only some
    columns): every strip is realigned against it, on the identity columns both readings have."""
    best: dict[int, tuple[tuple, ExtractedRow]] = {}
    seen: dict[int, list[int]] = {}
    identity = _identity
    fixed: dict[int, str] = {}
    if reference:
        read = {name for rows in strips for r in rows for name, c in r.cells.items() if c.text}
        columns = tuple(c for c in _IDENTITY_COLUMNS if c in read) or _IDENTITY_COLUMNS
        identity = lambda row: _identity(row, columns)  # noqa: E731
        fixed = {r.row_number: key for r in reference if r.row_number is not None and not r.empty and (key := identity(r))}
    for s, rows in enumerate(strips):
        rows = [r for r in rows if r.row_number is not None]
        anchor = fixed or {n: key for n, (_, r) in best.items() if not r.empty and (key := identity(r))}
        if k := _shift(rows, anchor, identity):
            rows = [r.model_copy(update={"row_number": r.row_number + k}) for r in rows]
        valid = [r for r in rows if 1 <= r.row_number <= max_row]
        for position, row in enumerate(valid):
            number = row.row_number
            seen.setdefault(number, [])
            if s not in seen[number]:
                seen[number].append(s)
            score = (not _on_edge(position, len(valid), s, len(strips)), not row.empty, row.mean_confidence, row.filled)
            if number not in best or score > best[number][0]:
                best[number] = (score, row)
    return MergedRows(rows=[best[n][1] for n in sorted(best)], seen_in=seen)


def missing_row_numbers(rows: list[ExtractedRow], expected: int) -> list[int]:
    """Printed row numbers in 1..N that no strip reported (blank rows reported as empty count as read).
    N is the expected rows per sheet, or the highest row number seen if larger."""
    read = {r.row_number for r in rows if r.row_number is not None}
    last = max([expected, *read]) if read or expected else 0
    return [n for n in range(1, last + 1) if n not in read]


def requery_band(row_number: int, seen_in: dict[int, list[int]], bounds: list[Band]) -> Band:
    """Crop for re-reading one missing row: the strips where its nearest neighbours were seen."""
    below = [n for n in seen_in if n < row_number]
    above = [n for n in seen_in if n > row_number]
    strips: list[int] = []
    if below:
        strips += seen_in[max(below)]
    if above:
        strips += seen_in[min(above)]
    if not strips:
        return (bounds[0][0], bounds[-1][1])
    return (bounds[min(strips)][0], bounds[max(strips)][1])
