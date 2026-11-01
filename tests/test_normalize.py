import pytest

from plan_cuidado.domain.normalize import (
    fold,
    match_option,
    normalize_digits,
    normalize_text,
    parse_date,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("06-08-26", "2026-08-06"),
        ("6/8/26", "2026-08-06"),
        ("06.08.2026", "2026-08-06"),
        ("31-12-99", "1999-12-31"),
        ("2026-08-06", "2026-08-06"),
        (" 01 - 02 - 25 ", "2025-02-01"),
    ],
)
def test_parse_date(raw, expected):
    assert parse_date(raw, pivot_year=2026) == expected


@pytest.mark.parametrize("raw", ["", "32-01-26", "12-13-26", "abc", "1-2"])
def test_parse_date_invalid(raw):
    assert parse_date(raw, pivot_year=2026) is None


def test_normalize_digits():
    assert normalize_digits("1.023.456 789") == "1023456789"
    assert normalize_digits("  ") == ""


def test_normalize_text():
    assert normalize_text("  kr 15  # 3-20 ") == "KR 15 # 3-20"


def test_fold():
    assert fold("Cédula  de Ciudadanía") == "CEDULA DE CIUDADANIA"


DOC_TYPES = [
    "1. REGISTRO CIVIL (RC)",
    "2. TARJETA DE IDENTIDAD (TI)",
    "3. CÉDULA DE CIUDADANÍA (CC)",
    "4. CÉDULA DE EXTRANJERIA (CE)",
    "5. ACTA",
    "6. CÉDULA VENEZOLANA (CV)",
    "7. PERMISO POR PROTECCIÓN  TEMPORAL (PTT)",
    "8. REGISTRO NACIDO VIVO",
    "9. PASAPORTE",
]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("CC", "3. CÉDULA DE CIUDADANÍA (CC)"),
        ("c.c.", "3. CÉDULA DE CIUDADANÍA (CC)"),
        ("TI", "2. TARJETA DE IDENTIDAD (TI)"),
        ("RC", "1. REGISTRO CIVIL (RC)"),
        ("PPT", "7. PERMISO POR PROTECCIÓN  TEMPORAL (PTT)"),
        ("pasaporte", "9. PASAPORTE"),
    ],
)
def test_match_doc_type(raw, expected):
    assert match_option(raw, DOC_TYPES, aliases={"PPT": "PTT", "PA": "PASAPORTE"}).value == expected


def test_match_codes_by_number(schema):
    terr = schema.by_key("codigo_del_territorio").options
    mtc = schema.by_key("microterritorio").options
    ebs = schema.by_key("equipo_basico_en_salud").options
    assert match_option("02", terr).value == "T02"
    assert match_option("2", terr).value == "T02"
    assert match_option("04", mtc).value == "MTC04"
    assert match_option("02", ebs).value == "EBS 02"
    assert match_option("T 31", terr).value == "T31"


def test_missing_code_is_not_invented(schema):
    terr = schema.by_key("codigo_del_territorio").options
    r = match_option("30", terr)  # T30 does not exist in the form
    assert r.value is None
    assert r.reason


def test_ambiguous_number_is_rejected(schema):
    comuna = schema.by_key("comuna_corregimiento").options
    assert match_option("1", comuna).value == "COMUNA 1"
    assert match_option("2", comuna).value is None  # COMUNA 2 vs CORREGIMIENTO 2
    assert match_option("corregimiento 2", comuna).value == "CORREGIMIENTO 2"


def test_barrio_fuzzy(schema):
    barrios = schema.by_key("barrio").options
    assert match_option("Ciudad Latina 2", barrios).value == "CIUDAD LATINA II" or match_option(
        "Ciudad Latina II", barrios
    ).value == "CIUDAD LATINA II"
    assert match_option("ciudadela maipore", barrios).value == "CIUDADELA MAIPORE"
    assert match_option("arisona", barrios).value == "ARIZONA"
    r = match_option("zzzz qqqq", barrios)
    assert r.value is None and r.score is not None


def test_empty_raw():
    assert match_option("", ["A"]).value is None
