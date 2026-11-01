import pytest

from plan_cuidado.domain.bench import consensus_stats, score_page, summarize_usage
from plan_cuidado.domain.mapping import OcrConfig
from plan_cuidado.domain.ocr import parse_extraction
from plan_cuidado.services.bench import run_bench
from tests.fakes import FakeRenderer, FakeVision, MemoryCache, ScriptedPage
from tests.test_ocr_mapping import GOOD_ROW, cell, page

CRITICAL = ["telefono", "ficha"]
TRUTH = {
    "page": 1,
    "header": {"ebs": "02"},
    "rows": [
        {"row": 1, "nombre": "Luisa Fernanda Castillo", "telefono": "310 123 4567", "barrio": "Ciudadela Maipore"},
        {"row": 2, "nombre": "Pedro Pérez", "telefono": "3209998877", "barrio": ""},
    ],
    "uncertain": ["r2.telefono?"],
}


def numbered(n, **overrides):
    return dict(GOOD_ROW, row_number=n, empty=False, **overrides)


def test_score_page_per_field_matches_rows_by_number():
    pred = parse_extraction(
        page([numbered(2, nombre=cell("PEDRO PEREZ"), telefono=cell("3209998870"), barrio=cell(None)), numbered(1)])
    )
    score = score_page(TRUTH, pred, CRITICAL)
    assert score.fields["nombre"].accuracy == 1.0  # case and accents are ignored; order irrelevant
    assert score.fields["telefono"].accuracy == 0.5  # spaces ignored, one digit wrong
    assert score.fields["barrio"].total == 1  # empty in both -> not counted
    assert score.fields["ebs"].accuracy == 1.0
    assert score.critical.accuracy == pytest.approx(0.5)  # only telefono present in truth
    assert score.missing_rows == []
    assert 0 < score.overall < 1


def test_missing_predicted_rows_are_counted():
    score = score_page(TRUTH, parse_extraction(page([numbered(1)])), CRITICAL)
    assert score.missing_rows == [2]
    assert score.fields["nombre"].accuracy == 0.5


def test_truth_without_row_numbers_uses_order():
    truth = {"rows": [{"nombre": "Luisa Fernanda Castillo"}]}
    assert score_page(truth, parse_extraction(page([GOOD_ROW])), CRITICAL).fields["nombre"].accuracy == 1.0


def test_consensus_stats_disagreement_and_residual_error():
    a = parse_extraction(page([numbered(1, telefono=cell("3101234567")), numbered(2, telefono=cell("3209998870"))]))
    b = parse_extraction(page([numbered(1, telefono=cell("3101234567")), numbered(2, telefono=cell("3209998877"))]))
    stats = consensus_stats(TRUTH, a, b, ["telefono"])
    assert stats.compared == 2
    assert stats.disagreements == 1  # row 2: caught for human review
    assert stats.residual_errors == 0
    wrong = parse_extraction(page([numbered(1, telefono=cell("3101234560")), numbered(2, telefono=cell("3209998877"))]))
    same_wrong = consensus_stats(TRUTH, wrong, wrong, ["telefono"])
    assert same_wrong.disagreements == 0 and same_wrong.residual_errors == 1  # both agree, both wrong
    assert same_wrong.disagreement_rate == 0 and same_wrong.residual_rate == 0.5


def test_summarize_usage():
    s = summarize_usage([{"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001}, {"prompt_tokens": 1}])
    assert s == {"prompt_tokens": 11, "completion_tokens": 5, "cost": pytest.approx(0.001)}


HEADER = {"ebs": cell("02"), "colaborador": cell("x")}
LAYOUT = OcrConfig(strips=2, expected_rows=2)


@pytest.fixture
def bench_mapping(mapping):
    return mapping.model_copy(update={"ocr": LAYOUT, "ocr_columns": {k: v for k, v in mapping.ocr_columns.items()}})


def test_run_bench_uses_the_strip_pipeline_for_each_model_and_reports_errors(bench_mapping):
    rows = [numbered(1, nombre=cell("Luisa Fernanda Castillo")), numbered(2, nombre=cell("Pedro Perez"))]
    vision = FakeVision({"a": ScriptedPage(HEADER, rows), "b": [RuntimeError("no disponible")]})
    report = run_bench(TRUTH, b"pdf", ["a", "b"], vision, FakeRenderer(pages=2), bench_mapping)
    a, b = report.results
    assert a.model == "a" and a.error is None and a.score.fields["nombre"].accuracy == 1.0
    assert a.usage["cost"] == pytest.approx(0.001 * 3)  # header + 2 strips
    assert b.error and "no disponible" in b.error
    assert report.consensus is None


def test_run_bench_consensus_mode_reuses_model_results(bench_mapping):
    rows = [numbered(1), numbered(2, telefono=cell("3209998877"))]
    other = [numbered(1), numbered(2, telefono=cell("3209998870"))]
    vision = FakeVision({"a": ScriptedPage(HEADER, rows), "b": ScriptedPage(HEADER, other)})
    report = run_bench(TRUTH, b"pdf", ["a"], vision, FakeRenderer(), bench_mapping, consensus=("a", "b"))
    assert [r.model for r in report.results] == ["a", "b"]
    assert report.consensus.disagreements == 1
    assert len([m for _, m in vision.calls if m == "a"]) == 3  # read once, not twice


def test_run_bench_consensus_reads_like_pc_ocr_when_critical_only(bench_mapping):
    rows = [numbered(1), numbered(2, telefono=cell("3209998877"))]
    other = [numbered(1), numbered(2, telefono=cell("3209998870"))]
    vision = FakeVision({"a": ScriptedPage(HEADER, rows), "b": ScriptedPage(HEADER, other)})
    critical_only = bench_mapping.model_copy(
        update={"ocr": bench_mapping.ocr.model_copy(update={"consensus_critical_only": True})}
    )
    report = run_bench(TRUTH, b"pdf", [], vision, FakeRenderer(), critical_only, consensus=("a", "b"))
    second = [s for m, s in vision.schemas if m == "b"]
    assert len(second) == 2  # strips only: the header (rotation, table) comes from "a"
    columns = set(second[0]["properties"]["rows"]["items"]["properties"]) - {"row_number", "empty"}
    assert columns == set(critical_only.critical_columns())
    assert report.consensus.disagreements == 1


def test_run_bench_page_out_of_range(bench_mapping):
    with pytest.raises(ValueError):
        run_bench(dict(TRUTH, page=9), b"pdf", ["a"], FakeVision({"a": [page([])]}), FakeRenderer(pages=2), bench_mapping)


# --------------------------------------------------------------------------- production scoring

from plan_cuidado.domain.bench import score_rows  # noqa: E402
from plan_cuidado.domain.tracker import TrackerRow  # noqa: E402

PROD_TRUTH = {
    "page": 1,
    "rows": [
        {"row": 1, "fecha_plan": "06-08-26", "tipo_doc": "CC", "numero_documento": "1.000.000.001",
         "ficha": "0202000001", "telefono": "310 000 0001", "barrio": "Ciudadela Maipore"},
        {"row": 2, "fecha_plan": "06-08-26", "tipo_doc": "TI", "numero_documento": "1000000002",
         "ficha": "0202000002", "telefono": "3100000002"},
        {"row": 3, "telefono": "3100000003"},
    ],
}


def tracker_row(n, answers, flags=None, disagreements=None):
    return TrackerRow(row_id=f"F-p1-r{n}", answers=answers, source_row=n, ocr_flags=flags or {},
                      ocr_disagreements=disagreements or {})


def test_score_rows_compares_normalized_values_and_classifies_marks(schema, mapping):
    rows = [
        tracker_row(1, {
            "fecha_de_visita": "2026-08-06", "tipo_de_documento_de_identificacion": "3. CÉDULA DE CIUDADANÍA (CC)",
            "numero_de_documento_de_identidad": "1000000001", "documento": "1000000001",
            "codigo_de_ficha": "0202000001", "telefono": "3100000001", "barrio": "CIUDADELA MAIPORE",
        }),
        tracker_row(2, {
            "fecha_de_visita": "2026-08-06", "tipo_de_documento_de_identificacion": "3. CÉDULA DE CIUDADANÍA (CC)",
            "numero_de_documento_de_identidad": "1000000009", "documento": "1000000009",
            "codigo_de_ficha": "0202000009", "telefono": "3100000002",
        }, flags={}, disagreements={"codigo_de_ficha": {"value": "0202000009", "note": "x"}}),
        # row 3: phone not read but flagged as low confidence (yellow)
        tracker_row(3, {}, flags={"telefono": "Confianza baja"}),
    ]
    score = score_rows(PROD_TRUTH, rows, schema, mapping, pivot_year=2026)
    assert score.fields["fecha_plan"].ok == 2
    assert score.fields["barrio"].ok == 1
    tipo = score.fields["tipo_doc"]
    assert (tipo.ok, tipo.silent) == (1, 1)  # row 2: CC instead of TI and nothing marks it
    doc = score.fields["numero_documento"]
    assert doc.silent == 1 and doc.accuracy == 0.5
    ficha = score.fields["ficha"]
    assert (ficha.ok, ficha.caught) == (1, 1)  # purple disagreement catches the wrong ficha
    tel = score.fields["telefono"]
    assert (tel.ok, tel.caught) == (2, 1)
    assert score.critical.total == 11 and score.critical.silent == 2
    assert ("r02", "tipo_doc") in score.silent_cells and ("r02", "numero_documento") in score.silent_cells
    assert score.marked_critical == 2  # ficha r2 (purple) + telefono r3 (yellow)


def test_score_rows_unread_placeholder_is_caught_but_absent_row_slips_through(schema, mapping):
    placeholders = [tracker_row(n, {}) for n in (1, 2, 3)]  # "fila no leída": required cells go red
    caught = score_rows(PROD_TRUTH, placeholders, schema, mapping, pivot_year=2026)
    assert caught.fields["telefono"].caught == 3 and caught.critical.silent == 0
    absent = score_rows(PROD_TRUTH, [], schema, mapping, pivot_year=2026)  # e.g. read as an empty row
    assert absent.missing_rows == [1, 2, 3]
    assert absent.fields["telefono"].silent == 3


def test_run_bench_scores_production_rows_and_consensus_marks(schema, bench_mapping):
    truth = {"page": 1, "rows": [{"row": 1, "telefono": "3101234567"}, {"row": 2, "telefono": "3209998877"}]}
    a = [numbered(1), numbered(2, telefono=cell("3209998870"))]  # a misreads row 2
    b = [numbered(1), numbered(2, telefono=cell("3209998877"))]
    vision = FakeVision({"a": ScriptedPage(HEADER, a), "b": ScriptedPage(HEADER, b)})
    report = run_bench(truth, b"pdf", [], vision, FakeRenderer(), bench_mapping, schema=schema, consensus=("a", "b"))
    alone = report.results[0].rows_score.fields["telefono"]
    assert (alone.ok, alone.silent) == (1, 1)  # a alone: the wrong phone slips through
    paired = report.consensus_score.fields["telefono"]
    assert (paired.ok, paired.caught, paired.silent) == (1, 1, 0)  # the pair paints it purple


def test_run_bench_reuses_the_cache(bench_mapping):
    cache = MemoryCache()
    vision = FakeVision({"a": ScriptedPage(HEADER, [numbered(1), numbered(2)])})
    run_bench(TRUTH, b"pdf", ["a"], vision, FakeRenderer(), bench_mapping, cache=cache)
    paid = len(vision.calls)
    again = run_bench(TRUTH, b"pdf", ["a"], vision, FakeRenderer(), bench_mapping, cache=cache)
    assert len(vision.calls) == paid and again.results[0].usage == {}
    run_bench(TRUTH, b"other pdf", ["a"], vision, FakeRenderer(), bench_mapping, cache=cache)
    assert len(vision.calls) == 2 * paid  # another PDF is another cache entry


def test_score_rows_ignores_spacing_and_punctuation_in_free_text(schema, mapping):
    truth = {"rows": [{"row": 1, "direccion": "KR 15H-32D-26 SUR", "telefono": "3100000001"}]}
    rows = [tracker_row(1, {"direccion_de_la_vivienda": "KR 15H 32D-26SUR", "telefono": "3100000001"})]
    assert score_rows(truth, rows, schema, mapping).fields["direccion"].ok == 1


def test_run_bench_scores_the_household_ficha_mark(bench_mapping, schema):
    truth = {"page": 1, "rows": [{"row": 1, "ficha": "0202012345"}, {"row": 2, "ficha": "0202012345"}]}
    rows = [numbered(1), numbered(2, integrante=cell("2"), ficha=cell("0202012845"))]
    vision = FakeVision({"a": ScriptedPage(HEADER, rows)})
    report = run_bench(truth, b"pdf", ["a"], vision, FakeRenderer(), bench_mapping, schema)
    (result,) = report.results
    assert result.rows_score.fields["ficha"].caught == 1
    assert result.rows_score.fields["ficha"].silent == 0


def test_run_bench_consensus_of_three_models_votes_like_pc_ocr(bench_mapping, schema):
    truth = {"page": 1, "rows": [{"row": 1, "telefono": "3209998877"}, {"row": 2, "telefono": "3209998877"}]}
    a = [numbered(1, telefono=cell("3209998870")), numbered(2, telefono=cell("3209998877"))]
    ok = [numbered(1, telefono=cell("3209998877")), numbered(2, telefono=cell("3209998877"))]
    vision = FakeVision({"a": ScriptedPage(HEADER, a), "b": ScriptedPage(HEADER, ok), "c": ScriptedPage(HEADER, ok)})
    report = run_bench(truth, b"pdf", [], vision, FakeRenderer(), bench_mapping, schema, consensus=("a", "b", "c"))
    assert [r.model for r in report.results] == ["a", "b", "c"]
    tel = report.consensus_score.fields["telefono"]
    assert tel.ok == 1 and tel.ok_marked == 1 and tel.silent == 0  # majority value, still purple
    assert report.consensus is None  # pairwise disagreement stats only for two models
