import pytest

from plan_cuidado.domain.ocr import ExtractedRow, parse_extraction
from plan_cuidado.domain.strips import merge_strips, missing_row_numbers, requery_band, strip_bounds


def row(n, value="x", conf=0.9, empty=False):
    return ExtractedRow(row_number=n, empty=empty, cells={"nombre": {"value": None if empty else value, "confidence": conf}})


def test_strip_bounds_cover_table_with_overlap():
    bounds = strip_bounds(3, 0.15, 0.2, 0.8)
    assert len(bounds) == 3
    assert bounds[0][0] == pytest.approx(0.2)
    assert bounds[-1][1] == pytest.approx(0.8)
    for (_, b1), (t2, _) in zip(bounds, bounds[1:]):
        assert t2 < b1  # consecutive strips overlap
    base = 0.6 / 3
    assert bounds[1] == pytest.approx((0.2 + base - 0.15 * base, 0.2 + 2 * base + 0.15 * base))


def test_single_strip_is_the_whole_table():
    assert strip_bounds(1, 0.15, 0.0, 1.0) == [(0.0, 1.0)]


def test_strip_bounds_rejects_bad_config():
    with pytest.raises(ValueError):
        strip_bounds(0, 0.1, 0, 1)
    with pytest.raises(ValueError):
        strip_bounds(2, 0.1, 0.9, 0.1)


def test_merge_dedupes_overlap_preferring_row_not_on_edge():
    # row 4 is the last row of strip 0 (cut by the edge) and the first row of strip 1
    s0 = [row(1), row(2), row(3), row(4, "CORTADO", conf=0.99)]
    s1 = [row(4, "COMPLETO", conf=0.5), row(5), row(6)]
    merged = merge_strips([s0, s1], max_row=40)
    assert [r.row_number for r in merged.rows] == [1, 2, 3, 4, 5, 6]
    four = next(r for r in merged.rows if r.row_number == 4)
    # strip 1 also has 4 on its top edge -> both on edges -> confidence decides
    assert four.cells["nombre"].value == "CORTADO"
    assert merged.seen_in[4] == [0, 1]


def test_merge_prefers_middle_occurrence():
    s0 = [row(1), row(2), row(3, "BORDE", conf=0.99)]
    s1 = [row(2), row(3, "CENTRO", conf=0.6), row(4)]
    merged = merge_strips([s0, s1], max_row=40)
    assert next(r for r in merged.rows if r.row_number == 3).cells["nombre"].value == "CENTRO"


def test_merge_drops_rows_without_valid_number():
    merged = merge_strips([[row(None), row(0), row(99), row(2)]], max_row=40)
    assert [r.row_number for r in merged.rows] == [2]


def test_missing_rows_considers_empty_rows_as_read():
    rows = [row(1), row(3), row(4, empty=True)]
    assert missing_row_numbers(rows, expected=5) == [2, 5]
    # rows beyond the expected count extend the range
    assert missing_row_numbers([row(1), row(7)], expected=5) == [2, 3, 4, 5, 6]


def test_requery_band_uses_strips_where_neighbours_were_seen():
    bounds = [(0.0, 0.4), (0.3, 0.7), (0.6, 1.0)]
    seen = {1: [0], 2: [0], 4: [0, 1], 5: [1], 9: [2]}
    assert requery_band(3, seen, bounds) == (0.0, 0.7)
    assert requery_band(7, seen, bounds) == (0.3, 1.0)
    # no neighbours at all -> whole table
    assert requery_band(3, {}, bounds) == (0.0, 1.0)


def test_extracted_row_accepts_flat_model_output():
    ex = parse_extraction(
        {"header": {}, "rows": [{"row_number": 7, "empty": False, "nombre": {"value": "Ana", "confidence": 0.9}}]}
    )
    (r,) = ex.rows
    assert r.row_number == 7 and r.empty is False
    assert r.cells["nombre"].text == "Ana"


def test_shipped_config_overlaps_at_least_two_rows_at_every_cut(mapping):
    """No printed row may sit exactly on a cut without being whole in some strip."""
    lay = mapping.ocr
    bounds = strip_bounds(lay.strips, lay.strip_overlap, lay.table_top, lay.table_bottom)
    row_height = (lay.table_bottom - lay.table_top) / lay.expected_rows  # upper bound: table is smaller
    for (_, bottom), (top, _) in zip(bounds, bounds[1:]):
        assert bottom - top >= 2 * row_height


def person(n, name, doc, empty=False):
    cells = {} if empty else {"nombre": {"value": name, "confidence": 0.9},
                              "numero_documento": {"value": doc, "confidence": 0.9}}
    return ExtractedRow(row_number=n, empty=empty, cells=cells)


def test_merge_realigns_a_strip_whose_row_numbers_are_shifted():
    # Regression (page 1 of the sample sheet): the lower strip numbered its rows one
    # less than printed, so patient No. 19 came back as 18 and the real No. 25 was
    # reported as an empty 25 -> one patient lost and another duplicated.
    names = {n: (f"Paciente {chr(64 + n)}{chr(90 - n)}", f"10{n:02d}93{n * 7:03d}") for n in range(15, 26)}
    upper = [person(n, *names[n]) for n in range(15, 23)]
    # same patients re-read with small OCR differences, numbered n - 1
    lower = [person(n - 1, names[n][0].lower(), names[n][1][:-1] + "0") for n in range(19, 26)]
    lower.append(person(25, None, None, empty=True))
    merged = merge_strips([upper, lower], max_row=40)
    by_number = {r.row_number: r for r in merged.rows if not r.empty}
    assert sorted(by_number) == list(range(15, 26))
    assert by_number[19].cells["nombre"].text.lower() == names[19][0].lower()
    assert by_number[25].cells["nombre"].text.lower() == names[25][0].lower()


def test_merge_keeps_numbers_when_strips_agree():
    s0 = [person(n, f"Nombre{n} Apellido{n}", f"52{n:03d}111") for n in range(1, 9)]
    s1 = [person(n, f"Nombre{n} Apellido{n}", f"52{n:03d}111") for n in range(6, 14)]
    merged = merge_strips([s0, s1], max_row=40)
    assert [r.row_number for r in merged.rows] == list(range(1, 14))


# --------------------------------------------------------------------------- table geometry

from plan_cuidado.domain.mapping import OcrConfig  # noqa: E402
from plan_cuidado.domain.ocr import TableBox  # noqa: E402
from plan_cuidado.domain.strips import table_geometry  # noqa: E402


def test_table_geometry_from_located_box_pads_edges_and_keeps_last_row():
    box = TableBox(titles_top=270, titles_bottom=300, left=140, right=940, last_row_bottom=800)
    geo = table_geometry(box, OcrConfig(expected_rows=25, table_top=0.15, table_bottom=0.95))
    assert geo.left < 0.14 and geo.right > 0.94  # a little margin on both sides
    assert geo.titles[0] < 0.27 and geo.titles[1] >= 0.30
    row = (0.80 - 0.30) / 25
    assert geo.top <= 0.30 and geo.bottom >= 0.80 + row / 2  # models place the last border a bit high
    assert geo.bottom <= 1.0


@pytest.mark.parametrize(
    "box",
    [
        None,
        TableBox(titles_top=300, titles_bottom=270, left=140, right=940, last_row_bottom=800),  # inverted
        TableBox(titles_top=270, titles_bottom=300, left=140, right=300, last_row_bottom=800),  # too narrow
        TableBox(titles_top=270, titles_bottom=300, left=140, right=940, last_row_bottom=350),  # too short
        TableBox(titles_top=100, titles_bottom=400, left=140, right=940, last_row_bottom=900),  # titles too tall
        TableBox(titles_top=270, titles_bottom=300, left=140, right=1400, last_row_bottom=800),  # out of range
    ],
)
def test_implausible_box_falls_back_to_configured_table_band(box):
    geo = table_geometry(box, OcrConfig(table_top=0.15, table_bottom=0.95))
    assert (geo.top, geo.bottom, geo.left, geo.right, geo.titles) == (0.15, 0.95, 0.0, 1.0, None)


def test_locating_can_be_turned_off():
    box = TableBox(titles_top=270, titles_bottom=300, left=140, right=940, last_row_bottom=800)
    geo = table_geometry(box, OcrConfig(locate_table=False, table_top=0.15, table_bottom=0.95))
    assert geo.titles is None and geo.top == 0.15


def test_merge_without_confidences_prefers_the_reading_with_more_cells():
    sparse = ExtractedRow(row_number=4, cells={"nombre": "ANA", "numero_documento": None})
    full = ExtractedRow(row_number=4, cells={"nombre": "ANA", "numero_documento": "123"})
    # row 4 is on the bottom edge of strip 1 and the top edge of strip 2: both on edges
    merged = merge_strips([[row(3, "LUIS"), sparse], [full, row(5, "MARTA")]], 25)
    four = next(r for r in merged.rows if r.row_number == 4)
    assert four.cells["numero_documento"].value == "123"


def doc_only(n, doc):
    return ExtractedRow(row_number=n, cells={"numero_documento": doc})


def test_reference_rows_realign_a_shifted_strip_of_a_critical_columns_reader():
    reference = [person(n, f"NOMBRE {n}", f"10{n:02d}5{n:02d}7{n:02d}") for n in range(1, 11)]
    docs = {n: f"10{n:02d}5{n:02d}7{n:02d}" for n in range(1, 11)}
    strip1 = [doc_only(n, docs[n]) for n in range(1, 6)]
    strip2 = [doc_only(n - 1, docs[n]) for n in range(5, 11)]  # counted rows: numbers one too low
    merged = merge_strips([strip1, strip2], 25, reference=reference)
    assert {r.row_number: r.cells["numero_documento"].value for r in merged.rows} == docs


