from plan_cuidado.domain.consensus import apply_consensus
from plan_cuidado.domain.tracker import TrackerRow

CRITICAL = ["telefono", "codigo_de_ficha", "fecha_de_visita"]


def r(n, **answers):
    flags = answers.pop("flags", {})
    return TrackerRow(row_id=f"F-p1-r{n}", source_row=n, answers=answers, ocr_flags=flags)


def test_agreement_keeps_value_and_adds_nothing():
    primary = [r(1, telefono="3101234567", barrio="A")]
    other = {"b": [r(1, telefono="3101234567", barrio="B")]}
    (out,) = apply_consensus(primary, "a", other, CRITICAL)
    assert out.answers["telefono"] == "3101234567"
    assert out.ocr_disagreements == {}  # barrio is not critical


def test_disagreement_keeps_primary_value_and_records_both_readings():
    primary = [r(1, telefono="3101234567")]
    other = {"b": [r(1, telefono="3101234561")]}
    (out,) = apply_consensus(primary, "a", other, CRITICAL)
    assert out.answers["telefono"] == "3101234567"
    entry = out.ocr_disagreements["telefono"]
    assert entry["value"] == "3101234567"
    assert "3101234567" in entry["note"] and "3101234561" in entry["note"]
    assert "a" in entry["note"] and "b" in entry["note"]


def test_one_side_empty_is_a_disagreement_and_shows_raw_reading():
    primary = [r(1, flags={"codigo_de_ficha": "Confianza baja (0.40): OCR leyó '0202040299'"})]
    other = {"b": [r(1, codigo_de_ficha="0202040299")]}
    (out,) = apply_consensus(primary, "a", other, CRITICAL)
    entry = out.ocr_disagreements["codigo_de_ficha"]
    assert entry["value"] == ""
    assert "Confianza baja" in entry["note"] and "0202040299" in entry["note"]


def test_both_empty_or_row_missing_in_other_model_is_not_compared():
    primary = [r(1), r(2, telefono="3101234567")]
    other = {"b": [r(1)]}
    out = apply_consensus(primary, "a", other, CRITICAL)
    assert all(o.ocr_disagreements == {} for o in out)


def test_no_other_models_returns_rows_unchanged():
    rows = [r(1, telefono="1")]
    assert apply_consensus(rows, "a", {}, CRITICAL) == rows


def test_with_three_readers_the_majority_value_is_kept_and_still_marked():
    primary = [r(1, telefono="3101234560")]
    others = {"b": [r(1, telefono="3101234567")], "c": [r(1, telefono="3101234567")]}
    (out,) = apply_consensus(primary, "a", others, CRITICAL)
    assert out.answers["telefono"] == "3101234567"  # 2 of 3 readers
    entry = out.ocr_disagreements["telefono"]
    assert entry["value"] == "3101234567"  # purple until a human checks it
    assert "3101234560" in entry["note"]


def test_without_a_strict_majority_the_primary_value_is_kept():
    primary = [r(1, telefono="3101234560")]
    others = {"b": [r(1, telefono="3101234567")], "c": [r(1, telefono="3101234561")]}
    (out,) = apply_consensus(primary, "a", others, CRITICAL)
    assert out.answers["telefono"] == "3101234560"
    assert out.ocr_disagreements["telefono"]["value"] == "3101234560"


def test_an_empty_majority_never_erases_a_value():
    primary = [r(1, telefono="3101234560")]
    others = {"b": [r(1)], "c": [r(1)]}
    (out,) = apply_consensus(primary, "a", others, CRITICAL)
    assert out.answers["telefono"] == "3101234560"
