from plan_cuidado.domain.static_fill import (
    REMOVED_PREFIX,
    STATIC_HEADERS,
    apply_static_and_derived,
    merge_static_table,
    static_from_table,
)
from plan_cuidado.domain.tracker import Status, TrackerRow


def _row(status=Status.EXTRACTED, **answers):
    return TrackerRow(row_id="r", status=status, answers=dict(answers))


def test_new_static_table_has_one_row_per_question(schema):
    grid, seeded = merge_static_table([], schema, {})
    assert grid[0] == STATIC_HEADERS == ["clave", "pregunta", "valor", "obligatoria"]
    assert [r[0] for r in grid[1:]] == schema.keys
    ficha = next(r for r in grid if r[0] == "codigo_de_ficha")
    assert ficha == ["codigo_de_ficha", "CODIGO DE FICHA", "", "SI"]
    assert seeded == 0


def test_refresh_keeps_values_adds_new_and_marks_removed(schema):
    old = [STATIC_HEADERS, ["tipo_de_visita", "TIPO", "PRIMERA VISITA", "SI"], ["vieja", "VIEJA", "x", "NO"]]
    grid, _ = merge_static_table(old, schema, {})
    by_key = {r[0]: r for r in grid[1:]}
    assert by_key["tipo_de_visita"][2] == "PRIMERA VISITA"
    assert set(schema.keys) <= set(by_key)
    assert by_key["vieja"][1].startswith(REMOVED_PREFIX) and by_key["vieja"][2] == "x"
    again, _ = merge_static_table(grid, schema, {})
    assert again == grid  # idempotent: the prefix is not added twice


def test_seed_fills_only_empty_values(schema):
    old = [STATIC_HEADERS, ["tipo_de_visita", "TIPO", "SEGUIMIENTO", "SI"]]
    grid, seeded = merge_static_table(old, schema, {"tipo_de_visita": "PRIMERA VISITA", "barrio": "ARIZONA"})
    by_key = {r[0]: r for r in grid[1:]}
    assert by_key["tipo_de_visita"][2] == "SEGUIMIENTO" and by_key["barrio"][2] == "ARIZONA"
    assert seeded == 1


def test_static_from_table_ignores_removed_and_empty(schema):
    grid = [STATIC_HEADERS, ["barrio", "BARRIO", " ARIZONA ", "SI"], ["edad", "EDAD", "", "SI"],
            ["vieja", REMOVED_PREFIX + "VIEJA", "x", ""]]
    assert static_from_table(grid) == {"barrio": "ARIZONA"}


def test_static_never_overwrites_existing_values(schema, mapping):
    static = {"valores": {"tipo_de_visita": "PRIMERA VISITA", "barrio": "ARIZONA", "unknown": "x"}}
    row = _row(barrio="CIUDADELA MAIPORE")
    out = apply_static_and_derived(row, schema, mapping, static)
    assert out.answers["tipo_de_visita"] == "PRIMERA VISITA"
    assert out.answers["barrio"] == "CIUDADELA MAIPORE"
    assert "unknown" not in out.answers


def test_flat_static_data_is_accepted(schema, mapping):
    out = apply_static_and_derived(_row(), schema, mapping, {"tipo_de_visita": "SEGUIMIENTO", "_nota": "x"})
    assert out.answers["tipo_de_visita"] == "SEGUIMIENTO"


def test_derived_fields(schema, mapping):
    row = _row(codigo_del_territorio="T02", fecha_de_nacimiento="1990-05-10", fecha_de_visita="2026-08-06")
    out = apply_static_and_derived(row, schema, mapping, {})
    assert out.answers["nombre_del_territorio"] == "T02COMPARTIR"
    assert out.answers["edad"] == "36"
    assert out.answers["curso_de_vida"].startswith("ADULTEZ")


def test_derived_does_not_overwrite(schema, mapping):
    row = _row(codigo_del_territorio="T02", nombre_del_territorio="T12COMPARTIR", edad="40")
    out = apply_static_and_derived(row, schema, mapping, {})
    assert out.answers["nombre_del_territorio"] == "T12COMPARTIR"
    assert out.answers["edad"] == "40"


def test_conditional_defaults(schema, mapping):
    out = apply_static_and_derived(_row(cuenta_con_alguna_discapacidad="NO"), schema, mapping, {})
    assert out.answers["en_caso_de_ser_si_la_respuesta_especifique_cual"] == "NO APLICA"
    out = apply_static_and_derived(_row(cuenta_con_alguna_discapacidad="SI"), schema, mapping, {})
    assert out.answers.get("en_caso_de_ser_si_la_respuesta_especifique_cual", "") == ""


def test_static_values_feed_conditionals(schema, mapping):
    static = {"valores": {"cuenta_con_alguna_discapacidad": "NO"}}
    out = apply_static_and_derived(_row(), schema, mapping, static)
    assert out.answers["en_caso_de_ser_si_la_respuesta_especifique_cual"] == "NO APLICA"


def test_submitted_rows_are_untouched(schema, mapping):
    row = _row(status=Status.SUBMITTED)
    assert apply_static_and_derived(row, schema, mapping, {"tipo_de_visita": "SEGUIMIENTO"}) is row
