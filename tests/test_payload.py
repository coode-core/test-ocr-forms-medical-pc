from urllib.parse import parse_qsl, urlsplit

from plan_cuidado.domain.payload import build_submission, prefilled_url
from plan_cuidado.domain.schema import FormSchema, Question, QuestionType
from plan_cuidado.domain.tracker import Status, TrackerRow


def q(key, entry, qtype, options=None, section=0, has_other=False):
    return Question(
        item_id=entry + 1000, entry_id=entry, key=key, label=key.upper(), title=key, type=qtype,
        options=options or [], section=section, has_other=has_other,
    )


SCHEMA = FormSchema(
    title="T",
    public_id="e/ABC",
    section_count=2,
    questions=[
        q("name", 1, QuestionType.SHORT_TEXT),
        q("visit", 2, QuestionType.DATE),
        q("pick", 3, QuestionType.RADIO, ["A", "B"]),
        q("many", 4, QuestionType.CHECKBOX, ["X", "Y", "Z"], section=1),
        q("empty", 5, QuestionType.SHORT_TEXT),
        q("other", 6, QuestionType.RADIO, ["A"], has_other=True),
    ],
)


def row(**answers):
    return TrackerRow(row_id="r", status=Status.READY, answers=answers)


def test_payload_entries():
    p = build_submission(row(name="ANA", visit="2026-08-06", pick="B", many="X; Z", empty=""), SCHEMA)
    assert ("entry.1", "ANA") in p
    assert ("entry.2_year", "2026") in p
    assert ("entry.2_month", "8") in p
    assert ("entry.2_day", "6") in p
    assert ("entry.3", "B") in p
    assert [v for k, v in p if k == "entry.4"] == ["X", "Z"]
    assert not any(k == "entry.5" for k, _ in p)
    assert ("pageHistory", "0,1") in p
    assert ("fvv", "1") in p


def test_other_option():
    p = build_submission(row(other="ALGO MAS"), SCHEMA)
    assert ("entry.6", "__other_option__") in p
    assert ("entry.6.other_option_response", "ALGO MAS") in p


def test_real_schema_single_section(schema):
    p = build_submission(row(codigo_de_ficha="0202012345"), schema)
    assert ("entry.136813167", "0202012345") in p
    assert ("pageHistory", "0") in p


def test_prefilled_url():
    url = prefilled_url(row(name="ANA MARÍA", visit="2026-08-06", many="X; Y"), SCHEMA)
    parts = urlsplit(url)
    assert parts.path == "/forms/d/e/ABC/viewform"
    qs = parse_qsl(parts.query)
    assert ("usp", "pp_url") in qs
    assert ("entry.1", "ANA MARÍA") in qs
    assert ("entry.2", "2026-08-06") in qs
    assert [v for k, v in qs if k == "entry.4"] == ["X", "Y"]
