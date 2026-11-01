from plan_cuidado.domain.schema import (
    QuestionType,
    diff_schemas,
    form_response_url,
    parse_form_html,
    slugify,
    viewform_url,
)


def test_parses_title_and_public_id(schema):
    assert schema.title.startswith("FORMULARIO PLAN DE CUIDADO INTEGRAL")
    assert schema.public_id == "e/1FAIpQLSeVEWX0_CKBJHchjbxwid3fgb35vRtWwSZU1CpaxO3YNoVLQA"


def test_parses_all_questions_skipping_text_items(schema):
    # 52 items in the form, one of them is a plain title block (type 6)
    assert len(schema.questions) == 51
    assert all(q.entry_id for q in schema.questions)


def test_required_count(schema):
    assert sum(q.required for q in schema.questions) == 46


def test_question_details(schema):
    ficha = schema.by_key("codigo_de_ficha")
    assert ficha.entry_id == 136813167
    assert ficha.type is QuestionType.SHORT_TEXT
    assert ficha.required is True
    assert ficha.label == "CODIGO DE FICHA"

    visit = schema.by_key("fecha_de_visita")
    assert visit.type is QuestionType.DATE
    assert visit.include_year is True
    assert visit.include_time is False

    barrio = schema.by_key("barrio")
    assert barrio.type is QuestionType.RADIO
    assert len(barrio.options) == 310
    assert barrio.options[0] == "ABC SANTA ANA"

    assert schema.by_key("hallazgos_relevantes_identificados_en_la_familia").type is QuestionType.PARAGRAPH


def test_keys_are_unique(schema):
    keys = [q.key for q in schema.questions]
    assert len(keys) == len(set(keys))


def test_single_section_form(schema):
    assert schema.section_count == 1
    assert {q.section for q in schema.questions} == {0}


def test_urls(schema):
    assert form_response_url(schema) == (
        "https://docs.google.com/forms/d/e/1FAIpQLSeVEWX0_CKBJHchjbxwid3fgb35vRtWwSZU1CpaxO3YNoVLQA/formResponse"
    )
    assert viewform_url(schema).endswith("/viewform")


def test_slugify():
    assert slugify(" \nFECHA DE VISITA: ") == "fecha_de_visita"
    assert slugify("CÓDIGO DEL TERRITORIO:") == "codigo_del_territorio"
    assert slugify("¿CUAL?") == "cual"
    long = slugify("LA PERSONA FUE CANALIZADA A LOS SERVICIOS DE SALUD PARA CONTROL DE RIESGO")
    assert len(long) <= 60 and not long.endswith("_")


def _html_with(data: str) -> str:
    return f"<script>var FB_PUBLIC_LOAD_DATA_ = {data};</script>"


def test_sections_checkbox_other_and_time():
    data = (
        '[null,[null,[' 
        '[1,"Q one",null,4,[[11,[["A",null,null,null,0],["",null,null,null,1]],1]]],'
        '[2,"Page two",null,8,null],'
        '[3,"When",null,10,[[33,null,0]]]'
        '],null,null,null,null,null,null,"Title"],"/forms","Title",null,null,null,"",null,0,0,null,"",0,"e/XYZ"]'
    )
    s = parse_form_html(_html_with(data))
    assert s.section_count == 2
    q1, q3 = s.questions
    assert q1.type is QuestionType.CHECKBOX
    assert q1.options == ["A"]
    assert q1.has_other is True
    assert q1.section == 0
    assert q3.type is QuestionType.TIME
    assert q3.section == 1
    assert s.public_id == "e/XYZ"


def test_diff_schemas(schema):
    trimmed = schema.model_copy(update={"questions": schema.questions[1:]})
    changed_q = schema.questions[2].model_copy(update={"options": ["X"]})
    other = schema.model_copy(update={"questions": [changed_q, *schema.questions[3:]]})
    d = diff_schemas(trimmed, other)
    assert d.removed == [schema.questions[1].key]
    assert d.added == []
    assert d.changed == [changed_q.key]
    assert not diff_schemas(schema, schema).has_changes
