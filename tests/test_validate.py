from plan_cuidado.domain.static_fill import apply_static_and_derived
from plan_cuidado.domain.tracker import Status, TrackerRow
from plan_cuidado.domain.validate import IssueKind, validate_row


def complete_answers(schema):
    answers = {}
    for q in schema.questions:
        if q.options:
            answers[q.key] = q.options[0]
        elif q.type.value == "date":
            answers[q.key] = "2026-08-06"
        else:
            answers[q.key] = "TEXTO"
    answers.update(
        telefono="3101234567",
        numero_de_documento_de_identidad="1023456789",
        documento="1023456789",
        codigo_de_ficha="0202012345",
        cuenta_con_alguna_discapacidad="NO",
        en_caso_de_ser_si_la_respuesta_especifique_cual="NO APLICA",
    )
    return answers


def _row(answers, flags=None):
    return TrackerRow(row_id="r", status=Status.EXTRACTED, answers=answers, ocr_flags=flags or {})


def kinds(issues):
    return {(i.key, i.kind) for i in issues}


def test_complete_row_is_ready(schema, mapping):
    issues, status = validate_row(_row(complete_answers(schema)), schema, mapping)
    assert issues == []
    assert status is Status.READY


def test_required_missing(schema, mapping):
    a = complete_answers(schema)
    a["barrio"] = ""
    issues, status = validate_row(_row(a), schema, mapping)
    assert ("barrio", IssueKind.REQUIRED) in kinds(issues)
    assert status is Status.NEEDS_REVIEW


def test_optional_missing_is_fine(schema, mapping):
    a = complete_answers(schema)
    a["comuna_corregimiento"] = ""
    a["e_mail"] = ""
    assert validate_row(_row(a), schema, mapping)[0] == []


def test_value_not_in_options(schema, mapping):
    a = complete_answers(schema)
    a["barrio"] = "BARRIO INVENTADO"
    assert ("barrio", IssueKind.NOT_IN_OPTIONS) in kinds(validate_row(_row(a), schema, mapping)[0])


def test_rule_failures_are_invalid(schema, mapping):
    a = complete_answers(schema)
    a.update(telefono="12345", numero_de_documento_de_identidad="10.23", codigo_de_ficha="X", fecha_de_visita="06-08-26")
    k = kinds(validate_row(_row(a), schema, mapping)[0])
    assert {
        ("telefono", IssueKind.INVALID),
        ("numero_de_documento_de_identidad", IssueKind.INVALID),
        ("codigo_de_ficha", IssueKind.INVALID),
        ("fecha_de_visita", IssueKind.INVALID),
    } <= k


def test_low_confidence_flag_on_empty_cell_is_yellow_not_red(schema, mapping):
    a = complete_answers(schema)
    a["telefono"] = ""
    issues, status = validate_row(_row(a, {"telefono": "Confianza baja (0.40): '3101234567'"}), schema, mapping)
    assert kinds(issues) == {("telefono", IssueKind.LOW_CONFIDENCE)}
    assert "3101234567" in issues[0].message
    assert status is Status.NEEDS_REVIEW


def test_flag_clears_once_the_human_fills_the_cell(schema, mapping):
    a = complete_answers(schema)
    assert validate_row(_row(a, {"telefono": "Confianza baja"}), schema, mapping)[0] == []


def test_conditional_require(schema, mapping):
    a = complete_answers(schema)
    a["eps_actual"] = "OTRA"
    a["cual"] = ""
    assert ("cual", IssueKind.REQUIRED) in kinds(validate_row(_row(a), schema, mapping)[0])
    a["cual"] = "SALUD XYZ"
    assert validate_row(_row(a), schema, mapping)[0] == []


def test_conditional_default_conflict_is_invalid(schema, mapping):
    a = complete_answers(schema)
    a["cuenta_con_alguna_discapacidad"] = "NO"
    a["en_caso_de_ser_si_la_respuesta_especifique_cual"] = "VISUAL"
    assert ("en_caso_de_ser_si_la_respuesta_especifique_cual", IssueKind.INVALID) in kinds(
        validate_row(_row(a), schema, mapping)[0]
    )


def test_checkbox_values(schema, mapping):
    from plan_cuidado.domain.schema import Question, QuestionType

    q = Question(item_id=1, entry_id=2, key="cb", label="CB", title="CB", type=QuestionType.CHECKBOX, options=["A", "B"])
    s = schema.model_copy(update={"questions": [q]})
    assert validate_row(_row({"cb": "A; B"}), s, mapping)[0] == []
    assert kinds(validate_row(_row({"cb": "A; Z"}), s, mapping)[0]) == {("cb", IssueKind.NOT_IN_OPTIONS)}


def test_full_pipeline_from_static(schema, mapping):
    a = complete_answers(schema)
    a.pop("en_caso_de_ser_si_la_respuesta_especifique_cual")
    row = apply_static_and_derived(_row(a), schema, mapping, {})
    assert validate_row(row, schema, mapping)[1] is Status.READY


def test_model_disagreement_is_purple_and_takes_precedence(schema, mapping):
    answers = complete_answers(schema)
    row = TrackerRow(
        row_id="r",
        answers=answers,
        ocr_disagreements={"telefono": {"value": "3101234567", "note": "Los modelos no coinciden"}},
    )
    issues, status = validate_row(row, schema, mapping)
    assert kinds(issues) == {("telefono", IssueKind.DISAGREEMENT)}
    assert status is Status.NEEDS_REVIEW


def test_disagreement_over_low_confidence_when_value_empty(schema, mapping):
    answers = complete_answers(schema)
    del answers["telefono"]
    row = TrackerRow(
        row_id="r",
        answers=answers,
        ocr_flags={"telefono": "Confianza baja"},
        ocr_disagreements={"telefono": {"value": "", "note": "no coinciden"}},
    )
    issues, _ = validate_row(row, schema, mapping)
    assert kinds(issues) == {("telefono", IssueKind.DISAGREEMENT)}


def test_disagreement_resolved_when_user_edits_value(schema, mapping):
    answers = complete_answers(schema)  # telefono differs from the stored primary reading
    row = TrackerRow(row_id="r", answers=answers, ocr_disagreements={"telefono": {"value": "3000000000", "note": "x"}})
    issues, status = validate_row(row, schema, mapping)
    assert issues == [] and status is Status.READY
