import pytest

from plan_cuidado.domain.tracker import (
    CONTROL_COLUMNS,
    InvalidTransition,
    Status,
    TrackerRow,
    build_columns,
    dedupe_new_rows,
    make_row_id,
    processed_file_ids,
    row_to_values,
    transition,
    values_to_row,
)


def _row(**kw):
    base = dict(row_id="r1", status=Status.EXTRACTED, answers={})
    base.update(kw)
    return TrackerRow(**base)


def test_columns_contain_every_question_and_control(schema):
    cols = build_columns(schema)
    keys = [c.key for c in cols]
    assert set(schema.keys) <= set(keys)
    assert set(CONTROL_COLUMNS) <= set(keys)
    assert len(keys) == len(set(keys))
    assert keys[0] == "row_id"
    headers = [c.header for c in cols]
    assert "CODIGO DE FICHA" in headers


def test_row_roundtrip_by_header(schema):
    cols = build_columns(schema)
    row = _row(
        answers={"codigo_de_ficha": "0202012345", "barrio": "ARIZONA"},
        source_file_name="a.pdf",
        source_file_id="F1",
        source_page=2,
        source_row=5,
        ocr_confidence=0.81,
        ocr_flags={"telefono": "Confianza baja"},
    )
    values = row_to_values(row, cols)
    headers = [c.header for c in cols]
    # columns may be reordered by the user in the Sheet: mapping is by header
    pairs = list(zip(headers, values))[::-1]
    back = values_to_row([h for h, _ in pairs], [v for _, v in pairs], schema)
    assert back == row


def test_values_to_row_tolerates_short_rows(schema):
    cols = build_columns(schema)
    headers = [c.header for c in cols]
    row = values_to_row(headers, ["x1", "READY"], schema)
    assert row.row_id == "x1"
    assert row.status is Status.READY
    assert row.answers.get("barrio", "") == ""


def test_transitions():
    r = _row()
    r = transition(r, Status.NEEDS_REVIEW)
    r = transition(r, Status.READY)
    r = transition(r, Status.SUBMITTED)
    with pytest.raises(InvalidTransition):
        transition(r, Status.READY)
    with pytest.raises(InvalidTransition):
        transition(_row(), Status.SUBMITTED)  # must be validated first
    failed = transition(transition(_row(status=Status.READY), Status.FAILED), Status.READY)
    assert failed.status is Status.READY


def test_processed_file_ids():
    rows = [_row(source_file_id="A"), _row(row_id="r2", source_file_id="B"), _row(row_id="r3")]
    assert processed_file_ids(rows) == {"A", "B"}


def test_row_id_is_deterministic():
    assert make_row_id("1AbCdEfGhIjK", 2, 7) == make_row_id("1AbCdEfGhIjK", 2, 7)
    assert make_row_id("1AbCdEfGhIjK", 2, 7) != make_row_id("1AbCdEfGhIjK", 2, 8)


def test_dedupe_new_rows():
    keys = ["numero_de_documento_de_identidad", "fecha_de_visita"]
    existing = [_row(answers={"numero_de_documento_de_identidad": "1", "fecha_de_visita": "2026-01-01"})]
    new = [
        _row(row_id="n1", answers={"numero_de_documento_de_identidad": "1", "fecha_de_visita": "2026-01-01"}),
        _row(row_id="n2", answers={"numero_de_documento_de_identidad": "2", "fecha_de_visita": "2026-01-01"}),
        _row(row_id="n3", answers={"numero_de_documento_de_identidad": "2", "fecha_de_visita": "2026-01-01"}),
        _row(row_id="n4", answers={}),  # incomplete keys are never treated as duplicates
        _row(row_id="r1", answers={"numero_de_documento_de_identidad": "9"}),  # same row id
    ]
    kept, skipped = dedupe_new_rows(existing, new, keys)
    assert [r.row_id for r in kept] == ["n2", "n4"]
    assert [r.row_id for r in skipped] == ["n1", "n3", "r1"]


def test_row_to_header_values_follows_sheet_order(schema):
    from plan_cuidado.domain.tracker import row_to_header_values

    row = _row(answers={"barrio": "ARIZONA"}, source_page=3)
    headers = ["BARRIO", "unknown column", "source_page", "row_id"]
    assert row_to_header_values(row, headers, schema) == ["ARIZONA", None, "3", "r1"]


def test_disagreements_roundtrip_through_sheet_values(schema):
    from plan_cuidado.domain.tracker import build_columns, row_to_values, values_to_row

    row = TrackerRow(row_id="a", ocr_disagreements={"telefono": {"value": "1", "note": "n"}})
    cols = build_columns(schema)
    back = values_to_row([c.header for c in cols], row_to_values(row, cols), schema)
    assert back.ocr_disagreements == row.ocr_disagreements
    assert "ocr_disagreements" in [c.key for c in cols]
