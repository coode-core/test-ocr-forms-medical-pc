import json

import pytest

from plan_cuidado.domain.ocr import (
    PageExtraction,
    SourceRef,
    header_json_schema,
    rows_json_schema,
    flagged_ratio,
    parse_extraction,
    rows_from_extraction,
)
from plan_cuidado.domain.tracker import Status


def cell(v, c=0.95):
    return {"value": v, "confidence": c}


def page(rows, header=None, rotation=0):
    return {
        "rotation_degrees": rotation,
        "header": header or {"ebs": cell("02"), "colaborador": cell("Colaboradora Prueba")},
        "rows": rows,
    }


GOOD_ROW = {
    "fecha_plan": cell("06-08-26"),
    "nombre": cell("Luisa Fernanda Castillo"),
    "tipo_doc": cell("CC"),
    "numero_documento": cell("1.023.456.789"),
    "ficha": cell("0202012345"),
    "integrante": cell("1"),
    "direccion": cell("kr 15 # 3-20"),
    "telefono": cell("310 123 4567"),
    "comuna": cell("1"),
    "barrio": cell("Ciudadela Maipore"),
    "territorio": cell("02"),
    "microterritorio": cell("04"),
    "observaciones": cell(None, 0.0),
}

SRC = SourceRef(file_id="FILE123456", file_name="planilla.pdf", link="https://drive/x", page=1)


def test_json_schema_is_strict_and_lists_columns():
    s = rows_json_schema()
    row_props = s["properties"]["rows"]["items"]["properties"]
    assert "barrio" in row_props and "telefono" in row_props
    assert s["additionalProperties"] is False
    assert s["properties"]["rows"]["items"]["additionalProperties"] is False
    assert "ebs" in header_json_schema()["properties"]["header"]["properties"]


def test_parse_extraction_accepts_code_fences():
    raw = "```json\n" + json.dumps(page([GOOD_ROW])) + "\n```"
    ex = parse_extraction(raw)
    assert isinstance(ex, PageExtraction)
    assert ex.rows[0].cells["nombre"].value == "Luisa Fernanda Castillo"


def test_parse_extraction_rejects_garbage():
    with pytest.raises(ValueError):
        parse_extraction("no json here")


def test_maps_good_row(schema, mapping):
    ex = parse_extraction(page([GOOD_ROW]))
    (row,) = rows_from_extraction(ex, schema, mapping, SRC, pivot_year=2026)
    a = row.answers
    assert a["fecha_de_visita"] == "2026-08-06"
    assert a["nombre_del_usuario"] == "LUISA FERNANDA CASTILLO"
    assert a["tipo_de_documento_de_identificacion"] == "3. CÉDULA DE CIUDADANÍA (CC)"
    assert a["numero_de_documento_de_identidad"] == "1023456789"
    assert a["documento"] == "1023456789"
    assert a["codigo_de_ficha"] == "0202012345"
    assert a["telefono"] == "3101234567"
    assert a["comuna_corregimiento"] == "COMUNA 1"
    assert a["barrio"] == "CIUDADELA MAIPORE"
    assert a["codigo_del_territorio"] == "T02"
    assert a["microterritorio"] == "MTC04"
    assert a["equipo_basico_en_salud"] == "EBS 02"
    # colaborador mapping is disabled by default (doctor comes from static data)
    assert "nombre_del_medico_que_atiende_al_paciente" not in a
    assert row.ocr_flags == {}
    assert row.status is Status.EXTRACTED
    assert row.source_page == 1 and row.source_row == 1
    assert row.source_file_id == "FILE123456"
    assert row.ocr_confidence == pytest.approx(0.95)


def test_low_confidence_is_left_empty_and_flagged(schema, mapping):
    r = dict(GOOD_ROW, telefono=cell("3101234567", 0.4))
    (row,) = rows_from_extraction(parse_extraction(page([r])), schema, mapping, SRC)
    assert row.answers.get("telefono", "") == ""
    assert "3101234567" in row.ocr_flags["telefono"]
    assert row.ocr_confidence == pytest.approx(0.4)


def test_rule_failure_is_left_empty_and_flagged(schema, mapping):
    r = dict(GOOD_ROW, telefono=cell("60112345"), ficha=cell("9912"))
    (row,) = rows_from_extraction(parse_extraction(page([r])), schema, mapping, SRC)
    assert row.answers.get("telefono", "") == ""
    assert row.answers.get("codigo_de_ficha", "") == ""
    assert "telefono" in row.ocr_flags and "codigo_de_ficha" in row.ocr_flags


def test_unknown_option_is_left_empty_and_flagged(schema, mapping):
    r = dict(GOOD_ROW, territorio=cell("30"), fecha_plan=cell("45-13-26"))
    (row,) = rows_from_extraction(parse_extraction(page([r])), schema, mapping, SRC)
    assert row.answers.get("codigo_del_territorio", "") == ""
    assert row.answers.get("fecha_de_visita", "") == ""
    assert {"codigo_del_territorio", "fecha_de_visita"} <= set(row.ocr_flags)


def test_blank_rows_are_skipped_and_row_numbers_kept(schema, mapping):
    blank = {k: cell(None, 0.0) for k in GOOD_ROW}
    rows = rows_from_extraction(parse_extraction(page([blank, GOOD_ROW])), schema, mapping, SRC)
    assert len(rows) == 1
    assert rows[0].source_row == 2


def test_flagged_ratio(schema, mapping):
    good = rows_from_extraction(parse_extraction(page([GOOD_ROW])), schema, mapping, SRC)
    assert flagged_ratio(good) == 0
    bad_row = {k: cell("???", 0.2) for k in GOOD_ROW}
    bad = rows_from_extraction(parse_extraction(page([bad_row])), schema, mapping, SRC)
    assert flagged_ratio(bad) > 0.5
    assert flagged_ratio([]) == 1.0


def test_row_number_is_used_as_source_row_and_empty_rows_skipped(schema, mapping):
    rows = [dict(GOOD_ROW, row_number=7, empty=False), dict(GOOD_ROW, row_number=8, empty=True)]
    out = rows_from_extraction(parse_extraction(page(rows)), schema, mapping, SRC, pivot_year=2026)
    assert [r.source_row for r in out] == [7]
    assert out[0].row_id == "FILE123456-p1-r7"


def test_json_schemas_require_row_number_and_empty():
    row_schema = rows_json_schema()["properties"]["rows"]["items"]
    assert {"row_number", "empty"} <= set(row_schema["required"])
    assert row_schema["properties"]["row_number"]["type"] == "integer"
    assert set(header_json_schema()["required"]) == {"rotation_degrees", "header", "table"}


def test_strip_and_row_prompts_are_specific():
    from plan_cuidado.domain.ocr import row_prompt, strip_prompt

    assert "2 of 3" in strip_prompt(2, 3)
    assert "No. 14" in row_prompt(14)


@pytest.mark.parametrize("written", ["PT", "PPT", "PEP"])
def test_temporary_protection_permit_abbreviations_map_to_the_ptt_option(schema, mapping, written):
    (row,) = rows_from_extraction(parse_extraction(page([dict(GOOD_ROW, tipo_doc=cell(written))])), schema, mapping, SRC)
    assert row.answers["tipo_de_documento_de_identificacion"].endswith("(PTT)")


def test_cells_are_plain_strings_without_confidence_to_save_output_tokens():
    row_props = rows_json_schema()["properties"]["rows"]["items"]["properties"]
    assert row_props["telefono"] == {"type": ["string", "null"]}
    assert header_json_schema()["properties"]["header"]["properties"]["ebs"] == {"type": ["string", "null"]}


def test_plain_string_cells_are_mapped_without_confidence(schema, mapping):
    flat = {k: (v["value"]) for k, v in GOOD_ROW.items()}
    ex = parse_extraction({"rotation_degrees": 0, "header": {"ebs": "02", "colaborador": None}, "rows": [flat]})
    assert ex.header["ebs"].value == "02" and ex.header["colaborador"].value is None
    (row,) = rows_from_extraction(ex, schema, mapping, SRC)
    assert row.answers["telefono"] == "3101234567"
    assert row.ocr_flags == {}
    assert row.ocr_confidence is None


def test_prompts_do_not_ask_for_confidence_and_know_titles_are_pasted_on_top():
    from plan_cuidado.domain.ocr import HEADER_PROMPT, row_prompt, strip_prompt

    for prompt in (strip_prompt(2, 3), row_prompt(4), HEADER_PROMPT):
        assert "confidence" not in prompt
    assert "may not be visible" not in strip_prompt(2, 3)
    assert "column titles" in strip_prompt(2, 3) and "top" in strip_prompt(2, 3)


def test_rows_schema_and_prompts_can_ask_for_a_subset_of_columns():
    from plan_cuidado.domain.ocr import row_prompt, strip_prompt

    items = rows_json_schema(["numero_documento", "telefono"])["properties"]["rows"]["items"]
    assert set(items["properties"]) == {"row_number", "empty", "numero_documento", "telefono"}
    assert set(items["required"]) == set(items["properties"])
    for prompt in (strip_prompt(1, 3, ["numero_documento", "telefono"]), row_prompt(5, ["numero_documento", "telefono"])):
        assert "Return only these columns: numero_documento, telefono" in prompt
    assert "Return only these columns" not in strip_prompt(1, 3)
