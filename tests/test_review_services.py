import pytest

from plan_cuidado.domain.schema import FormSchema
from plan_cuidado.domain.tracker import Status, TrackerRow
from plan_cuidado.services.review import fill_static, validate_all
from plan_cuidado.services.schema import SetupRequired, load_schema, refresh_schema
from tests.fakes import FakeDrive, FakeTracker
from tests.test_validate import complete_answers


def test_refresh_schema_writes_config_tracker_and_static_tab(form_html, schema):
    drive, tracker = FakeDrive(), FakeTracker()
    result = refresh_schema(form_html, drive, tracker)
    assert result.diff is None and result.static_migrated == 0
    assert set(drive.configs) == {"form_schema.json"}  # static_data.json is no longer generated
    assert tracker.layout == schema
    assert set(tracker.static) == set(schema.keys)
    assert load_schema(drive) == schema


def test_refresh_schema_reports_diff(form_html, schema):
    old = schema.model_copy(update={"questions": schema.questions[1:]})
    drive = FakeDrive(configs={"form_schema.json": old.model_dump(mode="json")})
    result = refresh_schema(form_html, drive, FakeTracker())
    assert result.diff.added == [schema.questions[0].key]


def test_legacy_static_json_is_migrated_once(form_html, schema):
    static = {"valores": {"tipo_de_visita": "PRIMERA VISITA", "barrio": ""}}
    drive = FakeDrive(configs={"static_data.json": static})
    tracker = FakeTracker()
    result = refresh_schema(form_html, drive, tracker)
    assert result.static_migrated == 1
    assert tracker.read_static() == {"tipo_de_visita": "PRIMERA VISITA"}
    assert drive.configs["static_data.json"].get("_migrado")
    # the user clears the value in the tab: a second refresh must not copy it again
    tracker.static["tipo_de_visita"] = ""
    again = refresh_schema(form_html, drive, tracker)
    assert again.static_migrated == 0 and tracker.read_static() == {}


def test_load_schema_requires_setup():
    with pytest.raises(SetupRequired):
        load_schema(FakeDrive())


def test_fill_static_updates_only_changed_rows(schema, mapping):
    mapping = mapping.model_copy(update={"static_defaults": {}})
    rows = [
        TrackerRow(row_id="a", answers={}),
        TrackerRow(row_id="b", answers={"tipo_de_visita": "SEGUIMIENTO"}),
        TrackerRow(row_id="c", status=Status.SUBMITTED, answers={}),
    ]
    tracker = FakeTracker(rows)
    n = fill_static(tracker, schema, mapping, {"tipo_de_visita": "PRIMERA VISITA"})
    assert n == 1
    assert tracker.get("a").answers["tipo_de_visita"] == "PRIMERA VISITA"
    assert tracker.get("b").answers["tipo_de_visita"] == "SEGUIMIENTO"
    assert tracker.get("c").answers == {}


def test_fill_static_uses_yaml_defaults_below_the_static_tab(schema, mapping):
    mapping = mapping.model_copy(update={"static_defaults": {"tipo_de_visita": "SEGUIMIENTO", "etnia": "NINGUNO"}})
    tracker = FakeTracker([TrackerRow(row_id="a", answers={})])
    fill_static(tracker, schema, mapping, {"tipo_de_visita": "PRIMERA VISITA"})
    answers = tracker.get("a").answers
    assert answers["tipo_de_visita"] == "PRIMERA VISITA"  # the tab wins over the YAML
    assert answers["etnia"] == "NINGUNO"


def test_yaml_static_defaults_are_valid_form_options(schema, mapping):
    for key, value in mapping.static_defaults.items():
        q = schema.get(key)
        assert q is not None, key
        assert not q.options or value in q.options, (key, value)


def test_validate_all_sets_statuses_and_marks(schema, mapping):
    rows = [
        TrackerRow(row_id="ok", answers=complete_answers(schema)),
        TrackerRow(row_id="bad", answers={}),
        TrackerRow(row_id="done", status=Status.SUBMITTED, answers={}),
        TrackerRow(row_id="retry", status=Status.FAILED, answers=complete_answers(schema), error="timeout"),
    ]
    tracker = FakeTracker(rows)
    report = validate_all(tracker, schema, mapping)
    assert report.ready == 2 and report.needs_review == 1
    assert tracker.get("ok").status is Status.READY
    assert tracker.get("bad").status is Status.NEEDS_REVIEW
    assert tracker.get("done").status is Status.SUBMITTED
    assert tracker.get("retry").status is Status.READY
    marked_rows, issues = tracker.marks
    assert "done" not in {r.row_id for r in marked_rows}
    assert issues and all(i.row_id == "bad" for i in issues)
