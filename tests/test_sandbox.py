from plan_cuidado.domain.sandbox import build_forms_api_requests, compare_answers, response_to_answers
from plan_cuidado.domain.tracker import Status, TrackerRow
from plan_cuidado.services.sandbox import run_sandbox
from tests.fakes import FakeSubmitter, FakeTracker
from tests.test_validate import complete_answers


def test_build_requests_cover_every_question(schema):
    reqs = build_forms_api_requests(schema)
    assert len(reqs) == len(schema.questions)
    first = reqs[1]["createItem"]
    assert first["location"] == {"index": 1}
    assert first["item"]["title"] == "CODIGO DE FICHA:"
    assert first["item"]["questionItem"]["question"]["required"] is True
    assert "textQuestion" in first["item"]["questionItem"]["question"]
    barrio = next(r for r in reqs if r["createItem"]["item"]["title"] == "BARRIO:")
    choice = barrio["createItem"]["item"]["questionItem"]["question"]["choiceQuestion"]
    barrio_opts = schema.by_key("barrio").options
    # the real form repeats 2 barrios; the Forms API rejects duplicate options
    assert choice["type"] == "RADIO" and len(choice["options"]) == len(set(barrio_opts)) == 308
    date_q = reqs[2]["createItem"]["item"]
    assert date_q["title"] == "FECHA DE VISITA:"  # newlines removed
    assert date_q["questionItem"]["question"]["dateQuestion"] == {"includeYear": True, "includeTime": False}
    para = next(r for r in reqs if "HALLAZGOS" in r["createItem"]["item"]["title"])
    assert para["createItem"]["item"]["questionItem"]["question"]["textQuestion"] == {"paragraph": True}


def test_response_to_answers_and_compare(schema):
    qid_titles = {"a1": "CODIGO DE FICHA:", "a2": "BARRIO:"}
    response = {
        "answers": {
            "a1": {"questionId": "a1", "textAnswers": {"answers": [{"value": "0202012345"}]}},
            "a2": {"questionId": "a2", "textAnswers": {"answers": [{"value": "ARIZONA"}]}},
        }
    }
    answers = response_to_answers(response, qid_titles)
    assert answers == {"codigo_de_ficha": "0202012345", "barrio": "ARIZONA"}
    row = TrackerRow(row_id="r", answers={"codigo_de_ficha": "0202012345", "barrio": "COMPARTIR", "edad": ""})
    assert compare_answers(row, answers) == [("barrio", "COMPARTIR", "ARIZONA")]


class FakeFormsApi:
    def __init__(self, clone_html, responses_from):
        self.clone_html = clone_html
        self.responses_from = responses_from
        self.created = None

    def create_form(self, title, requests):
        self.created = (title, requests)
        return "FORM1", "https://docs.google.com/forms/d/e/CLONE/viewform"

    def question_titles(self, form_id):
        return {f"q{q.item_id}": q.title for q in self.responses_from.questions}

    def list_responses(self, form_id):
        out = []
        for _, payload in self.submitter.sent:
            entries = dict(payload)
            answers = {}
            for q in self.responses_from.questions:
                if f"entry.{q.entry_id}" in entries:
                    answers[f"q{q.item_id}"] = {"textAnswers": {"answers": [{"value": entries[f"entry.{q.entry_id}"]}]}}
            out.append({"answers": answers})
        return out


def test_run_sandbox_end_to_end_with_fakes(schema, mapping, form_html):
    rows = [TrackerRow(row_id=f"r{i}", status=Status.READY, answers=complete_answers(schema)) for i in range(4)]
    tracker = FakeTracker(rows)
    submitter = FakeSubmitter()
    api = FakeFormsApi(form_html, schema)
    api.submitter = submitter
    report = run_sandbox(api, tracker, schema, mapping, fetch_html=lambda url: form_html, submitter=submitter,
                         rows_limit=3, sleep=lambda s: None)
    assert api.created[0].startswith("[SANDBOX]")
    assert len(submitter.sent) == 3
    assert report.submitted == 3
    # text answers survive; dates are split in the payload so they don't round-trip in this fake
    assert all(k in {"fecha_de_visita", "fecha_de_nacimiento", "fecha_de_canalizacion"} for _, k, _, _ in report.mismatches)
    # the real tracker is never touched by the sandbox
    assert all(r.status is Status.READY for r in tracker.rows)


def test_google_forms_api_retries_every_call():
    from plan_cuidado.adapters import net
    from plan_cuidado.adapters.forms_api import GoogleFormsApi

    log = []

    class Req:
        def __init__(self, result):
            self.result = result

        def execute(self, **kwargs):
            log.append(kwargs)
            return self.result

    class Responses:
        def list(self, **kwargs):
            return Req({"responses": [{"createTime": "1"}]})

    class Forms:
        def create(self, **kwargs):
            return Req({"formId": "F"})

        def batchUpdate(self, **kwargs):
            return Req({})

        def setPublishSettings(self, **kwargs):
            return Req({})

        def get(self, **kwargs):
            return Req({"responderUri": "https://r", "items": []})

        def responses(self):
            return Responses()

    class Service:
        def forms(self):
            return Forms()

    api = GoogleFormsApi(Service())
    assert api.create_form("t", []) == ("F", "https://r")
    assert api.question_titles("F") == {}
    assert len(api.list_responses("F")) == 1
    assert log and all(kw == {"num_retries": net.GOOGLE_NUM_RETRIES} for kw in log)
